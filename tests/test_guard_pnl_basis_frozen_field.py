"""behavioral_guard: 日次基準の妥当性を「計算時点で凍結したフィールド」から
判定する (2026-09 レビュー Codex 指摘 #1・#3)。

再現された欠陥（#1・最重要）: last_eod_portfolio_value_as_of は2つの異なる
役割を1つのフィールドで兼ねていた ―― (a) 今日の daily_pnl_jpy を計算した
基準の記録、(b) --eod が翌日向けに先取りする新基準のステージング場所。
正常な EOD 確定（17:35 cron、失敗ではない）が (b) として今日の日付を
書き込むと、直後に _print_status() が (a) の意味で再評価し、
delta=0 を「無効」と判定して正常な当日損益まで data_confidence_caution/
new_entry_allowed=False にしてしまっていた。

再現された欠陥（#3）: 1〜4日の単純な暦日差では、火曜基準・水曜欠測・木曜評価
のような「平日1日分の欠測」を通してしまう（3連休は許容すべきだが、
平日の欠測は許容してはいけない）。

修正: daily_pnl_basis_as_of という専用フィールドを新設し、
update_pnl/snapshot_portfolio_pnl が計算時点の基準を都度そこへ凍結する。
--eod による last_eod_portfolio_value_as_of の書き換えはこのフィールドに
影響しない。妥当性判定は「直前の営業日（土日のみ考慮、祝日は対象外）と
一致するか」の完全一致にする（単純な暦日差の範囲チェックをやめる）。
"""
from __future__ import annotations

import json
import sys

import pytest

import behavioral_guard as bg


def _frozen_date(y, m, d):
    from datetime import date
    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return date(y, m, d)
    return _FrozenDate


def _frozen_clock(monkeypatch, y, m, d, hh=17, mm=35, ss=0):
    """date.today() と datetime.now() を両方同じ日に固定する。

    update_pnl/snapshot_portfolio_pnl は portfolio_value_as_of の刻印に
    datetime.now() を使うため、date だけ固定すると date.today() が返す
    「今日」と datetime.now() が返す実際の今日がズレて
    _daily_pnl_basis_is_valid の pv_date チェックが噛み合わなくなる。
    """
    from datetime import date, datetime

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return date(y, m, d)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(y, m, d, hh, mm, ss)

    monkeypatch.setattr(bg, "date", _FrozenDate)
    monkeypatch.setattr(bg, "datetime", _FrozenDateTime)


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "guard_state.json"
    monkeypatch.setattr(bg, "STATE_FILE", f)
    return f


# ── _daily_pnl_basis_is_valid: 完全一致（直前営業日）を要求する ────────────

def test_basis_matching_prior_business_day_is_valid():
    state = {
        "portfolio_value_as_of": "2026-09-08T09:00:00",
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",  # 月曜の直前営業日=金曜ではなく前日(日曜明け)
    }
    # 2026-09-08 は火曜。直前営業日は月曜(2026-09-07)。
    assert bg._daily_pnl_basis_is_valid(state, as_of_date="2026-09-08") is True


def test_basis_one_weekday_gap_is_invalid():
    """再現の核心(#3): 火曜基準を、水曜が欠測した木曜の判定に使い回さない。"""
    state = {
        "portfolio_value_as_of": "2026-09-10T09:00:00",  # 木曜に計算はされている
        "daily_pnl_basis_as_of": "2026-09-08T17:35:00",  # だが基準は火曜のまま(水曜が欠測)
    }
    # 2026-09-10 は木曜。直前営業日は水曜(2026-09-09)であるべき。
    assert bg._daily_pnl_basis_is_valid(state, as_of_date="2026-09-10") is False


def test_basis_across_a_weekend_is_valid():
    """金曜確定 → 月曜評価は許容する（土日は営業日ではない）。"""
    state = {
        "portfolio_value_as_of": "2026-09-14T09:00:00",  # 月曜
        "daily_pnl_basis_as_of": "2026-09-11T17:35:00",  # 金曜
    }
    assert bg._daily_pnl_basis_is_valid(state, as_of_date="2026-09-14") is True


def test_portfolio_value_as_of_not_matching_as_of_date_is_invalid():
    """基準日は正しくても、その日自体に一度も計算が走っていなければ無効。"""
    state = {
        "portfolio_value_as_of": "2026-09-05T09:00:00",  # 別の日
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",
    }
    assert bg._daily_pnl_basis_is_valid(state, as_of_date="2026-09-08") is False


def test_missing_daily_pnl_basis_as_of_is_invalid():
    state = {"portfolio_value_as_of": "2026-09-08T09:00:00"}
    assert bg._daily_pnl_basis_is_valid(state, as_of_date="2026-09-08") is False


def test_malformed_basis_does_not_crash():
    state = {
        "portfolio_value_as_of": "2026-09-08T09:00:00",
        "daily_pnl_basis_as_of": "not-a-date",
    }
    assert bg._daily_pnl_basis_is_valid(state, as_of_date="2026-09-08") is False


# ── update_pnl / snapshot_portfolio_pnl が凍結フィールドを都度スタンプする ──

def test_update_pnl_stamps_daily_pnl_basis_as_of(state_file):
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 10_000_000.0
    init["last_eod_portfolio_value_as_of"] = "2026-09-07T17:35:00"
    init["portfolio_value"] = 10_000_000.0
    state_file.write_text(json.dumps(init))

    bg.update_pnl(pnl_jpy=0, portfolio_value=10_100_000.0)

    state = json.loads(state_file.read_text())
    assert state["daily_pnl_basis_as_of"] == "2026-09-07T17:35:00"


def test_snapshot_portfolio_pnl_stamps_daily_pnl_basis_as_of(state_file, monkeypatch):
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 10_000_000.0
    init["last_eod_portfolio_value_as_of"] = "2026-09-07T17:35:00"
    state_file.write_text(json.dumps(init))

    class _Fake:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 10_100_000.0, "positions": []}

    monkeypatch.setitem(sys.modules, "portfolio_manager", _Fake)

    result = bg.snapshot_portfolio_pnl()
    assert result["daily_pnl_basis_as_of"] == "2026-09-07T17:35:00"


# ── 再現の核心(#1): 正常な --eod 確定が当日自身の損益を無効化しない ────────

def test_normal_eod_confirmation_does_not_invalidate_todays_own_pnl(
    state_file, monkeypatch,
):
    """Codex 指摘の再現: EOD確定後に _print_status() が再評価しても、
    直前に正しく計算された当日損益は有効なまま。"""
    _frozen_clock(monkeypatch, 2026, 9, 8)
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 29_000_000.0
    init["last_eod_portfolio_value_as_of"] = "2026-09-07T17:35:00"
    init["date"] = "2026-09-08"
    state_file.write_text(json.dumps(init))

    class _Fake:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 29_290_000.0, "positions": []}  # +1%

    monkeypatch.setitem(sys.modules, "portfolio_manager", _Fake)

    state = bg.snapshot_portfolio_pnl()
    assert state["loss_guard_stage"] == "ok"
    assert state["new_entry_allowed"] is True

    rc = bg._run_snapshot_cli(["snapshot", "--eod"])
    assert rc == 0

    final = json.loads(state_file.read_text())
    assert final["loss_guard_stage"] == "ok", (
        "正常な EOD 確定が当日自身の損益を data_confidence_caution にしてはいけない"
    )
    assert final["new_entry_allowed"] is True
    assert final["daily_pnl_pct"] == pytest.approx(0.01, abs=1e-6)


def test_second_same_day_snapshot_after_eod_does_not_corrupt_todays_pnl(
    state_file, monkeypatch,
):
    """再現の核心（2026-09 レビュー Codex 3ラウンド目 指摘 #1）:
    上のテストは EOD確定で止まるが、実運用では同日中にもう一度
    snapshot が走り得る（手動再実行・analyst の self-heal 等）。
    --eod は last_eod_portfolio_value(_as_of) を翌日向けにステージング
    済みなので、その後の同日内 snapshot がこれを再度「今日の基準」として
    読み直すと、確定済みの当日損益が壊れる。"""
    _frozen_clock(monkeypatch, 2026, 9, 8)
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 29_000_000.0
    init["last_eod_portfolio_value_as_of"] = "2026-09-07T17:35:00"
    init["date"] = "2026-09-08"
    state_file.write_text(json.dumps(init))

    class _Fake:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 29_290_000.0, "positions": []}  # +1%

    monkeypatch.setitem(sys.modules, "portfolio_manager", _Fake)

    bg.snapshot_portfolio_pnl()
    rc = bg._run_snapshot_cli(["snapshot", "--eod"])
    assert rc == 0

    # 同日中、もう一度 snapshot（--eod 無し）が走る。
    second = bg.snapshot_portfolio_pnl()
    assert second["daily_pnl_pct"] == pytest.approx(0.01, abs=1e-6), (
        "2回目の同日 snapshot が基準を読み直し、当日損益を壊している"
    )
    second_eval = bg.evaluate(dict(second))
    assert second_eval["loss_guard_stage"] == "ok"
    assert second_eval["new_entry_allowed"] is True


def test_normal_day_is_archived_correctly_at_next_rollover(state_file, monkeypatch):
    """再現の核心（自己レビューで追加発見した派生バグ）: 正常な一日
    （成功したEOD確定）が翌朝のロールオーバーで basis_valid=False に
    誤記録されない。"""
    _frozen_clock(monkeypatch, 2026, 9, 7)
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 29_000_000.0
    init["last_eod_portfolio_value_as_of"] = "2026-09-04T17:35:00"  # 金曜（9/7=月曜の直前営業日）
    init["date"] = "2026-09-07"
    state_file.write_text(json.dumps(init))

    class _Fake:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 29_290_000.0, "positions": []}

    monkeypatch.setitem(sys.modules, "portfolio_manager", _Fake)

    bg._run_snapshot_cli(["snapshot", "--eod"])

    # 翌日 9/8 の最初の load_state()（ロールオーバー）
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    loaded = bg.load_state()
    entry = next((e for e in loaded["pnl_history"] if e["date"] == "2026-09-07"), None)
    assert entry is not None
    assert entry["pnl_jpy"] == pytest.approx(290_000.0, abs=1.0)
    assert entry.get("basis_valid", True) is True


def test_update_pnl_after_eod_same_day_does_not_corrupt_todays_pnl(
    state_file, monkeypatch,
):
    """update_pnl 経由（alert.update_guard_state の実経路）でも同じ再現。"""
    _frozen_clock(monkeypatch, 2026, 9, 8)
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 29_000_000.0
    init["last_eod_portfolio_value_as_of"] = "2026-09-07T17:35:00"
    init["date"] = "2026-09-08"
    init["portfolio_value"] = 29_290_000.0
    state_file.write_text(json.dumps(init))

    bg.update_pnl(pnl_jpy=0, portfolio_value=29_290_000.0)  # +1%, freezes today's basis

    # 明示的に --eod を模倣（load_state 経由の直接操作で最小再現）
    eod_state = bg.load_state()
    eod_state["last_eod_portfolio_value"] = eod_state.get("portfolio_value", 0.0)
    eod_state["last_eod_portfolio_value_as_of"] = eod_state.get("portfolio_value_as_of")
    bg.save_state(eod_state)

    second = bg.update_pnl(pnl_jpy=0, portfolio_value=29_290_000.0)
    assert second["daily_pnl_pct"] == pytest.approx(0.01, abs=1e-6), (
        "--eod 後の同日 update_pnl が基準を読み直し、当日損益を壊している"
    )


# ── 再現の核心(#5 3ラウンド目): update_pnl が portfolio_value を無検証で保存 ──

@pytest.mark.parametrize("bad_value", [0, -5_000_000, True, float("nan"), "not-a-number"])
def test_update_pnl_rejects_invalid_portfolio_value(state_file, bad_value):
    """再現 (2026-09 レビュー Codex 3ラウンド目 指摘 #5): snapshot_portfolio_pnl
    は書込み側の検証を持つが、alert.update_guard_state() から実際に呼ばれる
    もう一方の writer である update_pnl() には同じ検証が無く、0・負数・bool
    がそのまま portfolio_value として保存され得た。"""
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 10_000_000.0
    init["portfolio_value"] = 10_000_000.0
    state_file.write_text(json.dumps(init))

    with pytest.raises(ValueError):
        bg.update_pnl(pnl_jpy=0, portfolio_value=bad_value)

    # 拒否された呼出しは state を書き換えていない。
    state = json.loads(state_file.read_text())
    assert state["portfolio_value"] == 10_000_000.0


def test_update_pnl_accepts_valid_portfolio_value(state_file):
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 10_000_000.0
    state_file.write_text(json.dumps(init))

    result = bg.update_pnl(pnl_jpy=0, portfolio_value=10_100_000.0)
    assert result["portfolio_value"] == 10_100_000.0

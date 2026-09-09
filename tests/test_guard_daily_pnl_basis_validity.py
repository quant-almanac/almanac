"""behavioral_guard: 日次P&L基準の妥当性を確認済みEODの新しさから導出する
(2026-09 レビュー S1b・Codex 指摘1、および独立レビューでの Codex 指摘 #1・#3)。

再現された欠陥（要約、詳細は tests/test_guard_eod_failure_handling.py）:
EOD 評価が失敗しても load_state() の日またぎロールオーバーは
``last_eod_portfolio_value = state.get('portfolio_value', 0.0)`` を無条件に
実行し、古い（何日も前の）評価額を「今日の基準」として確定してしまう。
その状態で計算された daily_pnl_pct は複数日分の差分を1日分として提示し、
日次 -3% ショックゲートを誤発火・誤不発火させ得る。

設計: 数値フィールド（daily_pnl_jpy/pct）は常に数値のまま保つ。妥当性は
_daily_pnl_basis_is_valid() が daily_pnl_basis_as_of（計算時点で凍結した
基準）と portfolio_value_as_of（実際にその日計算されたか）の両方から
導出する。last_eod_portfolio_value_as_of を直接は読まない ―― そのフィールド
は --eod が翌日向けに先取りステージングする役割も兼ねるため、当日中に
「今日計算済みの値の妥当性」を調べる目的には使えない
（独立レビュー Codex 指摘 #1 で判明: 正常な EOD 確定直後にその日自身の
daily_pnl が無効判定されるバグとして発見・再現）。

_daily_pnl_basis_is_valid 自体の単体テスト（naive/malformed/weekend 等）は
tests/test_guard_pnl_basis_frozen_field.py に集約した。このファイルは
load_state() のロールオーバー・_update_rolling30・evaluate() との
結合動作を扱う。
"""
from __future__ import annotations

import json
import sys
from datetime import date

import pytest

import behavioral_guard as bg


def _frozen_date(y, m, d):
    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return date(y, m, d)
    return _FrozenDate


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "guard_state.json"
    monkeypatch.setattr(bg, "STATE_FILE", f)
    return f


# ── load_state() のロールオーバー: portfolio_value_as_of で swap を判断 ───

def test_rollover_swaps_eod_baseline_when_portfolio_value_was_confirmed_for_yesterday(
    state_file, monkeypatch,
):
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    seed = bg._default_state()
    seed["date"] = "2026-09-07"
    seed["portfolio_value"] = 29_500_000.0
    seed["portfolio_value_as_of"] = "2026-09-07T17:35:00"  # 昨日 (prev_date) 確定
    seed["last_eod_portfolio_value"] = 29_000_000.0
    state_file.write_text(json.dumps(seed))

    loaded = bg.load_state()

    assert loaded["last_eod_portfolio_value"] == 29_500_000.0
    assert loaded["last_eod_portfolio_value_as_of"] == "2026-09-07T17:35:00"


def test_rollover_keeps_existing_baseline_when_portfolio_value_is_stale(
    state_file, monkeypatch,
):
    """再現ケース: 評価が何日も失敗していて portfolio_value_as_of が 昨日を
    指していない ―― 古い評価額を新しい EOD 基準として確定しない。"""
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    seed = bg._default_state()
    seed["date"] = "2026-09-07"
    seed["portfolio_value"] = 20_000_000.0  # 3日前の値のまま
    seed["portfolio_value_as_of"] = "2026-09-05T00:00:00"  # 3日前
    seed["last_eod_portfolio_value"] = 29_000_000.0
    seed["last_eod_portfolio_value_as_of"] = "2026-09-04T17:35:00"
    state_file.write_text(json.dumps(seed))

    loaded = bg.load_state()

    assert loaded["last_eod_portfolio_value"] == 29_000_000.0, (
        "古い portfolio_value を新しい EOD 基準として確定してはいけない"
    )
    assert loaded["last_eod_portfolio_value_as_of"] == "2026-09-04T17:35:00"


def test_rollover_keeps_existing_baseline_when_portfolio_value_as_of_is_absent(
    state_file, monkeypatch,
):
    """移行直後（旧 state に portfolio_value_as_of が無い）も安全側。"""
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    seed = bg._default_state()
    seed["date"] = "2026-09-07"
    seed["portfolio_value"] = 29_500_000.0
    del seed["portfolio_value_as_of"]
    seed["last_eod_portfolio_value"] = 29_000_000.0
    seed["last_eod_portfolio_value_as_of"] = "2026-09-04T17:35:00"
    state_file.write_text(json.dumps(seed))

    loaded = bg.load_state()

    assert loaded["last_eod_portfolio_value"] == 29_000_000.0


# ── ロールオーバー時の pnl_history 記録 ────────────────────────────────

def test_rollover_archives_valid_days_pnl_normally(state_file, monkeypatch):
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    seed = bg._default_state()
    seed["date"] = "2026-09-07"  # 月曜
    seed["daily_pnl_jpy"] = 12_345.0
    seed["portfolio_value"] = 29_500_000.0
    seed["portfolio_value_as_of"] = "2026-09-07T17:35:00"  # 月曜中に計算された
    seed["daily_pnl_basis_as_of"] = "2026-09-04T17:35:00"  # 直前営業日=金曜、一致
    state_file.write_text(json.dumps(seed))

    loaded = bg.load_state()

    entry = next(e for e in loaded["pnl_history"] if e["date"] == "2026-09-07")
    assert entry["pnl_jpy"] == 12_345.0
    assert entry.get("basis_valid", True) is True


def test_rollover_archives_invalid_days_as_none_not_as_a_multiday_delta(
    state_file, monkeypatch,
):
    """再現の核心: 基準が無効だった日は pnl_jpy=None で記録し、複数日分の
    差分をその1日の値として黙って残さない。"""
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    seed = bg._default_state()
    seed["date"] = "2026-09-07"
    seed["daily_pnl_jpy"] = -900_000.0  # 実は複数日分の差分
    seed["portfolio_value"] = 20_000_000.0
    seed["portfolio_value_as_of"] = "2026-09-05T00:00:00"  # 月曜中に計算されていない
    seed["daily_pnl_basis_as_of"] = "2026-08-20T17:35:00"  # 基準も古すぎる
    state_file.write_text(json.dumps(seed))

    loaded = bg.load_state()

    entry = next(e for e in loaded["pnl_history"] if e["date"] == "2026-09-07")
    assert entry["pnl_jpy"] is None
    assert entry["basis_valid"] is False


def test_rollover_flags_a_total_outage_day_unconfirmed_even_with_a_fresh_basis(
    state_file, monkeypatch,
):
    """基準が新鮮でも、その日一日評価が一度も成功しなかった
    （portfolio_value_as_of が前日を指さない）なら「確認済みゼロ変化」と
    誤認してはいけない（自己レビューで発見: 全休止日が無記録のまま 30日
    集計へ黙って0寄与していた。基準の新鮮さと、その日実際に評価が走ったかは
    別の条件であり、両方満たさない限り確定値として記録しない）。
    """
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    seed = bg._default_state()
    seed["date"] = "2026-09-07"  # 月曜。前営業日(金 9/4)基準は新鮮
    seed["daily_pnl_jpy"] = 0.0  # 一度も更新されず rollover 既定値のまま
    seed["portfolio_value"] = 29_000_000.0
    seed["portfolio_value_as_of"] = "2026-09-04T17:35:00"  # 金曜のまま。月曜中は一度も更新なし
    seed["last_eod_portfolio_value"] = 29_000_000.0
    seed["last_eod_portfolio_value_as_of"] = "2026-09-04T17:35:00"  # 金曜。月曜の基準としては新鮮
    seed["daily_pnl_basis_as_of"] = "2026-08-20T17:35:00"  # 実際に計算された形跡が無い(古いまま)
    state_file.write_text(json.dumps(seed))

    loaded = bg.load_state()

    entry = next((e for e in loaded["pnl_history"] if e["date"] == "2026-09-07"), None)
    assert entry is not None, "全休止日が無記録のまま消えてはいけない"
    assert entry["pnl_jpy"] is None
    assert entry["basis_valid"] is False


# ── ロールオーバー: 土日は「欠測」として記録しない (2026-09 Codex 3ラウンド目 #2) ──

def test_rollover_does_not_archive_a_weekend_prev_date_at_all(state_file, monkeypatch):
    """土曜から日曜への（本来起きないはずの）ロールオーバーが起きても、
    土曜を basis_valid=False の欠測として記録しない ―― 土曜はそもそも
    評価を予定していない日であり、平日の欠測とは区別する。"""
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 6))  # 日曜
    seed = bg._default_state()
    seed["date"] = "2026-09-05"  # 土曜（本来 state['date'] がここに来ること自体が想定外の経路）
    seed["portfolio_value_as_of"] = "2026-09-04T17:35:00"  # 金曜のまま、土曜は無評価
    state_file.write_text(json.dumps(seed))

    loaded = bg.load_state()

    entry = next((e for e in loaded["pnl_history"] if e["date"] == "2026-09-05"), None)
    assert entry is None, "土曜を欠測として pnl_history に記録してはいけない"


def test_weekend_status_calls_do_not_corrupt_the_following_mondays_guard(
    state_file, monkeypatch,
):
    """再現の核心 (2026-09 レビュー Codex 3ラウンド目 指摘 #2・実機再現):
    金曜に正常EOD確定 → 土日に status 表示（読み取り専用のつもりが
    load_state()+save_state() で date を土日へ進めてしまう）→ 月曜に
    正常な snapshot、という順序で、土日2日分が「基準未確認」として
    pnl_history に記録され、月曜の正常な結果まで
    data_confidence_caution/new_entry_allowed=False に巻き込んでいた。"""
    def freeze(y, m, d, hh=10, mm=0):
        from datetime import date as _date, datetime as _datetime

        class _D(_date):
            @classmethod
            def today(cls):
                return _date(y, m, d)

        class _DT(_datetime):
            @classmethod
            def now(cls, tz=None):
                return _datetime(y, m, d, hh, mm, 0)

        monkeypatch.setattr(bg, "date", _D)
        monkeypatch.setattr(bg, "datetime", _DT)

    # 金曜 2026-09-04、正常 EOD 確定。
    init = bg._default_state()
    init["date"] = "2026-09-04"
    init["last_eod_portfolio_value"] = 20_000_000.0
    init["last_eod_portfolio_value_as_of"] = "2026-09-03T17:35:00"
    init["daily_pnl_basis_as_of"] = "2026-09-03T17:35:00"
    init["daily_pnl_baseline_jpy"] = 20_000_000.0
    init["portfolio_value"] = 20_200_000.0
    init["portfolio_value_as_of"] = "2026-09-04T17:35:00"
    init["daily_pnl_jpy"] = 200_000.0
    init["daily_pnl_pct"] = 0.01
    state_file.write_text(json.dumps(init))

    # 土曜・日曜に status（読み取り目的の呼出しのつもりでも save_state する）。
    freeze(2026, 9, 5)
    bg._print_status()
    freeze(2026, 9, 6)
    bg._print_status()

    # 月曜、正常な snapshot。
    freeze(2026, 9, 7, 6, 30)

    class _Fake:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 20_400_000.0, "positions": []}

    monkeypatch.setitem(sys.modules, "portfolio_manager", _Fake)
    bg.snapshot_portfolio_pnl()

    final = bg.load_state()
    for weekend_date in ("2026-09-05", "2026-09-06"):
        entry = next((e for e in final["pnl_history"] if e["date"] == weekend_date), None)
        assert entry is None, f"{weekend_date}（土日）を欠測として記録してはいけない"
    assert final.get("monthly_pnl_basis_excluded_days", 0) == 0

    ev = bg.evaluate(dict(final))
    assert ev["loss_guard_stage"] == "ok"
    assert ev["new_entry_allowed"] is True


# ── _update_rolling30: 無効な日を除外する ──────────────────────────────

def test_update_rolling30_excludes_none_pnl_days_from_the_sum(monkeypatch):
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    state = {
        "date": "2026-09-08",  # 火曜。直前営業日は月曜(9/7)
        "pnl_history": [
            {"date": "2026-09-01", "pnl_jpy": 10_000.0},
            {"date": "2026-09-02", "pnl_jpy": None, "basis_valid": False},
            {"date": "2026-09-03", "pnl_jpy": 5_000.0},
        ],
        "daily_pnl_jpy": 0.0,
        "portfolio_value": 30_000_000.0,
        "portfolio_value_as_of": "2026-09-08T09:00:00",
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",
    }
    bg._update_rolling30(state)
    assert state["monthly_pnl_jpy"] == 15_000.0  # None の日は合計から除外、今日分(0.0)は含む
    assert state["monthly_pnl_basis_excluded_days"] == 1  # 過去の None 1日のみ


def test_update_rolling30_never_raises_typeerror_on_none_entries(monkeypatch):
    """再現: 旧実装は e['pnl_jpy'] を無条件 sum() していたため None 混入で
    TypeError になっていた。"""
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    state = {
        "pnl_history": [{"date": "2026-09-01", "pnl_jpy": None, "basis_valid": False}],
        "daily_pnl_jpy": 0.0,
        "portfolio_value": 30_000_000.0,
    }
    bg._update_rolling30(state)  # 例外を投げないことそのものがテスト


def test_update_rolling30_excludes_todays_contribution_when_basis_invalid(monkeypatch):
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    state = {
        "pnl_history": [],
        "daily_pnl_jpy": -900_000.0,  # 無効基準による多日差分
        "portfolio_value": 30_000_000.0,
        "date": "2026-09-08",
        "portfolio_value_as_of": "2026-09-08T09:00:00",  # 今日計算はされている
        "daily_pnl_basis_as_of": "2026-08-20T17:35:00",  # だが基準そのものが古すぎる
    }
    bg._update_rolling30(state)
    assert state["monthly_pnl_jpy"] == 0.0
    assert state["monthly_pnl_basis_excluded_days"] == 1


def test_update_rolling30_excludes_todays_contribution_when_never_computed_today(
    monkeypatch,
):
    """基準は新鮮でも、今日一度も計算されていない（portfolio_value_as_of が
    今日を指さない）なら今日分を含めない。"""
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    state = {
        "pnl_history": [],
        "daily_pnl_jpy": 0.0,
        "portfolio_value": 30_000_000.0,
        "date": "2026-09-08",
        "portfolio_value_as_of": "2026-09-05T09:00:00",  # 今日ではない
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",  # 基準自体は新鮮
    }
    bg._update_rolling30(state)
    assert state["monthly_pnl_basis_excluded_days"] == 1


def test_update_rolling30_includes_todays_contribution_when_basis_valid(monkeypatch):
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    state = {
        "pnl_history": [],
        "daily_pnl_jpy": 30_000.0,
        "portfolio_value": 30_000_000.0,
        "date": "2026-09-08",
        "portfolio_value_as_of": "2026-09-08T09:00:00",
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",
    }
    bg._update_rolling30(state)
    assert state["monthly_pnl_jpy"] == 30_000.0
    assert state["monthly_pnl_basis_excluded_days"] == 0


# ── evaluate(): 基準無効なら loss_guard_state へ None を渡す・KeyError無し ──

def test_evaluate_passes_none_daily_when_basis_invalid(monkeypatch):
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    captured = {}
    real = bg.loss_guard_state

    def _spy(**kw):
        captured.update(kw)
        return real(**kw)

    monkeypatch.setattr(bg, "loss_guard_state", _spy)

    state = bg._default_state()
    state["date"] = "2026-09-08"
    state["daily_pnl_pct"] = -0.20  # 無効基準による誤った多日差分
    state["monthly_pnl_pct"] = -0.02
    state["portfolio_value_as_of"] = "2026-09-08T09:00:00"
    state["daily_pnl_basis_as_of"] = "2026-08-20T17:35:00"  # 基準が古すぎる

    bg.evaluate(state)

    assert captured["daily_pnl_decimal"] is None


def test_evaluate_passes_real_daily_when_basis_valid(monkeypatch):
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    captured = {}
    real = bg.loss_guard_state

    def _spy(**kw):
        captured.update(kw)
        return real(**kw)

    monkeypatch.setattr(bg, "loss_guard_state", _spy)

    state = bg._default_state()
    state["date"] = "2026-09-08"
    state["daily_pnl_pct"] = -0.01
    state["monthly_pnl_pct"] = -0.02
    state["portfolio_value_as_of"] = "2026-09-08T09:00:00"
    state["daily_pnl_basis_as_of"] = "2026-09-07T17:35:00"

    bg.evaluate(state)

    assert captured["daily_pnl_decimal"] == -0.01


def test_evaluate_does_not_crash_when_loss_guard_returns_data_confidence_caution(
    monkeypatch,
):
    """再現: stage_by_name / labels に data_confidence_caution が無く KeyError
    していた。両方とも不明の state でも evaluate() が例外なく完走すること。"""
    monkeypatch.setattr(bg, "date", _frozen_date(2026, 9, 8))
    state = bg._default_state()
    state["date"] = "2026-09-08"
    state["daily_pnl_pct"] = -0.20  # 無効化される
    state["monthly_pnl_pct"] = None  # 30日も不明

    result = bg.evaluate(state)  # KeyError を投げないことそのものがテスト

    assert result["loss_guard_stage"] == "data_confidence_caution"
    assert result["guardrail_stage"] == 0
    assert result["new_entry_allowed"] is False  # bool(None) == False, 安全側

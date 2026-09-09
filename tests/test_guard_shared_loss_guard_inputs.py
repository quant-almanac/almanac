"""behavioral_guard.resolve_loss_guard_inputs(): guard_state.json を直接読む
全consumer（evaluate() 自身・execution_preflight・analyst/data_gatherer）が
同じ有効性判定を共有する (2026-09 独立レビュー Codex 指摘 #2・#4)。

再現された欠陥 #2: execution_preflight._guard_metrics と
analyst/data_gatherer._loss_guard_from_guard_state は guard_state.json の
daily_pnl_pct/monthly_pnl_pct を生の float として直接読み、
behavioral_guard.evaluate() が行う「基準は確認済みか」の判定を経由しない。
同じ stale な guard_state に対して、guard 自身は data_confidence_caution、
preflight/分析側は daily_block、のように異なる結論に達し得た。

再現された欠陥 #4: monthly_pnl_basis_excluded_days（30日集計のうち何日が
未確認で除外されたか）を保存はするが、判定側（loss_guard_state への
受け渡し）はこの件数を一切参照していなかった。除外1日を含む状態でも
30日損益0・stage=ok・新規リスク許可、になり得た。
"""
from __future__ import annotations

import pytest

import behavioral_guard as bg


def test_resolves_daily_and_rolling_when_both_confirmed():
    state = {
        "date": "2026-09-08",
        "daily_pnl_pct": -0.01,
        "monthly_pnl_pct": -0.02,
        "portfolio_value_as_of": "2026-09-08T09:00:00",
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",
        "monthly_pnl_basis_excluded_days": 0,
        "monthly_pnl_computed_for_date": "2026-09-08",
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["daily"] == -0.01
    assert result["rolling"] == -0.02


def test_daily_none_when_basis_invalid():
    state = {
        "date": "2026-09-08",
        "daily_pnl_pct": -0.20,
        "monthly_pnl_pct": -0.02,
        "monthly_pnl_basis_excluded_days": 0,
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["daily"] is None


def test_rolling_none_when_excluded_days_positive():
    """再現の核心 #4: 除外日数が1日でもあれば rolling を未確認扱いにする。"""
    state = {
        "date": "2026-09-08",
        "daily_pnl_pct": 0.0,
        "monthly_pnl_pct": 0.0,  # 除外分を除いた部分合計。表面上は0
        "portfolio_value_as_of": "2026-09-08T09:00:00",
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",
        "monthly_pnl_basis_excluded_days": 1,
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["daily"] == 0.0
    assert result["rolling"] is None


def test_rolling_confirmed_when_excluded_days_absent_or_zero():
    """excluded_days フィールド自体が無い（旧state・移行直後）場合は
    0扱いにする ―― 実装済みの通常運用を過度にブロックしない。"""
    state = {
        "date": "2026-09-08",
        "monthly_pnl_pct": -0.02,
        "monthly_pnl_computed_for_date": "2026-09-08",
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["rolling"] == -0.02


def test_malformed_excluded_days_fails_closed():
    state = {
        "date": "2026-09-08",
        "monthly_pnl_pct": -0.02,
        "monthly_pnl_basis_excluded_days": "not-a-number",
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["rolling"] is None


def test_non_dict_or_empty_guard_state_returns_all_none():
    assert bg.resolve_loss_guard_inputs(None, as_of_date="2026-09-08") == {
        "daily": None, "rolling": None,
    }
    assert bg.resolve_loss_guard_inputs({}, as_of_date="2026-09-08") == {
        "daily": None, "rolling": None,
    }


def test_evaluate_uses_the_shared_resolver_for_consistency(monkeypatch):
    """evaluate() 自身も resolve_loss_guard_inputs を経由し、二重実装を持たない。"""
    called = {}
    real = bg.resolve_loss_guard_inputs

    def _spy(state, *, as_of_date=None):
        called["called"] = True
        return real(state, as_of_date=as_of_date)

    monkeypatch.setattr(bg, "resolve_loss_guard_inputs", _spy)

    state = bg._default_state()
    state["date"] = "2026-09-08"
    bg.evaluate(state)

    assert called.get("called") is True


def test_evaluate_does_not_crash_when_rolling_is_none_but_daily_triggers_a_stage(
    monkeypatch,
):
    """自己レビューで発見: rolling=None を新設したことで、daily_block などの
    ラベル文字列を組み立てる際に rolling*100 が TypeError を投げていた
    （旧実装は rolling が None になる経路が無く、daily 側だけ NaN 表示に
    対応していた）。"""
    state = bg._default_state()
    state["date"] = "2026-09-08"
    state["daily_pnl_pct"] = -0.05  # daily_block 相当
    state["monthly_pnl_pct"] = 0.0
    state["portfolio_value_as_of"] = "2026-09-08T09:00:00"
    state["daily_pnl_basis_as_of"] = "2026-09-07T17:35:00"
    state["monthly_pnl_basis_excluded_days"] = 1  # rolling を None にする

    result = bg.evaluate(state)  # 例外を投げないことそのものがテスト

    assert result["loss_guard_stage"] == "daily_block"


def test_execution_preflight_guard_metrics_matches_evaluate_conclusion(tmp_path):
    """再現の核心 #2: 同じ stale な guard_state に対して、guard 自身
    (evaluate) と execution_preflight (_guard_metrics) が異なる結論に
    達しないことを直接確認する。"""
    import execution_preflight as ep

    # daily は無効（基準無し）・rolling も除外あり、という stale な state。
    state = {
        "date": "2026-09-08",
        "daily_pnl_pct": -0.20,
        "monthly_pnl_pct": 0.0,
        "monthly_pnl_basis_excluded_days": 1,
    }
    (tmp_path / "guard_state.json").write_text(__import__("json").dumps(state))

    guard_result = bg.evaluate(dict(state))
    daily, rolling = ep._guard_metrics(tmp_path)

    assert daily is None
    assert rolling is None
    # guard 自身も両方不明のはず（daily_pnl_pct は基準無しで無効・
    # monthly は除外日数ありで無効）。
    assert guard_result["loss_guard_stage"] == "data_confidence_caution"


def test_analyst_data_gatherer_uses_the_shared_resolver():
    """analyst/data_gatherer._loss_guard_from_guard_state も
    resolve_loss_guard_inputs を経由する（生の float を直接読まない）。"""
    import ast
    import pathlib

    src = pathlib.Path("analyst/data_gatherer.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_loss_guard_from_guard_state":
            found = "resolve_loss_guard_inputs" in ast.dump(node)
            break
    assert found, (
        "_loss_guard_from_guard_state が resolve_loss_guard_inputs を"
        " 経由していない（guard_state の生値を直接読んでいる可能性）"
    )


# ── 3ラウンド目 Codex 指摘 #3: as_of_date 無指定時の既定を「今日」にする ──

def test_no_explicit_as_of_date_defaults_to_real_today_not_guard_state_date(monkeypatch):
    """再現の核心 (2026-09 レビュー Codex 3ラウンド目 指摘 #3):
    execution_preflight._guard_metrics / analyst/data_gatherer は
    as_of_date を指定せずに呼ぶ。guard_state['date'] を「今日」の代わりに
    使うと、内部的に自己整合した stale な state（cron が何日も止まって
    いる）が「確認済み」として通ってしまう。"""
    from datetime import date as _date

    class _FrozenDate(_date):
        @classmethod
        def today(cls):
            return _date(2026, 9, 8)  # 実際の今日

    monkeypatch.setattr(bg, "date", _FrozenDate)

    stale_but_self_consistent = {
        "date": "2026-09-01",  # 1週間前のまま更新されていない
        "daily_pnl_pct": -0.20,
        "monthly_pnl_pct": -0.20,
        "monthly_pnl_basis_excluded_days": 0,
        "portfolio_value_as_of": "2026-09-01T09:00:00",
        "daily_pnl_basis_as_of": "2026-08-31T17:35:00",
    }

    result = bg.resolve_loss_guard_inputs(stale_but_self_consistent)  # as_of_date 無指定

    assert result["daily"] is None, "1週間stale なstateを「今日確認済み」にしてはいけない"


def test_explicit_as_of_date_still_takes_precedence(monkeypatch):
    """明示指定（evaluate() 自身の呼び方）は従来どおり優先する。"""
    from datetime import date as _date

    class _FrozenDate(_date):
        @classmethod
        def today(cls):
            return _date(2026, 9, 8)

    monkeypatch.setattr(bg, "date", _FrozenDate)

    state = {
        "date": "2026-09-01",
        "daily_pnl_pct": -0.02,
        "portfolio_value_as_of": "2026-09-01T09:00:00",
        "daily_pnl_basis_as_of": "2026-08-31T17:35:00",
    }

    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-01")
    assert result["daily"] == -0.02


# ── 3ラウンド目 Codex 指摘 #4: 欠損・bool・NaN の daily/rolling を拒否する ──

@pytest.mark.parametrize("bad_value", [None, False, True, float("nan"), float("inf")])
def test_daily_rejects_missing_bool_and_nonfinite_values(bad_value):
    state = {
        "date": "2026-09-08",
        "daily_pnl_pct": bad_value,
        "portfolio_value_as_of": "2026-09-08T09:00:00",
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["daily"] is None, f"daily_pnl_pct={bad_value!r} を確認済み扱いにしてはいけない"


@pytest.mark.parametrize("bad_value", [None, False, True, float("nan"), float("inf")])
def test_rolling_rejects_missing_bool_and_nonfinite_values(bad_value):
    state = {
        "date": "2026-09-08",
        "monthly_pnl_pct": bad_value,
        "monthly_pnl_basis_excluded_days": 0,
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["rolling"] is None, f"monthly_pnl_pct={bad_value!r} を確認済み扱いにしてはいけない"


def test_daily_still_accepts_a_legitimate_negative_value():
    """P&L は負数が正当な値 ―― positive_finite 相当で誤って弾いてはいけない。"""
    state = {
        "date": "2026-09-08",
        "daily_pnl_pct": -0.05,
        "portfolio_value_as_of": "2026-09-08T09:00:00",
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["daily"] == -0.05


def test_daily_still_accepts_a_legitimate_zero_value():
    state = {
        "date": "2026-09-08",
        "daily_pnl_pct": 0.0,
        "portfolio_value_as_of": "2026-09-08T09:00:00",
        "daily_pnl_basis_as_of": "2026-09-07T17:35:00",
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["daily"] == 0.0


# ── 4ラウンド目 Codex 指摘 #2: rolling も computed-for-date の鮮度を確認する ──
#
# excluded_days<=0（30日窓に欠損が無い）だけでは、その計算自体が「いつ」
# 行われたかを問わない。cron が丸々止まっていても、最後に計算された時点で
# たまたま窓に欠損が無ければ excluded_days=0 のまま永久に「確認済み」扱いに
# なり得る。実測: 1ヶ月以上前(8/1)の state（excluded_days=0）を、実際の
# 今日(9/8)を as_of_date として渡しても rolling=-0.13 が返り、
# stage_3（全リスク増加凍結・人間レビュー要求）に達した。


def test_rolling_rejects_a_computation_from_a_stale_date_even_with_zero_excluded_days():
    state = {
        "date": "2026-08-01",
        "monthly_pnl_pct": -0.13,
        "monthly_pnl_basis_excluded_days": 0,
        "monthly_pnl_computed_for_date": "2026-08-01",
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["rolling"] is None, "1ヶ月以上前に計算された rolling を確認済み扱いにしてはいけない"


def test_rolling_accepts_a_computation_made_today():
    state = {
        "date": "2026-09-08",
        "monthly_pnl_pct": -0.13,
        "monthly_pnl_basis_excluded_days": 0,
        "monthly_pnl_computed_for_date": "2026-09-08",
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["rolling"] == -0.13


def test_rolling_rejects_when_computed_for_date_is_absent():
    """旧 state（移行直後・このフィールド未導入時点の永続化データ）は
    安全側で未確認扱いにする。"""
    state = {
        "date": "2026-09-08",
        "monthly_pnl_pct": -0.13,
        "monthly_pnl_basis_excluded_days": 0,
    }
    result = bg.resolve_loss_guard_inputs(state, as_of_date="2026-09-08")
    assert result["rolling"] is None


def test_update_rolling30_stamps_monthly_pnl_computed_for_date(monkeypatch):
    from datetime import date as _date

    class _Frozen(_date):
        @classmethod
        def today(cls):
            return _date(2026, 9, 8)

    monkeypatch.setattr(bg, "date", _Frozen)
    state = bg._default_state()
    state["date"] = "2026-09-08"
    state["portfolio_value"] = 10_000_000.0
    bg._update_rolling30(state)

    assert state["monthly_pnl_computed_for_date"] == "2026-09-08"

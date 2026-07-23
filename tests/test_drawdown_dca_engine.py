"""Tests for drawdown_dca_engine guard functions.

The DCA engine fires automatic buy tranches during drawdowns. Three pure
helper functions gate every tranche release; a bug in any of them can cause:
  - _within_cooldown:           same tranche fires too often (over-trading)
  - _annual_budget_remaining:   annual ¥ cap exceeded
  - _tranche_conditions_met:    wrong conditions trigger (or suppress) a tranche

All three are tested in isolation against synthetic state dicts and market
snapshots — no yfinance, no LLM, no file I/O.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from drawdown_dca_engine import (  # noqa: E402
    ANNUAL_BUDGET_CAP_PCT,
    COOLDOWN_DAYS,
    _annual_budget_remaining_pct,
    _tranche_conditions_met,
    _within_cooldown,
)


# ---------------------------------------------------------------------------
# _within_cooldown
# ---------------------------------------------------------------------------


def test_within_cooldown_false_when_never_fired() -> None:
    state = {"last_fired": {}}
    assert _within_cooldown(state, "T1") is False


def test_within_cooldown_false_when_tranche_not_in_state() -> None:
    state = {"last_fired": {"T2": date.today().isoformat()}}
    assert _within_cooldown(state, "T1") is False


def test_within_cooldown_true_when_fired_today() -> None:
    state = {"last_fired": {"T1": date.today().isoformat()}}
    assert _within_cooldown(state, "T1") is True


def test_within_cooldown_true_when_fired_recently() -> None:
    """Fired COOLDOWN_DAYS-1 days ago → still within cooldown."""
    recent = (date.today() - timedelta(days=COOLDOWN_DAYS - 1)).isoformat()
    state = {"last_fired": {"T1": recent}}
    assert _within_cooldown(state, "T1") is True


def test_within_cooldown_false_when_fired_beyond_window() -> None:
    """Fired COOLDOWN_DAYS+1 days ago → cooldown expired."""
    old = (date.today() - timedelta(days=COOLDOWN_DAYS + 1)).isoformat()
    state = {"last_fired": {"T1": old}}
    assert _within_cooldown(state, "T1") is False


def test_within_cooldown_exactly_at_boundary() -> None:
    """Fired exactly COOLDOWN_DAYS days ago — boundary: .days < COOLDOWN_DAYS is False."""
    boundary = (date.today() - timedelta(days=COOLDOWN_DAYS)).isoformat()
    state = {"last_fired": {"T1": boundary}}
    # (today - boundary).days == COOLDOWN_DAYS, which is NOT < COOLDOWN_DAYS
    assert _within_cooldown(state, "T1") is False


def test_within_cooldown_handles_malformed_date() -> None:
    """Malformed date string in state → treated as never fired."""
    state = {"last_fired": {"T1": "not-a-date"}}
    assert _within_cooldown(state, "T1") is False


def test_within_cooldown_handles_missing_last_fired_key() -> None:
    state = {}  # no "last_fired" key at all
    assert _within_cooldown(state, "T1") is False


# ---------------------------------------------------------------------------
# _annual_budget_remaining_pct
# ---------------------------------------------------------------------------


def test_annual_budget_full_when_nothing_spent() -> None:
    state = {"year": date.today().year, "annual_spent_pct": 0.0}
    remaining = _annual_budget_remaining_pct(state)
    assert remaining == pytest.approx(ANNUAL_BUDGET_CAP_PCT)


def test_annual_budget_partial_spend() -> None:
    spent = ANNUAL_BUDGET_CAP_PCT / 2
    state = {"year": date.today().year, "annual_spent_pct": spent}
    assert _annual_budget_remaining_pct(state) == pytest.approx(spent)


def test_annual_budget_fully_spent_returns_zero() -> None:
    state = {"year": date.today().year, "annual_spent_pct": ANNUAL_BUDGET_CAP_PCT}
    assert _annual_budget_remaining_pct(state) == pytest.approx(0.0)


def test_annual_budget_overspent_clamps_to_zero() -> None:
    state = {"year": date.today().year, "annual_spent_pct": ANNUAL_BUDGET_CAP_PCT * 2}
    assert _annual_budget_remaining_pct(state) == 0.0


def test_annual_budget_resets_on_year_change() -> None:
    """Year in state != current year → treat as fresh start (full budget)."""
    state = {"year": date.today().year - 1, "annual_spent_pct": 0.10}
    assert _annual_budget_remaining_pct(state) == pytest.approx(ANNUAL_BUDGET_CAP_PCT)


def test_annual_budget_handles_missing_fields() -> None:
    """State with missing keys → falls back to full budget (year mismatch)."""
    state = {}  # no year, no spent
    # state.get("year") returns None, which != today.year → full budget returned
    assert _annual_budget_remaining_pct(state) == pytest.approx(ANNUAL_BUDGET_CAP_PCT)


# ---------------------------------------------------------------------------
# _tranche_conditions_met — T1 (様子見エントリー)
# T1: dd ≤ -0.08 AND VIX ≥ 25 AND VIX-decay ≤ -10%
# ---------------------------------------------------------------------------


def _t1_pass() -> tuple[dict, dict, dict, dict]:
    dd      = {"dd_from_peak": -0.09}           # ≤ -0.08 ✓
    panic   = {}                                 # T1 uses no F&G / HY OAS
    vix_ctx = {"vix": {"level": 26.0, "decay_from_peak_5d_pct": -11.0}}
    rsi     = {}
    return dd, panic, vix_ctx, rsi


def test_t1_passes_all_conditions() -> None:
    dd, panic, vix_ctx, rsi = _t1_pass()
    ok, reasons = _tranche_conditions_met("T1", dd, panic, vix_ctx, rsi)
    assert ok is True
    assert len(reasons) > 0


def test_t1_fails_when_dd_not_deep_enough() -> None:
    dd, panic, vix_ctx, rsi = _t1_pass()
    dd["dd_from_peak"] = -0.05   # shallower than -0.08
    ok, _ = _tranche_conditions_met("T1", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t1_fails_when_dd_is_none() -> None:
    dd, panic, vix_ctx, rsi = _t1_pass()
    dd["dd_from_peak"] = None
    ok, _ = _tranche_conditions_met("T1", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t1_fails_when_vix_too_low() -> None:
    dd, panic, vix_ctx, rsi = _t1_pass()
    vix_ctx["vix"]["level"] = 20.0   # < 25
    ok, _ = _tranche_conditions_met("T1", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t1_fails_when_vix_decay_insufficient() -> None:
    dd, panic, vix_ctx, rsi = _t1_pass()
    vix_ctx["vix"]["decay_from_peak_5d_pct"] = -5.0   # > -10% → not decaying enough
    ok, _ = _tranche_conditions_met("T1", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t1_fails_when_vix_none() -> None:
    dd, panic, vix_ctx, rsi = _t1_pass()
    vix_ctx["vix"]["level"] = None
    ok, _ = _tranche_conditions_met("T1", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t1_reasons_list_is_non_empty() -> None:
    dd, panic, vix_ctx, rsi = _t1_pass()
    _, reasons = _tranche_conditions_met("T1", dd, panic, vix_ctx, rsi)
    assert isinstance(reasons, list)
    assert len(reasons) >= 1


# ---------------------------------------------------------------------------
# _tranche_conditions_met — T2 (本格買い下がり)
# T2: dd ≤ -0.12 AND VIX ≥ 25 AND F&G ≤ 25 AND HY OAS ≥ 500bps
# ---------------------------------------------------------------------------


def _t2_pass() -> tuple[dict, dict, dict, dict]:
    dd      = {"dd_from_peak": -0.13}
    panic   = {"fear_greed": 20, "hy_oas_bps": 550}
    vix_ctx = {"vix": {"level": 28.0}}
    rsi     = {}
    return dd, panic, vix_ctx, rsi


def test_t2_passes_all_conditions() -> None:
    dd, panic, vix_ctx, rsi = _t2_pass()
    ok, _ = _tranche_conditions_met("T2", dd, panic, vix_ctx, rsi)
    assert ok is True


def test_t2_fails_when_dd_not_deep_enough() -> None:
    dd, panic, vix_ctx, rsi = _t2_pass()
    dd["dd_from_peak"] = -0.10   # shallower than -0.12
    ok, _ = _tranche_conditions_met("T2", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t2_fails_when_fear_greed_too_high() -> None:
    dd, panic, vix_ctx, rsi = _t2_pass()
    panic["fear_greed"] = 30    # > 25
    ok, _ = _tranche_conditions_met("T2", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t2_fails_when_fear_greed_none() -> None:
    dd, panic, vix_ctx, rsi = _t2_pass()
    panic["fear_greed"] = None
    ok, _ = _tranche_conditions_met("T2", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t2_fails_when_hy_oas_too_low() -> None:
    dd, panic, vix_ctx, rsi = _t2_pass()
    panic["hy_oas_bps"] = 400   # < 500
    ok, _ = _tranche_conditions_met("T2", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t2_fails_when_hy_oas_none() -> None:
    dd, panic, vix_ctx, rsi = _t2_pass()
    panic["hy_oas_bps"] = None
    ok, _ = _tranche_conditions_met("T2", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t2_fails_when_vix_too_low() -> None:
    dd, panic, vix_ctx, rsi = _t2_pass()
    vix_ctx["vix"]["level"] = 20.0
    ok, _ = _tranche_conditions_met("T2", dd, panic, vix_ctx, rsi)
    assert ok is False


# ---------------------------------------------------------------------------
# _tranche_conditions_met — T3 (キャピチュレーション反転)
# T3: dd ≤ -0.18 AND VIX ≥ 40 AND (P/C > 1.2 OR VIX > 40) AND RSI-reversed
# ---------------------------------------------------------------------------


def _t3_pass() -> tuple[dict, dict, dict, dict]:
    dd      = {"dd_from_peak": -0.20}
    panic   = {"put_call": None, "vix": 45}      # VIX > 40 satisfies either-or
    vix_ctx = {"vix": {"level": 42.0}}
    rsi     = {"reversed": True, "rsi_latest": 32}
    return dd, panic, vix_ctx, rsi


def test_t3_passes_via_vix_over_40() -> None:
    dd, panic, vix_ctx, rsi = _t3_pass()
    ok, _ = _tranche_conditions_met("T3", dd, panic, vix_ctx, rsi)
    assert ok is True


def test_t3_passes_via_put_call_over_1_2() -> None:
    dd, panic, vix_ctx, rsi = _t3_pass()
    panic["put_call"] = 1.5    # P/C > 1.2 satisfies either-or, even if vix ≤ 40
    panic["vix"] = 38
    ok, _ = _tranche_conditions_met("T3", dd, panic, vix_ctx, rsi)
    assert ok is True


def test_t3_fails_when_dd_not_deep_enough() -> None:
    dd, panic, vix_ctx, rsi = _t3_pass()
    dd["dd_from_peak"] = -0.15   # shallower than -0.18
    ok, _ = _tranche_conditions_met("T3", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t3_fails_when_rsi_not_reversed() -> None:
    dd, panic, vix_ctx, rsi = _t3_pass()
    rsi["reversed"] = False
    ok, _ = _tranche_conditions_met("T3", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t3_fails_when_rsi_key_missing() -> None:
    dd, panic, vix_ctx, _ = _t3_pass()
    rsi = {}   # no "reversed" key → falsy
    ok, _ = _tranche_conditions_met("T3", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t3_fails_when_neither_pc_nor_vix_over_40() -> None:
    dd, panic, vix_ctx, rsi = _t3_pass()
    panic["put_call"] = 1.0    # ≤ 1.2
    panic["vix"] = 38          # ≤ 40
    ok, _ = _tranche_conditions_met("T3", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t3_fails_when_vix_level_below_40() -> None:
    dd, panic, vix_ctx, rsi = _t3_pass()
    vix_ctx["vix"]["level"] = 35.0   # < 40
    ok, _ = _tranche_conditions_met("T3", dd, panic, vix_ctx, rsi)
    assert ok is False


def test_t3_fails_pc_and_vix_both_none() -> None:
    dd, panic, vix_ctx, rsi = _t3_pass()
    panic["put_call"] = None
    panic["vix"] = None
    ok, _ = _tranche_conditions_met("T3", dd, panic, vix_ctx, rsi)
    assert ok is False


# ---------------------------------------------------------------------------
# Reason strings — always a list, always non-empty on evaluation
# ---------------------------------------------------------------------------


def test_reasons_contain_dd_info_on_fail() -> None:
    dd      = {"dd_from_peak": -0.01}
    panic   = {}
    vix_ctx = {"vix": {"level": 30.0, "decay_from_peak_5d_pct": -12.0}}
    rsi     = {}
    ok, reasons = _tranche_conditions_met("T1", dd, panic, vix_ctx, rsi)
    assert ok is False
    assert any("DD" in r for r in reasons)


def test_reasons_contain_vix_info_on_pass() -> None:
    dd, panic, vix_ctx, rsi = _t1_pass()
    ok, reasons = _tranche_conditions_met("T1", dd, panic, vix_ctx, rsi)
    assert ok is True
    assert any("VIX" in r for r in reasons)


# ---------------------------------------------------------------------------
# Codex P1 #10 — recommended clipped to remaining budget + spend capped atomically
# ---------------------------------------------------------------------------

from drawdown_dca_engine import _build_recommended_buys  # noqa: E402
import drawdown_dca_engine as _dca  # noqa: E402


def test_build_recommended_buys_clips_to_deploy_budget() -> None:
    buys = _build_recommended_buys("T1", cash_jpy=10_000_000,
                                   target_tickers=["A", "B", "C"], deploy_jpy=30_000)
    assert len(buys) == 3
    assert sum(b["target_jpy"] for b in buys) == pytest.approx(30_000, abs=3)


def test_build_recommended_buys_zero_deploy_returns_empty() -> None:
    assert _build_recommended_buys("T1", cash_jpy=1_000_000,
                                   target_tickers=["A"], deploy_jpy=0.0) == []


def test_generate_ladder_signals_caps_annual_budget(tmp_path, monkeypatch) -> None:
    """残予算 0.001 でも overspend せず、推奨額・消費とも残予算へ clip される。"""
    import types
    fake_macro = types.ModuleType("macro_fetcher")
    fake_macro.get_macro_context = lambda: {}
    fake_macro.classify_panic = lambda m: {}
    monkeypatch.setitem(sys.modules, "macro_fetcher", fake_macro)
    fake_vix = types.ModuleType("vix_tracker")
    fake_vix.get_vix_context = lambda: {}
    monkeypatch.setitem(sys.modules, "vix_tracker", fake_vix)

    monkeypatch.setattr(_dca, "compute_drawdown_state",
                        lambda *a, **k: {"current_value_jpy": 30_000_000, "current_dd_pct": -0.2})
    monkeypatch.setattr(_dca, "evaluate_sector_breadth",
                        lambda: {"broad_selloff": True, "sectors_below_ma20": 9})
    monkeypatch.setattr(_dca, "check_volume_capitulation", lambda *a, **k: True)
    monkeypatch.setattr(_dca, "evaluate_rsi_reversal", lambda *a, **k: {})
    monkeypatch.setattr(_dca, "_tranche_conditions_met", lambda tid, *a, **k: (tid == "T1", []))
    monkeypatch.setattr(_dca, "STATE_FILE", tmp_path / "dca_state.json")

    _dca._save_state({"last_fired": {}, "annual_spent_pct": _dca.ANNUAL_BUDGET_CAP_PCT - 0.001,
                      "year": date.today().year})

    out = _dca.generate_ladder_signals(cash_jpy=30_000_000, dry_run=False)
    assert out["active_tranche"] == "T1"

    final = _dca._load_state()
    assert final["annual_spent_pct"] <= _dca.ANNUAL_BUDGET_CAP_PCT + 1e-9  # capped, no overspend
    total = sum(b["target_jpy"] for b in out["recommended_buys"])
    assert total <= 0.001 * 30_000_000 + 5  # clipped to remaining budget


def test_compute_drawdown_excludes_estimated_rows(tmp_path) -> None:
    """Codex P1 #4: 推定 (estimated=1) NAV 行は DD 計算に混入しない。"""
    import sqlite3
    from datetime import date, timedelta
    db = tmp_path / "perf.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE daily_performance "
                 "(date TEXT PRIMARY KEY, portfolio_value REAL, estimated INTEGER DEFAULT 0)")
    today = date.today()
    for i in range(6):
        d = (today - timedelta(days=12 - i * 2)).isoformat()
        conn.execute("INSERT INTO daily_performance(date, portfolio_value, estimated) "
                     "VALUES (?,?,0)", (d, 100.0))
    # 推定の暴落行を最新日に置く: 除外されなければ -50% DD になる
    conn.execute("INSERT INTO daily_performance(date, portfolio_value, estimated) "
                 "VALUES (?,?,1)", (today.isoformat(), 50.0))
    conn.commit()
    conn.close()
    state = _dca.compute_drawdown_state(db_file=db, lookback_days=60)
    assert state["current_value_jpy"] == 100   # 最新の *実* 行 (推定は除外)
    assert state["dd_from_peak"] == 0.0         # 推定暴落の -50% は混入しない

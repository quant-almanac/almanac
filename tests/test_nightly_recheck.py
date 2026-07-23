"""Tests for nightly_recheck.check_delta() — pure logic, no network calls.

check_delta() compares the previous ai_portfolio_analysis.json market snapshot
with current market data and returns (should_reanalyze, triggers, deltas).

Coverage:
  - No prev data → no comparison possible → no triggers → should=False
  - Each individual market metric:  VIX, SPY, QQQ, USDJPY, yield10y
      · above threshold → trigger fires
      · below threshold → trigger silent
      · exactly at threshold → trigger fires (>=)
  - yield10y as nested dict {'value': x} → correctly extracted
  - prev_mm from 'market_meta' fallback path
  - None current price → metric skipped entirely
  - Multiple triggers → any() → should=True
  - All metrics below threshold → should=False
  - THRESH constants match documented values
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import nightly_recheck as nr  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_cache(path: Path, market_meta: dict) -> None:
    """Write a minimal ai_portfolio_analysis.json with the given market snapshot."""
    data = {"synthesis": {"market_meta_snapshot": market_meta}}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _base_snapshot() -> dict:
    return {
        "vix":        15.0,
        "spy_price":  500.0,
        "qqq_price":  420.0,
        "usdjpy":     150.0,
        "us10y_yield": 4.20,
    }


# Default "current" values that don't trigger any alert
def _noop_macro() -> dict:
    return {"vix": 15.0, "us10y_yield": 4.20}


def _make_price_fn(spy: float = 500.0, qqq: float = 420.0,
                   usdjpy: float = 150.0) -> "Callable[[str], float]":
    mapping = {"SPY": spy, "QQQ": qqq, "JPY=X": usdjpy}
    return lambda t: mapping.get(t)


# ---------------------------------------------------------------------------
# THRESH constants
# ---------------------------------------------------------------------------


def test_thresh_vix_delta_is_3() -> None:
    assert nr.THRESH["vix_delta"] == 3.0


def test_thresh_spy_pct_is_2() -> None:
    assert nr.THRESH["spy_pct"] == 2.0


def test_thresh_qqq_pct_is_2_5() -> None:
    assert nr.THRESH["qqq_pct"] == 2.5


def test_thresh_usdjpy_delta_is_1() -> None:
    assert nr.THRESH["usdjpy_delta"] == 1.0


def test_thresh_yield_delta_is_0_2() -> None:
    assert nr.THRESH["yield_delta"] == 0.2


# ---------------------------------------------------------------------------
# No previous data
# ---------------------------------------------------------------------------


def test_no_prev_data_returns_no_triggers(monkeypatch, tmp_path) -> None:
    """Missing or empty cache → nothing to compare → no triggers."""
    cache = tmp_path / "empty.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {})
    monkeypatch.setattr(nr, "_get_live_price", lambda t: None)

    should, triggers, deltas = nr.check_delta()
    assert should is False
    assert triggers == {}
    assert deltas == {}


def test_missing_cache_file_no_crash(monkeypatch, tmp_path) -> None:
    """CACHE_PATH does not exist → load_json returns {} → no crash."""
    monkeypatch.setattr(nr, "CACHE_PATH", tmp_path / "nonexistent.json")
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {})
    monkeypatch.setattr(nr, "_get_live_price", lambda t: None)

    should, triggers, deltas = nr.check_delta()
    assert should is False


# ---------------------------------------------------------------------------
# VIX
# ---------------------------------------------------------------------------


def test_vix_spike_above_threshold_triggers(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {"vix": 20.0, "us10y_yield": 4.20})
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn())

    should, triggers, deltas = nr.check_delta()
    assert triggers.get("vix_spike") is True
    assert deltas["vix"] == pytest.approx(5.0)
    assert should is True


def test_vix_below_threshold_no_trigger(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {"vix": 16.0, "us10y_yield": 4.20})
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn())

    _, triggers, deltas = nr.check_delta()
    assert triggers.get("vix_spike") is False
    assert deltas["vix"] == pytest.approx(1.0)


def test_vix_exactly_at_threshold_triggers(monkeypatch, tmp_path) -> None:
    """VIX delta == THRESH['vix_delta'] → triggers (>= condition)."""
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())          # prev vix=15
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {"vix": 18.0, "us10y_yield": 4.20})  # delta=3.0
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn())

    _, triggers, _ = nr.check_delta()
    assert triggers.get("vix_spike") is True


def test_vix_none_current_skips_comparison(monkeypatch, tmp_path) -> None:
    """cur vix=None → no VIX trigger entry."""
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {"vix": None, "us10y_yield": 4.20})
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn())

    _, triggers, deltas = nr.check_delta()
    assert "vix_spike" not in triggers
    assert "vix" not in deltas


# ---------------------------------------------------------------------------
# SPY
# ---------------------------------------------------------------------------


def test_spy_move_above_threshold_triggers(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())          # prev spy=500
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", _noop_macro)
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn(spy=512.0))  # +2.4%

    _, triggers, deltas = nr.check_delta()
    assert triggers.get("spy_move") is True
    assert deltas["spy_pct"] == pytest.approx(2.4, abs=0.01)


def test_spy_move_below_threshold_no_trigger(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", _noop_macro)
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn(spy=505.0))  # +1%

    _, triggers, _ = nr.check_delta()
    assert triggers.get("spy_move") is False


# ---------------------------------------------------------------------------
# QQQ
# ---------------------------------------------------------------------------


def test_qqq_move_above_threshold_triggers(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())          # prev qqq=420
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", _noop_macro)
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn(qqq=431.0))  # +2.62%

    _, triggers, deltas = nr.check_delta()
    assert triggers.get("qqq_move") is True
    assert deltas["qqq_pct"] > 2.5


def test_qqq_move_below_threshold_no_trigger(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", _noop_macro)
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn(qqq=425.0))  # +1.19%

    _, triggers, _ = nr.check_delta()
    assert triggers.get("qqq_move") is False


# ---------------------------------------------------------------------------
# USDJPY
# ---------------------------------------------------------------------------


def test_fx_move_above_threshold_triggers(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())          # prev usdjpy=150.0
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", _noop_macro)
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn(usdjpy=151.5))  # delta=1.5

    _, triggers, deltas = nr.check_delta()
    assert triggers.get("fx_move") is True
    assert deltas["usdjpy"] == pytest.approx(1.5)


def test_fx_move_below_threshold_no_trigger(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", _noop_macro)
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn(usdjpy=150.5))  # delta=0.5

    _, triggers, _ = nr.check_delta()
    assert triggers.get("fx_move") is False


# ---------------------------------------------------------------------------
# yield10y
# ---------------------------------------------------------------------------


def test_yield_shift_above_threshold_triggers(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())          # prev yield=4.20
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {"vix": 15.0, "us10y_yield": 4.45})
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn())

    _, triggers, deltas = nr.check_delta()
    assert triggers.get("yield_shift") is True
    assert deltas["yield"] == pytest.approx(0.25, abs=0.001)


def test_yield_shift_below_threshold_no_trigger(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {"vix": 15.0, "us10y_yield": 4.30})
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn())

    _, triggers, _ = nr.check_delta()
    assert triggers.get("yield_shift") is False


def test_yield_as_dict_with_value_key(monkeypatch, tmp_path) -> None:
    """cur_macro['us10y_yield'] = {'value': 4.5} → extracted correctly."""
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    # Nested dict form: some macro_fetcher versions return this structure
    monkeypatch.setattr(nr, "_get_current_macro",
                        lambda: {"vix": 15.0, "us10y_yield": {"value": 4.50}})
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn())

    _, triggers, deltas = nr.check_delta()
    assert "yield" in deltas
    assert deltas["yield"] == pytest.approx(0.30, abs=0.001)   # |4.50 - 4.20|


# ---------------------------------------------------------------------------
# Fallback: prev_mm from market_meta (not synthesis.market_meta_snapshot)
# ---------------------------------------------------------------------------


def test_prev_mm_reads_from_market_meta_fallback(monkeypatch, tmp_path) -> None:
    """Cache without 'synthesis' key → reads from top-level 'market_meta'."""
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({
        "market_meta": {
            "vix": 15.0, "spy_price": 500.0, "qqq_price": 420.0,
            "usdjpy": 150.0, "us10y_yield": 4.20,
        }
    }), encoding="utf-8")
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {"vix": 20.0, "us10y_yield": 4.20})
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn())

    _, triggers, _ = nr.check_delta()
    assert triggers.get("vix_spike") is True  # delta=5 ≥ 3


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def test_all_below_threshold_should_is_false(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", _noop_macro)      # vix=15, yield=4.20
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn())    # spy=500, qqq=420, fx=150

    should, triggers, _ = nr.check_delta()
    assert should is False
    assert not any(triggers.values())


def test_multiple_triggers_should_is_true(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    _write_cache(cache, _base_snapshot())
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {"vix": 22.0, "us10y_yield": 4.60})
    monkeypatch.setattr(nr, "_get_live_price", _make_price_fn(spy=515.0, qqq=432.0, usdjpy=152.0))

    should, triggers, _ = nr.check_delta()
    assert should is True
    fired = [k for k, v in triggers.items() if v]
    assert len(fired) >= 2


def test_check_delta_returns_three_tuple(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(nr, "CACHE_PATH", cache)
    monkeypatch.setattr(nr, "_get_current_macro", lambda: {})
    monkeypatch.setattr(nr, "_get_live_price", lambda t: None)

    result = nr.check_delta()
    assert isinstance(result, tuple) and len(result) == 3
    should, triggers, deltas = result
    assert isinstance(should, bool)
    assert isinstance(triggers, dict)
    assert isinstance(deltas, dict)

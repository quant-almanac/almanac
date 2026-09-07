"""Part E-6: earnings_proximity_manager のビジネス日計算 & format 出力。"""
from __future__ import annotations

import importlib
import json
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace

import pytest


def test_business_days_future():
    m = importlib.import_module("earnings_proximity_manager")
    # 来週の同曜日 → 5 営業日
    tgt = date.today() + timedelta(days=7)
    n = m._business_days_until(tgt)
    assert 4 <= n <= 6


def test_business_days_past():
    m = importlib.import_module("earnings_proximity_manager")
    n = m._business_days_until(date.today() - timedelta(days=1))
    assert n == -1


def test_thresholds():
    m = importlib.import_module("earnings_proximity_manager")
    assert 0 < m.DAMAGE_PCT_THRESHOLD < 0.1
    assert 0 < m.IMPL_MOVE_FUDGE < 1.0
    assert 0 < m.BEAT_RATE_FORCE_TRIM <= 1.0


def test_scheduled_scan_rechecks_current_snapshot_inside_shared_lock(monkeypatch):
    m = importlib.import_module("earnings_proximity_manager")
    import utils

    lock_state = {"held": False}
    expected = {"schema_version": m.OUTPUT_SCHEMA_VERSION, "suggestions": []}

    @contextmanager
    def fake_lock(name, *, timeout):
        assert name == "earnings_proximity"
        assert timeout == 300.0
        lock_state["held"] = True
        try:
            yield
        finally:
            lock_state["held"] = False

    def load_current():
        assert lock_state["held"] is True
        return expected

    monkeypatch.setattr(utils, "process_lock", fake_lock)
    monkeypatch.setattr(m, "_load_current_snapshot", load_current)
    monkeypatch.setattr(
        m,
        "_scan_once",
        lambda **_kwargs: pytest.fail("duplicate network scan must be skipped"),
    )

    assert m.scan(dry_run=False, reuse_current=True) is expected
    assert lock_state["held"] is False


def test_scan_never_falls_back_to_non_atomic_output_write(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    import utils

    output = tmp_path / "earnings_hedge_suggestions.json"
    output.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "_load_holdings", lambda: [])
    monkeypatch.setattr(m, "_total_portfolio_jpy", lambda: 30_000_000.0)
    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(
            Ticker=lambda _ticker: SimpleNamespace(
                fast_info=SimpleNamespace(last_price=150.0),
            ),
        ),
    )

    def fail_atomic_write(_path, _payload):
        raise OSError("simulated atomic persistence failure")

    monkeypatch.setattr(utils, "atomic_write_json", fail_atomic_write)

    with pytest.raises(OSError, match="atomic persistence failure"):
        m._scan_once(dry_run=False)
    assert output.read_text(encoding="utf-8") == "sentinel"


def _current_snapshot(m, holdings, *, result_rows=None):
    rows = result_rows if result_rows is not None else [
        {"ticker": row["ticker"], "reason": "no_earnings_date"}
        for row in holdings
    ]
    return {
        "schema_version": m.OUTPUT_SCHEMA_VERSION,
        "generated_at": f"{date.today().isoformat()} 06:15:00",
        "holdings_scanned": len(holdings),
        "holdings_snapshot_sha256": m._holdings_snapshot_sha256(holdings),
        "suggestions": [],
        "skipped": rows,
    }


def test_same_day_snapshot_is_invalidated_by_ticker_or_share_change(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    overrides = tmp_path / "earnings_calendar_overrides.json"
    overrides.write_text('{"overrides": {}}', encoding="utf-8")
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "EARNINGS_OVERRIDES", overrides)

    holdings = [{"ticker": "SYNTH_A", "shares": 10.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    output.write_text(json.dumps(_current_snapshot(m, holdings)), encoding="utf-8")
    assert m.snapshot_is_current() is True

    # Same count is not enough: both a replacement holding and a sizing change
    # require a fresh earnings/damage calculation.
    monkeypatch.setattr(
        m, "_load_holdings",
        lambda: [{"ticker": "SYNTH_B", "shares": 10.0, "currency": "USD"}],
    )
    assert m.snapshot_is_current() is False
    monkeypatch.setattr(
        m, "_load_holdings",
        lambda: [{"ticker": "SYNTH_A", "shares": 11.0, "currency": "USD"}],
    )
    assert m.snapshot_is_current() is False


def test_snapshot_rejects_missing_or_duplicate_holding_result_rows(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    overrides = tmp_path / "earnings_calendar_overrides.json"
    overrides.write_text('{"overrides": {}}', encoding="utf-8")
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "EARNINGS_OVERRIDES", overrides)
    holdings = [
        {"ticker": "SYNTH_A", "shares": 10.0, "currency": "USD"},
        {"ticker": "SYNTH_B", "shares": 5.0, "currency": "USD"},
    ]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)

    missing = _current_snapshot(
        m, holdings,
        result_rows=[{"ticker": "SYNTH_A", "reason": "no_earnings_date"}],
    )
    output.write_text(json.dumps(missing), encoding="utf-8")
    assert m.snapshot_is_current() is False

    duplicate = _current_snapshot(
        m, holdings,
        result_rows=[
            {"ticker": "SYNTH_A", "reason": "no_earnings_date"},
            {"ticker": "SYNTH_A", "reason": "no_option_chain"},
        ],
    )
    output.write_text(json.dumps(duplicate), encoding="utf-8")
    assert m.snapshot_is_current() is False


def test_scan_persists_exact_holdings_fingerprint(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    holdings = [
        {"ticker": "SYNTH_A", "shares": 10.0, "currency": "USD"},
        {"ticker": "SYNTH_B", "shares": 5.0, "currency": "USD"},
    ]
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    monkeypatch.setattr(m, "_total_portfolio_jpy", lambda: 30_000_000.0)
    monkeypatch.setattr(m, "_next_earnings_with_source", lambda _ticker: None)
    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(
            Ticker=lambda _ticker: SimpleNamespace(
                fast_info=SimpleNamespace(last_price=150.0),
            ),
        ),
    )

    out = m._scan_once(dry_run=False)

    assert out["holdings_scanned"] == 2
    assert out["holdings_snapshot_sha256"] == m._holdings_snapshot_sha256(holdings)
    assert {row["ticker"] for row in out["skipped"]} == {"SYNTH_A", "SYNTH_B"}


def test_prompt_suppresses_snapshot_after_holdings_change(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    output.write_text(json.dumps({
        "suggestions": [{
            "ticker": "SYNTH_A",
            "business_days": 2,
            "earnings_date": date.today().isoformat(),
            "implied_move_pct": 5.0,
            "damage_pct": 2.0,
            "recommended_action": "trim_50pct",
        }],
        "skipped": [],
    }), encoding="utf-8")
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "snapshot_is_current", lambda: False)

    assert m.format_for_prompt() == ""


def test_corrupt_holdings_never_publish_an_empty_current_snapshot(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    holdings = tmp_path / "holdings.json"
    output = tmp_path / "earnings_hedge_suggestions.json"
    holdings.write_text("{not-json", encoding="utf-8")
    output.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(m, "HOLDINGS", holdings)
    monkeypatch.setattr(m, "OUTPUT", output)

    with pytest.raises(ValueError, match="holdings source is unreadable"):
        m._scan_once(dry_run=False)

    assert output.read_text(encoding="utf-8") == "sentinel"

"""Part E-6: earnings_proximity_manager のビジネス日計算 & format 出力。"""
from __future__ import annotations

import importlib
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

from datetime import date, datetime, timezone
import json

import technical_signals as ts
from analyst import _ensure_technical_state_fresh


def test_session_lag_and_freshness_contract_without_wall_clock():
    assert ts._session_lag("XLF", date(2026, 7, 13), expected=date(2026, 7, 13)) == 0
    assert ts._session_lag("XLF", date(2026, 7, 10), expected=date(2026, 7, 13)) == 1
    assert ts._session_lag("XLF", date(2026, 7, 9), expected=date(2026, 7, 13)) == 2
    assert ts._freshness_status(0) == "fresh"
    assert ts._freshness_status(1) == "degraded"
    assert ts._freshness_status(2) == "stale"


def test_force_refresh_bypasses_fresh_wrapper_cache(monkeypatch, tmp_path):
    cached = {"cached_at": "2999-01-01T00:00:00+00:00", "tickers": {"OLD": {}}}
    computed = {"cached_at": "2026-07-14T00:00:00+00:00", "tickers": {"NEW": {}}}
    calls = []
    monkeypatch.setattr(ts, "CACHE_FILE", tmp_path / "technical_state.json")
    monkeypatch.setattr(ts, "load_json", lambda path, default: cached)
    monkeypatch.setattr(ts, "compute_technical_state", lambda: calls.append("compute") or computed)
    monkeypatch.setattr(ts, "atomic_write_json", lambda path, value: calls.append((path, value)))

    assert ts.get_technical_context() is cached
    assert calls == []
    assert ts.get_technical_context(force=True) is computed
    assert calls[0] == "compute"


def test_screener_candidates_are_included_in_technical_universe(monkeypatch, tmp_path):
    (tmp_path / "holdings.json").write_text("{}", encoding="utf-8")
    (tmp_path / "scenario_playbook.json").write_text("{}", encoding="utf-8")
    (tmp_path / "margin_long_candidates.json").write_text(json.dumps({
        "candidates": [{"ticker": "MDB"}, {"ticker": "RCL"}],
    }), encoding="utf-8")
    (tmp_path / "short_candidates.json").write_text(json.dumps({
        "candidates": [{"ticker": "CVNA"}],
    }), encoding="utf-8")
    monkeypatch.setattr(ts, "BASE_DIR", tmp_path)

    universe = ts._build_ticker_universe()

    assert {"MDB", "RCL", "CVNA"} <= set(universe)


def test_analysis_refreshes_when_current_universe_outgrows_cache(monkeypatch, tmp_path):
    path = tmp_path / "technical_state.json"
    path.write_text(json.dumps({
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "tickers": {"SPY": {}},
        "source_health": {"max_lag_sessions": 0, "missing_count": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(ts, "_build_ticker_universe", lambda: ["SPY", "MDB"])
    calls = []
    assert _ensure_technical_state_fresh(
        base_dir=tmp_path,
        max_age_hours=4,
        refresher=lambda: calls.append("refresh"),
    ) is True
    assert calls == ["refresh"]


def test_analysis_refreshes_legacy_cache_without_quality_schema(monkeypatch, tmp_path):
    path = tmp_path / "technical_state.json"
    path.write_text(json.dumps({
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "tickers": {"SPY": {}},
        "source_health": {"max_lag_sessions": 0, "missing_count": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(ts, "_build_ticker_universe", lambda: ["SPY"])
    calls = []

    assert _ensure_technical_state_fresh(
        base_dir=tmp_path,
        max_age_hours=4,
        refresher=lambda: calls.append("refresh"),
    ) is True
    assert calls == ["refresh"]


def test_analysis_reuses_fresh_complete_cache_with_quality_schema(monkeypatch, tmp_path):
    path = tmp_path / "technical_state.json"
    path.write_text(json.dumps({
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "tickers": {"SPY": {"data_quality_status": "ok"}},
        "source_health": {
            "max_lag_sessions": 0,
            "missing_count": 0,
            "data_quality_counts": {"ok": 1},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(ts, "_build_ticker_universe", lambda: ["SPY"])
    calls = []

    assert _ensure_technical_state_fresh(
        base_dir=tmp_path,
        max_age_hours=4,
        refresher=lambda: calls.append("refresh"),
    ) is False
    assert calls == []

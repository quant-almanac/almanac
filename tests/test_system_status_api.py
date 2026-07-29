import asyncio

import auto_tune
from api.routes import dashboard, system_status


def test_system_status_uses_live_sources(monkeypatch):
    monkeypatch.setattr(dashboard, "_build_data_health", lambda: {"ok": True, "sources": {}})
    monkeypatch.setattr(auto_tune, "get_status", lambda: {
        "mode": "apply", "schedule": {"times": ["06:30"]}, "audit": {"status": "ok"}
    })
    monkeypatch.setattr(system_status, "load_json", lambda *args, **kwargs: {})
    monkeypatch.setattr(system_status, "_heartbeat_rows", lambda heartbeats: [])
    result = asyncio.run(system_status.get_system_status())
    assert result["data_health"]["ok"] is True
    assert result["feature_modes"]["auto_tune"] == "apply"
    assert result["schedules"]["auto_tune"]["times"] == ["06:30"]
    assert result["heartbeat_statuses"] == []
    assert any(row["role"] == "final_synthesis" for row in result["model_routes"])


def test_heartbeat_rows_separates_monitored_freshness_and_raw_status():
    rows = system_status._heartbeat_rows(
        {
            "portfolio_analyst": {
                "status": "ok",
                "last_run_ts": 1,
                "last_run_iso": "2026-07-29T06:00:00+09:00",
            },
            "unmonitored_job": {
                "status": "warn",
                "last_run_iso": "2026-07-29T07:00:00+09:00",
            },
        },
        health={
            "ok": [],
            "stale": [{
                "script": "portfolio_analyst",
                "age_hours": 30,
                "reason": "older_than_26h",
            }],
            "errors": [],
        },
    )
    by_key = {row["key"]: row for row in rows}

    assert by_key["portfolio_analyst"]["monitored"] is True
    assert by_key["portfolio_analyst"]["freshness_status"] == "stale"
    assert by_key["portfolio_analyst"]["max_age_hours"] == 26
    assert by_key["unmonitored_job"]["monitored"] is False
    assert by_key["unmonitored_job"]["freshness_status"] == "warning"
    assert by_key["unmonitored_job"]["error"] == "raw_status_warn"

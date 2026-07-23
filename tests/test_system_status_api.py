import asyncio

import auto_tune
from api.routes import dashboard, system_status


def test_system_status_uses_live_sources(monkeypatch):
    monkeypatch.setattr(dashboard, "_build_data_health", lambda: {"ok": True, "sources": {}})
    monkeypatch.setattr(auto_tune, "get_status", lambda: {
        "mode": "apply", "schedule": {"times": ["06:30"]}, "audit": {"status": "ok"}
    })
    monkeypatch.setattr(system_status, "load_json", lambda *args, **kwargs: {})
    result = asyncio.run(system_status.get_system_status())
    assert result["data_health"]["ok"] is True
    assert result["feature_modes"]["auto_tune"] == "apply"
    assert result["schedules"]["auto_tune"]["times"] == ["06:30"]
    assert any(row["role"] == "final_synthesis" for row in result["model_routes"])

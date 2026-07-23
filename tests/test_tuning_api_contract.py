import asyncio

import pytest
from fastapi import HTTPException

import auto_tune
from api.routes import tuning


def test_apply_mode_requires_explicit_confirmation():
    request = tuning.AutoModeRequest(mode="apply", confirm=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(tuning.update_auto_mode(request))
    assert exc.value.status_code == 400


def test_auto_tune_now_is_always_dry_run(monkeypatch):
    called = {}

    def fake_run(*, dry_run, force):
        called.update(dry_run=dry_run, force=force)
        return {"status": "dry_run"}

    monkeypatch.setattr(auto_tune, "run", fake_run)
    result = asyncio.run(tuning.trigger_auto_tune_now(force=True))
    assert result["status"] == "dry_run"
    assert called == {"dry_run": True, "force": True}


def test_legacy_bulk_apply_is_gone():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(tuning.apply_all_ai())
    assert exc.value.status_code == 410

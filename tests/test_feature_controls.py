from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

import feature_controls as fc


def _write_us_source(root, *, tickers: dict | None = None) -> None:
    path = root / "data" / "broker_short_us.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": tickers or {
            "AAPL": {
                "rakuten": True,
                "sbi": None,
                "borrow_cost_annual_pct": 0.02,
                "confirm_at_order": True,
            },
        },
    }), encoding="utf-8")


def test_us_short_defaults_off_and_jp_short_defaults_on(tmp_path):
    configured = fc.configured_short_features(base_dir=tmp_path)
    assert configured == {"us_short": False, "jp_short": True}


def test_us_short_toggle_is_single_runtime_authority(tmp_path):
    _write_us_source(tmp_path)

    enabled = fc.set_feature(
        "us_short",
        True,
        actor="test",
        rationale="account supports US margin short",
        base_dir=tmp_path,
    )

    assert enabled["configured_enabled"] is True
    assert enabled["effective_enabled"] is True
    assert enabled["mode"] == "human_execution_only"
    assert enabled["auto_order_enabled"] is False
    assert enabled["eligible_instruments"] == 1
    state = json.loads((tmp_path / "feature_control_state.json").read_text())
    assert state["features"]["us_short"]["enabled"] is True
    assert state["history"][-1]["new_enabled"] is True


def test_enabled_switch_still_fails_closed_without_borrow_source(tmp_path):
    status = fc.set_feature("us_short", True, actor="test", base_dir=tmp_path)

    assert status["configured_enabled"] is True
    assert status["effective_enabled"] is False
    assert any("借株可否データがありません" in reason for reason in status["blockers"])


def test_disclosure_config_loader_applies_runtime_state(tmp_path, monkeypatch):
    from disclosure_shadow_book import load_config

    monkeypatch.setenv("ALMANAC_STATE_DIR", str(tmp_path))
    fc.set_feature("us_short", True, actor="test", base_dir=tmp_path)

    config = load_config()
    assert config["us_short_enabled"] is True
    assert config["jp_short_enabled"] is True


def test_shortability_gate_reconciles_tradeability_metadata():
    from short_universe import apply_shortability_gate

    candidate = {
        "ticker": "AAPL",
        "tradeability": {
            "untradeable": True,
            "reasons": ["short_universe_verification_required"],
        },
    }
    universe = {
        "tickers": {
            "AAPL": {
                "shortable": True,
                "squeeze_guard_status": "ok",
                "reasons": [],
                "borrow_cost_annual_pct": 0.02,
                "cost_model": {"borrow_cost_annual_pct": 0.02},
            },
        },
    }

    result = apply_shortability_gate(candidate, universe)

    assert result["shortable"] is True
    assert result["tradeability"]["untradeable"] is False
    assert result["tradeability"]["reasons"] == []
    assert result["human_execution_only"] is True
    assert result["executable"] is False


def test_feature_api_rejects_read_only_feature():
    from api.routes.features import FeatureUpdateRequest, update_feature
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_feature("ginn", FeatureUpdateRequest(enabled=True)))
    assert exc_info.value.status_code == 409

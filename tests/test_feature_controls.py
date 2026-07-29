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


def test_us_short_status_separates_proxy_and_latest_funnel(tmp_path):
    _write_us_source(tmp_path, tickers={
        "AAPL": {"rakuten": True},
        "MSFT": {"rakuten": True},
    })
    (tmp_path / "tickers.json").write_text(json.dumps({
        "all": ["AAPL", "MSFT", "NVDA", "7203.T"],
    }), encoding="utf-8")
    (tmp_path / "short_candidates.json").write_text(json.dumps({
        "as_of": "2026-07-29 18:30",
        "data_quality": "degraded",
        "universe_requested_us": 3,
        "price_data_us": 2,
        "candidate_count_us": 1,
        "shortable_count_us": 1,
        "candidates": [{"ticker": "AAPL", "shortable": True}],
    }), encoding="utf-8")

    status = fc.set_feature("us_short", True, actor="test", base_dir=tmp_path)

    assert status["eligible_instruments"] == 2
    assert status["availability_universe_instruments"] == 3
    assert status["availability_coverage_pct"] == 66.7
    assert status["availability_label"] == "借株proxy該当"
    assert status["availability_metric_kind"] == "proxy_eligibility_rate"
    assert status["latest_scan_requested"] == 3
    assert status["latest_scan_downloaded"] == 2
    assert status["latest_candidates"] == 1
    assert status["latest_shortable"] == 1
    assert status["warnings"] == ["最新の価格取得率が66.7%です"]


def test_feature_inventory_exposes_major_modes_and_only_shorts_are_mutable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("ALMANAC_PRIVACY_MODE", raising=False)
    payload = fc.list_feature_statuses(base_dir=tmp_path)
    statuses = {row["key"]: row for row in payload["features"]}

    assert list(statuses) == [
        "us_short",
        "jp_short",
        "margin_long",
        "options_signals",
        "market_regime_v2",
        "ginn",
        "analysis_snapshot",
        "broker_reconciliation",
        "tax_basis",
        "privacy_mode",
        "kelly_shadow",
        "fx_hedge_shadow",
        "currency_policy",
        "execution_plan",
        "auto_tune",
    ]
    assert {key for key, row in statuses.items() if row["mutable"]} == {
        "us_short",
        "jp_short",
    }
    assert all("source" in row for row in statuses.values())
    assert all("freshness_status" in row for row in statuses.values())
    assert statuses["tax_basis"]["control_hint"].startswith("環境変数")
    assert statuses["privacy_mode"]["mode"] == "strict_local"


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

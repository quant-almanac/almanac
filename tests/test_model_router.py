"""Part C: model_router が role → model_id を想定通りに解決する。"""
from __future__ import annotations

import pytest


def test_critical_roles_route_to_opus():
    import model_router as mr
    for role in ("final_synthesis", "dca_tranche_selector"):
        mid = mr.get_model(role)
        assert "opus" in mid.lower(), f"{role} should map to Opus, got {mid}"


def test_sonnet_tier_roles_are_three_core_views():
    import model_router as mr
    for role in ("tier_analysis_long", "tier_analysis_medium", "tier_analysis_short"):
        mid = mr.get_model(role)
        assert "sonnet" in mid.lower(), f"{role} should map to Sonnet, got {mid}"


def test_margin_and_shortsell_route_to_deepseek_v4_pro():
    import model_router as mr
    for role in ("tier_analysis_margin_long", "tier_analysis_shortsell"):
        mid = mr.get_model(role)
        assert mid == "deepseek-v4-pro", f"{role} should map to DeepSeek V4 Pro, got {mid}"
        assert mr.resolve_adapter(role) == "deepseek"


def test_red_team_uses_diverse_models():
    import model_router as mr
    red = [mr.get_model(f"red_team_{i}") for i in (1, 2, 3)]
    assert len(set(red)) >= 2, f"red team should use >=2 distinct models: {red}"


def test_downgraded_roles_not_opus():
    import model_router as mr
    for role in ("screener_deepdive", "decision_support", "chat"):
        mid = mr.get_model(role)
        assert "opus" not in mid.lower(), f"{role} should not use Opus"

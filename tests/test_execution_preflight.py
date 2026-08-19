"""v7 deterministic preflight contracts (no portfolio state mutation)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import execution_preflight as ep
from risk_policy import POLICY, classify_execution_risk, concentration_limits


def test_minus_point_one_daily_is_normal_when_all_metrics_are_known():
    decision = classify_execution_risk(
        daily_pnl_decimal=-0.001,
        rolling_30_pnl_decimal=-0.002,
        var_1d_95_decimal=0.010,
        concentration_decimal=0.04,
        canonical_drawdown_decimal=-0.02,
        risk_increasing=True,
    )
    assert decision["disposition"] == "ready"


def test_minus_three_point_zero_one_requires_human_confirmation():
    decision = classify_execution_risk(
        daily_pnl_decimal=-0.0301,
        rolling_30_pnl_decimal=-0.002,
        var_1d_95_decimal=0.010,
        concentration_decimal=0.04,
        canonical_drawdown_decimal=-0.02,
        risk_increasing=True,
    )
    assert decision["disposition"] == "confirmation_required"
    assert {r["code"] for r in decision["reasons"]} == {"daily_loss_block"}


def test_missing_loss_guard_metrics_never_become_a_healthy_zero():
    decision = classify_execution_risk(
        daily_pnl_decimal=None,
        rolling_30_pnl_decimal=None,
        var_1d_95_decimal=0.010,
        concentration_decimal=0.04,
        canonical_drawdown_decimal=-0.02,
        risk_increasing=True,
    )
    assert decision["disposition"] == "confirmation_required"
    assert "loss_guard_data_unavailable" in {r["code"] for r in decision["reasons"]}


def test_normal_var_budget_is_one_point_four_not_bull_budget():
    decision = classify_execution_risk(
        daily_pnl_decimal=0.0,
        rolling_30_pnl_decimal=0.0,
        var_1d_95_decimal=0.015,
        var_policy_threshold_decimal=POLICY.var_normal_decimal,
        concentration_decimal=0.04,
        canonical_drawdown_decimal=-0.02,
        risk_increasing=True,
    )
    assert decision["disposition"] == "confirmation_required"
    assert "var_policy_threshold" in {r["code"] for r in decision["reasons"]}


def test_bull_var_budget_allows_one_point_five_below_one_point_six():
    decision = classify_execution_risk(
        daily_pnl_decimal=0.0,
        rolling_30_pnl_decimal=0.0,
        var_1d_95_decimal=0.015,
        var_policy_threshold_decimal=POLICY.var_bull_decimal,
        concentration_decimal=0.04,
        canonical_drawdown_decimal=-0.02,
        risk_increasing=True,
    )
    assert decision["disposition"] == "ready"


def test_promoted_minus_eight_point_one_dd_requires_human_confirmation():
    decision = classify_execution_risk(
        daily_pnl_decimal=-0.001, rolling_30_pnl_decimal=-0.002,
        var_1d_95_decimal=0.010, concentration_decimal=0.04,
        canonical_drawdown_decimal=-0.081, canonical_drawdown_stage="block",
        risk_increasing=True,
    )
    assert decision["disposition"] == "confirmation_required"
    assert {r["code"] for r in decision["reasons"]} == {"drawdown_block"}


@pytest.mark.parametrize(
    "var, concentration, code",
    [
        (POLICY.var_absolute_max_decimal, 0.04, "var_absolute_cap"),
        (0.01, POLICY.concentration_cap_decimal, "concentration_cap"),
    ],
)
def test_absolute_caps_are_hard_rejections(var, concentration, code):
    decision = classify_execution_risk(
        daily_pnl_decimal=0.0, rolling_30_pnl_decimal=0.0,
        var_1d_95_decimal=var, concentration_decimal=concentration,
        canonical_drawdown_decimal=-0.02, risk_increasing=True,
    )
    assert decision["disposition"] == "hard_reject"
    assert {r["code"] for r in decision["hard_reasons"]} == {code}


def test_short_position_cap_is_hard_and_cover_is_never_blocked():
    short = classify_execution_risk(
        daily_pnl_decimal=0.0,
        rolling_30_pnl_decimal=0.0,
        var_1d_95_decimal=0.01,
        concentration_decimal=0.02,
        canonical_drawdown_decimal=-0.01,
        risk_increasing=True,
        action_direction="short",
        current_short_positions=POLICY.max_short_positions,
    )
    cover = classify_execution_risk(
        daily_pnl_decimal=-0.20,
        rolling_30_pnl_decimal=-0.20,
        var_1d_95_decimal=0.10,
        concentration_decimal=0.50,
        canonical_drawdown_decimal=-0.20,
        risk_increasing=False,
        action_direction="cover",
        current_short_positions=POLICY.max_short_positions,
    )

    assert short["disposition"] == "hard_reject"
    assert "max_short_positions" in {row["code"] for row in short["hard_reasons"]}
    assert cover["disposition"] == "ready"


def test_sells_are_always_ready_even_in_a_freeze():
    decision = classify_execution_risk(
        daily_pnl_decimal=-0.2, rolling_30_pnl_decimal=-0.2,
        var_1d_95_decimal=0.04, concentration_decimal=0.2,
        canonical_drawdown_decimal=-0.2, risk_increasing=False,
    )
    assert decision["disposition"] == "ready"


def test_token_binds_action_identity_and_expires(monkeypatch):
    monkeypatch.setattr(ep, "load_api_key", lambda: "test-preflight-key")
    original = {"ticker": "NVDA", "direction": "buy", "quantity": 1, "price": 100,
                "currency": "USD", "account": "特定", "execution_position_keys": []}
    digest = ep.action_digest(original)
    context = {"reason_codes": ["daily_loss_block"], "metrics": {"daily_pnl_decimal": -0.031}}
    token, _ = ep.issue_preflight_token(
        digest=digest, disposition="ready", review_context=context,
    )
    claims = ep.validate_preflight_token(token, digest=digest)
    assert claims["disposition"] == "ready"
    assert claims["review_context"] == context
    changed = dict(original, quantity=2)
    with pytest.raises(ValueError, match="identity"):
        ep.validate_preflight_token(token, digest=ep.action_digest(changed))
    past = datetime.now(timezone.utc) - timedelta(minutes=61)
    expired, _ = ep.issue_preflight_token(digest=digest, disposition="ready", now=past)
    with pytest.raises(ValueError, match="expired"):
        ep.validate_preflight_token(expired, digest=digest)


def test_order_type_change_invalidates_reviewed_identity():
    base = {"ticker": "TEST", "direction": "buy", "quantity": 1, "price": 100,
            "order_type": "limit", "limit_price": 99}
    changed = dict(base, order_type="market")
    assert ep.action_digest(base) != ep.action_digest(changed)


def test_permission_id_change_invalidates_reviewed_identity():
    base = {
        "ticker": "VT", "direction": "buy", "quantity": 10, "price": 100,
        "capital_deployment_permission": {"permission_id": "permission-a"},
    }
    changed = {
        **base,
        "capital_deployment_permission": {"permission_id": "permission-b"},
    }
    assert ep.action_digest(base) != ep.action_digest(changed)


def test_dd_block_preflight_revalidates_scheduled_permission(monkeypatch, tmp_path):
    from capital_deployment import issue_scheduled_broad_permission

    action = {
        "ticker": "VT",
        "type": "buy",
        "direction": "buy",
        "source": "scheduled_broad_deployment",
        "human_execution_only": True,
        "quantity": 10,
        "price": 100,
        "estimated_notional_jpy": 150_000,
        "plan_item_id": "broad-plan-1",
        "route_id": "route-vt",
        "cash_wallet_key": "owner_a|broker_a|broker_cash|USD",
        "execution_owner": "owner_a",
        "execution_broker": "broker_a",
        "execution_account": "taxable",
        "settlement_pool": "broker_cash",
        "currency": "USD",
        "broad_family": "global_all_country",
        "execution_investment_type": "long",
    }
    action["capital_deployment_permission"] = issue_scheduled_broad_permission(
        action=action,
        canonical_dd_stage="block",
        dd_pacing_multiplier=0.25,
        state_snapshot={"dd": -0.081},
        now=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(ep, "_guard_metrics", lambda _base: (0.0, 0.0))
    monkeypatch.setattr(ep, "_analysis_risk_metrics", lambda _base: (0.01, "test", "2026-08-19"))
    monkeypatch.setattr(ep, "_promoted_drawdown_metrics", lambda _base: (-0.081, "block"))
    monkeypatch.setattr(ep, "_current_var_threshold", lambda _base, **_kwargs: 0.014)
    monkeypatch.setattr(ep, "_current_short_positions", lambda _base: 0)
    monkeypatch.setattr(ep, "_prospective_concentration", lambda _payload, _base: 0.04)
    monkeypatch.setattr(
        ep,
        "_prospective_concentration_observation",
        lambda _payload, _base: {
            "ratio": 0.04,
            "status": "ok",
            "proposed_notional_jpy": 150_000,
        },
    )
    monkeypatch.setattr(
        ep,
        "_broad_concentration_context",
        lambda _payload, _base: {
            **concentration_limits(
                broad_family="global_all_country", investment_type="long",
            ),
            "family_concentration_decimal": 0.10,
        },
    )

    valid = ep.evaluate_preflight_decision(action, base_dir=tmp_path)
    tampered = ep.evaluate_preflight_decision(
        {**action, "route_id": "another-route"}, base_dir=tmp_path,
    )

    assert valid["disposition"] == "confirmation_required"
    assert valid["metrics"]["scheduled_broad_permission"]["valid"] is True
    assert tampered["disposition"] == "hard_reject"
    assert "scheduled_broad_permission_invalid" in {
        row["code"] for row in tampered["hard_reasons"]
    }


def test_preflight_reads_risk_snapshot_and_live_promoted_drawdown(tmp_path, monkeypatch):
    (tmp_path / "guard_state.json").write_text(
        '{"daily_pnl_pct": -0.001, "monthly_pnl_pct": -0.002}', encoding="utf-8",
    )
    (tmp_path / "ai_portfolio_analysis.json").write_text(
        '{"as_of": "2026-08-03 06:00", "risk_snapshot": '
        '{"source": "parquet_reconstruction", "var_95_decimal": 0.01}}',
        encoding="utf-8",
    )
    (tmp_path / "drawdown_state.json").write_text(
        '{"enforcement_enabled": true, "last_drawdown_decimal": -0.081, "dd_state": "block"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ep, "load_api_key", lambda: "test-preflight-key")
    monkeypatch.setattr(ep, "_prospective_concentration", lambda payload, base_dir: 0.04)
    result = ep.evaluate_preflight(
        {"ticker": "TEST", "direction": "buy", "quantity": 1, "price": 100},
        base_dir=tmp_path,
    )
    assert result["metrics"]["var_1d_95_decimal"] == 0.01
    assert result["metrics"]["canonical_drawdown_decimal"] == -0.081
    assert result["metrics"]["canonical_drawdown_stage"] == "block"
    assert "drawdown_block" in {r["code"] for r in result["reasons"]}


def test_token_free_preflight_decision_never_loads_signing_key(tmp_path, monkeypatch):
    (tmp_path / "guard_state.json").write_text(
        '{"daily_pnl_pct": 0.0, "monthly_pnl_pct": 0.0}', encoding="utf-8",
    )
    (tmp_path / "ai_portfolio_analysis.json").write_text(
        '{"as_of": "2026-08-19T06:00:00+09:00", "risk_snapshot": '
        '{"source": "test", "var_95_decimal": 0.01}}', encoding="utf-8",
    )
    (tmp_path / "drawdown_state.json").write_text(
        '{"enforcement_enabled": true, "last_drawdown_decimal": -0.02, "dd_state": "ok"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ep, "_prospective_concentration", lambda payload, base_dir: 0.04)
    monkeypatch.setattr(ep, "load_api_key", lambda: (_ for _ in ()).throw(AssertionError("key must not load")))

    result = ep.evaluate_preflight_decision(
        {"ticker": "TEST", "direction": "buy", "quantity": 1, "price": 100},
        base_dir=tmp_path,
    )

    assert result["disposition"] == "ready"
    assert "preflight_token" not in result


def test_current_var_threshold_uses_fixed_regime_budgets(tmp_path):
    (tmp_path / "regime_state.json").write_text('{"regime": "A_強気"}', encoding="utf-8")
    (tmp_path / "vix_state.json").write_text('{"vix": {"level": 20}}', encoding="utf-8")
    assert ep._current_var_threshold(tmp_path, loss_guard_stage="ok") == POLICY.var_bull_decimal
    assert ep._current_var_threshold(tmp_path, loss_guard_stage="daily_block") == POLICY.var_bear_decimal


def test_analysis_cache_risk_snapshot_is_unit_explicit_and_position_free():
    from analyst import _build_execution_risk_snapshot

    snapshot = _build_execution_risk_snapshot(
        {
            "risk": {
                "source": "parquet_reconstruction",
                "var_95_decimal": 0.0123,
                "daily_pnl_decimal": -0.001,
                "positions": [{"ticker": "PRIVATE", "value_jpy": 1_000_000}],
            }
        },
        snapshot_as_of="2026-08-03 06:00",
    )
    assert snapshot["var_95_decimal"] == 0.0123
    assert snapshot["snapshot_as_of"] == "2026-08-03 06:00"
    assert "positions" not in snapshot


def test_concentration_check_is_strictly_read_only(tmp_path):
    files = {
        "guard_state.json": {"portfolio_value": 1_000_000},
        "ai_portfolio_analysis.json": {
            "as_of": "2026-08-19T07:00:00+09:00",
            "synthesis": {
                "analysis_id": "analysis-current",
                "investment_policy_observation": {
                    "denominator_jpy": 1_000_000,
                    "positions": [{
                        "canonical_instrument_id": "TEST",
                        "value_jpy": 70_000,
                        "cap_basis_tier": "long",
                    }],
                },
            },
        },
        "holdings.json": {
            # These persisted values are deliberately stale.  The preflight
            # must revalue the authoritative quantities at the execution
            # price instead of trusting either amount.
            "TEST_taxable": {"ticker": "TEST", "shares": 50, "current_value_jpy": 5_000},
            "TEST_nisa": {"ticker": "TEST", "shares": 20, "broker_position_value_jpy": 2_000},
        },
        "account.json": {"fx_rate_usdjpy": 150.0},
    }
    for name, value in files.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    before = {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in files
    }
    concentration = ep._prospective_concentration(
        {
            "analysis_id": "analysis-current", "ticker": "TEST", "direction": "buy",
            "quantity": 10, "price": 1_000, "currency": "JPY",
        },
        tmp_path,
    )
    after = {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in files
    }
    assert concentration == pytest.approx(0.08)
    assert after == before

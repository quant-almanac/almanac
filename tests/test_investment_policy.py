from copy import deepcopy

from investment_policy import (
    cash_deployment_policy,
    evaluate_concentration_policy,
)


def _position(
    ticker: str,
    value_jpy: float,
    *,
    investment_type: str = "long",
    owner: str = "husband",
    broker: str = "broker-a",
    account: str = "taxable",
) -> dict:
    return {
        "ticker": ticker,
        "value_jpy": value_jpy,
        "investment_type": investment_type,
        "owner": owner,
        "broker": broker,
        "account": account,
    }


def test_concentration_aggregates_same_instrument_across_household_accounts():
    positions = [
        _position("ABC", 600_000, owner="husband", broker="broker-a"),
        _position("abc", 500_000, owner="wife", broker="broker-b"),
    ]

    result = evaluate_concentration_policy(
        positions,
        portfolio_total_jpy=10_000_000,
    )

    assert result["mode"] == "shadow"
    assert result["action_effect"] == "none"
    assert result["breach_count"] == 1
    assert result["positions"][0]["canonical_instrument_id"] == "ABC"
    assert result["positions"][0]["value_jpy"] == 1_100_000
    assert result["positions"][0]["weight"] == 0.11
    assert result["positions"][0]["cap"] == 0.10


def test_concentration_uses_strictest_declared_tier_and_employer_override():
    positions = [
        _position("MIXED", 400_000, investment_type="long"),
        _position("MIXED", 100_000, investment_type="medium"),
        _position("EMP", 700_000, investment_type="medium"),
    ]

    result = evaluate_concentration_policy(
        positions,
        portfolio_total_jpy=10_000_000,
        employer_tickers={"EMP"},
    )
    rows = {row["canonical_instrument_id"]: row for row in result["positions"]}

    assert rows["MIXED"]["dominant_tier"] == "long"
    assert rows["MIXED"]["cap_basis_tier"] == "medium"
    assert rows["MIXED"]["cap"] == 0.05
    assert "mixed_tier_assignment:MIXED" in result["issues"]
    assert rows["EMP"]["employer_stock"] is True
    assert rows["EMP"]["cap_basis_tier"] == "employer_stock"
    assert rows["EMP"]["cap"] == 0.10
    assert rows["EMP"]["breached"] is False


def test_concentration_excludes_cash_positions():
    result = evaluate_concentration_policy(
        [
            _position("CASH_JPY", 9_000_000, investment_type="cash"),
            _position("ABC", 1_000_000),
        ],
        portfolio_total_jpy=10_000_000,
    )

    assert [row["canonical_instrument_id"] for row in result["positions"]] == ["ABC"]
    assert result["positions"][0]["weight"] == 0.10
    assert result["breach_count"] == 0


def test_missing_portfolio_total_is_visible_and_uses_invested_value_fallback():
    result = evaluate_concentration_policy(
        [_position("ABC", 600_000), _position("XYZ", 400_000)],
        portfolio_total_jpy=None,
    )

    assert result["denominator_jpy"] == 1_000_000
    assert "portfolio_total_missing_used_invested_positions" in result["issues"]
    assert result["status"] == "review"


def test_observation_does_not_mutate_input_positions():
    positions = [_position("ABC", 1_100_000)]
    original = deepcopy(positions)

    evaluate_concentration_policy(positions, portfolio_total_jpy=10_000_000)

    assert positions == original


def test_all_confirmed_system_cash_is_surplus_but_operational_reservations_remain():
    policy = cash_deployment_policy()

    assert policy == {
        "all_system_cash_is_surplus": True,
        "protected_cash_reserve_jpy": 0,
        "tactical_cash_retention_allowed": True,
        "operational_reservations_still_required": True,
        "monthly_budget_method": "deployable_surplus_divided_by_regime_horizon",
        "deployment_policy_version": "regime_horizon_v1",
        "deployment_months_by_level": {
            2: 2,
            1: 3,
            0: 6,
            -1: 12,
            -2: None,
        },
    }

from __future__ import annotations

import contribution_ledger as ledger


def _contribution(*, contribution_id: str = "salary-july", amount: int = 120_000, months: int = 1, bucket: str = "normal") -> dict:
    return {
        "id": contribution_id,
        "source": "salary",
        "bucket": bucket,
        "owner": "husband",
        "broker": "rakuten",
        "currency": "JPY",
        "amount_jpy": amount,
        "start_month": "2026-07",
        "release_months": months,
        "status": "approved",
    }


def test_released_money_carries_until_an_explicitly_linked_execution_consumes_it() -> None:
    summary = ledger.summarize_contributions(
        {"contributions": [_contribution(amount=120_000, months=2)]},
        {"executions": [
            # A real historical buy without funding provenance must not drain
            # a contribution approved after the fact.
            {"id": "legacy", "direction": "buy", "status": "executed", "notional_jpy": 90_000},
            {"id": "linked", "direction": "buy", "status": "executed", "contribution_id": "salary-july", "notional_jpy": 20_000, "saved_at": "2026-07-10T09:00:00"},
        ]},
        month="2026-08",
    )

    assert summary["released_this_month_jpy"] == 60_000
    assert summary["released_to_date_jpy"] == 120_000
    assert summary["consumed_this_month_jpy"] == 0
    assert summary["available_normal_jpy"] == 100_000


def test_order_reserves_and_execution_consumes_only_its_own_contribution() -> None:
    summary = ledger.summarize_contributions(
        {"contributions": [
            _contribution(contribution_id="normal", amount=100_000),
            _contribution(contribution_id="opportunity", amount=80_000, bucket="opportunity"),
        ]},
        {"executions": [
            {"id": "normal-order", "direction": "buy", "status": "ordered", "contribution_id": "normal", "notional_jpy": 40_000, "saved_at": "2026-07-04T09:00:00"},
            {"id": "opp-fill", "direction": "buy", "status": "executed", "contribution_id": "opportunity", "notional_jpy": 30_000, "saved_at": "2026-07-05T09:00:00"},
            {"id": "sell", "direction": "sell", "status": "executed", "contribution_id": "normal", "notional_jpy": 80_000},
        ]},
        month="2026-07",
    )

    assert summary["available_normal_jpy"] == 60_000
    assert summary["available_opportunity_jpy"] == 50_000
    assert summary["reserved_normal_jpy"] == 40_000
    assert summary["filled_opportunity_jpy"] == 30_000
    assert summary["consumed_this_month_jpy"] == 70_000


def test_release_schedule_distributes_remainder_deterministically() -> None:
    assert ledger.release_schedule(_contribution(amount=10, months=3)) == {
        "2026-07": 4,
        "2026-08": 3,
        "2026-09": 3,
    }

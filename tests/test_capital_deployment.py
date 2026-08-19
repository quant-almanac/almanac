from datetime import date, datetime, timezone

from capital_deployment import (
    active_operational_reservations,
    build_wallet_capacity_timeline,
    issue_scheduled_broad_permission,
    reachable_future_nisa_wait,
    resolve_drawdown_pacing,
    validate_scheduled_broad_permission,
)


def _scheduled_broad_action() -> dict:
    return {
        "ticker": "VT",
        "type": "buy",
        "source": "scheduled_broad_deployment",
        "human_execution_only": True,
        "estimated_notional_jpy": 500_000,
        "plan_item_id": "broad-plan-1",
        "route_id": "route-vt",
        "cash_wallet_key": "owner_a|broker_a|broker_cash|USD",
        "execution_owner": "owner_a",
        "execution_broker": "broker_a",
        "execution_account": "taxable",
        "settlement_pool": "broker_cash",
        "currency": "USD",
        "broad_family": "global_all_country",
    }


def test_wallet_timeline_applies_each_explicit_reservation_once():
    wallet_key = "wife|sbi|broker_cash|JPY"
    reservations = [
        {
            "reservation_id": f"example-{due}",
            "due_date": f"2026-08-{due}",
            "wallet_key": wallet_key,
            "amount_jpy": 10_000,
            "status": "active",
        }
        for due in (10, 17, 24, 31)
    ]
    timeline = build_wallet_capacity_timeline(
        [{
            "wallet_key": wallet_key,
            "owner": "wife",
            "broker": "sbi",
            "settlement_pool": "broker_cash",
            "currency": "JPY",
            "available_jpy": 100_000,
            "available_native": 100_000,
            "source_as_of": "2026-08-07T00:00:00+09:00",
        }],
        now=datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        month_end=date(2026, 8, 31),
        fx_rate_usdjpy=150.0,
        schedule_reservations=reservations,
        generate_schedule_reservations=False,
    )

    wallet = timeline["wallets"][0]
    assert wallet["unreflected_wallet_outflows_jpy"] == 20_000
    assert wallet["future_operational_reservations_jpy"] == 20_000
    assert wallet["available_after_all_reservations_jpy"] == 60_000


def test_future_nisa_wait_never_borrows_another_owners_wallet():
    result = reachable_future_nisa_wait(
        preferences={
            "approved_future_nisa_wait_jpy": 2_400_000,
            "approved_future_nisa_wait_owner": "husband",
            "future_nisa_growth_capacity_jpy": 2_400_000,
            "future_nisa_capacity_approved_by": "owner",
        },
        nisa={"husband": {"broker": "楽天証券"}, "wife": {"broker": "SBI証券"}},
        wallets_after_reservations=[
            {
                "wallet_key": "husband|rakuten|broker_cash|USD",
                "available_after_operational_reservations_jpy": 1_000_000,
            },
            {
                "wallet_key": "wife|sbi|broker_cash|JPY",
                "available_after_operational_reservations_jpy": 9_000_000,
            },
        ],
    )

    assert result["approved_nisa_wait_jpy"] == 1_000_000
    assert result["no_cross_owner_transfer"] is True


def test_missing_controller_is_prepromotion_but_unreadable_ledger_fails_closed(tmp_path):
    assert resolve_drawdown_pacing(base_dir=tmp_path)["dd_pacing_multiplier"] == 1.0
    (tmp_path / "almanac.db").write_text("not sqlite", encoding="utf-8")
    assert resolve_drawdown_pacing(base_dir=tmp_path)["dd_pacing_multiplier"] == 0.0


def test_scheduled_broad_permission_is_action_bound():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    action = _scheduled_broad_action()
    action["capital_deployment_permission"] = issue_scheduled_broad_permission(action=action, canonical_dd_stage="block", dd_pacing_multiplier=.25, state_snapshot={"dd": -.09}, now=now)
    assert validate_scheduled_broad_permission(action, canonical_dd_stage="block", now=now)
    action["ticker"] = "VTI"
    assert not validate_scheduled_broad_permission(action, canonical_dd_stage="block", now=now)


def test_scheduled_broad_permission_rejects_tampered_contract_fields():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    action = _scheduled_broad_action()
    action["capital_deployment_permission"] = issue_scheduled_broad_permission(
        action=action, canonical_dd_stage="block", dd_pacing_multiplier=0.25,
        state_snapshot={"dd": -0.09}, now=now,
    )
    action["capital_deployment_permission"]["max_notional_jpy"] = 9_000_000
    assert not validate_scheduled_broad_permission(
        action, canonical_dd_stage="block", now=now,
    )


def test_operational_reservation_uses_latest_status_for_duplicate_id():
    reservations = active_operational_reservations([
        {"reservation_id": "same", "status": "active", "amount_jpy": 50_000},
        {"reservation_id": "same", "status": "cancelled", "amount_jpy": 50_000},
        {"reservation_id": "other", "status": "active", "amount_jpy": 10_000},
    ])
    assert [row["reservation_id"] for row in reservations] == ["other"]


def test_unroutable_open_buy_fails_closed_for_every_wallet():
    timeline = build_wallet_capacity_timeline(
        [{
            "wallet_key": "owner_a|broker_a|broker_cash|USD",
            "owner": "owner_a",
            "broker": "broker_a",
            "settlement_pool": "broker_cash",
            "currency": "USD",
            "available_jpy": 1_000_000,
            "available_native": 6_666,
            "source_as_of": "2026-08-19T00:00:00+09:00",
        }],
        now=datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        month_end=date(2026, 8, 31),
        fx_rate_usdjpy=150.0,
        executions=[{
            "id": "open-unroutable",
            "status": "ordered",
            "direction": "buy",
            "ticker": "VT",
            "quantity": 10,
            "price": 100,
            "currency": "USD",
        }],
        generate_schedule_reservations=False,
    )

    assert timeline["all_wallets_resolved"] is False
    assert timeline["unresolved_open_order_ids"] == ["open-unroutable"]
    assert timeline["wallets"][0]["reservation_status"] == "open_order_route_unresolved"
    assert timeline["wallets"][0]["available_after_all_reservations_jpy"] == 0


def test_open_buy_for_an_unlisted_wallet_also_fails_closed():
    timeline = build_wallet_capacity_timeline(
        [{
            "wallet_key": "owner_a|broker_a|broker_cash|USD",
            "owner": "owner_a",
            "broker": "broker_a",
            "settlement_pool": "broker_cash",
            "currency": "USD",
            "available_jpy": 1_000_000,
            "available_native": 6_666,
            "source_as_of": "2026-08-19T00:00:00+09:00",
        }],
        now=datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
        month_end=date(2026, 8, 31),
        fx_rate_usdjpy=150.0,
        executions=[{
            "id": "open-other-wallet",
            "status": "ordered",
            "direction": "buy",
            "ticker": "VT",
            "quantity": 10,
            "price": 100,
            "currency": "USD",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "settlement_pool": "broker_cash",
        }],
        generate_schedule_reservations=False,
    )

    assert timeline["all_wallets_resolved"] is False
    assert timeline["unresolved_open_order_ids"] == ["open-other-wallet"]
    assert timeline["wallets"][0]["available_after_all_reservations_jpy"] == 0

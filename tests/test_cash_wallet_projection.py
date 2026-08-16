from datetime import datetime

from cash_wallet_projection import build_wallet_projection


def _cash_info():
    return {
        "wallets": [
            {
                "wallet_key": "husband|rakuten|broker_cash|JPY",
                "owner": "husband", "broker": "rakuten", "settlement_pool": "broker_cash",
                "currency": "JPY", "available_native": 1000, "available_jpy": 1000,
                "source_as_of": "2026-08-01", "resources": ["account.json:JPY"],
            },
            {
                "wallet_key": "wife|sbi|broker_cash|JPY",
                "owner": "wife", "broker": "sbi", "settlement_pool": "broker_cash",
                "currency": "JPY", "available_native": 2000, "available_jpy": 2000,
                "source_as_of": "2026-08-01", "resources": ["CASH_JPY_SBI_WIFE"],
            },
        ]
    }


def test_projection_never_assigns_wife_or_card_schedule_events_to_husband_wallet():
    projection = build_wallet_projection(_cash_info(), now=datetime(2026, 8, 16), event_rows=[
        {"event_id": "card", "event_type": "cash_flow", "direction": "in", "quantity": 100000,
         "price": 1, "currency": "JPY", "source": "schedule_backfill",
         "raw_payload": {"funding_source": "external_card", "owner": "husband", "broker": "rakuten"}},
        {"event_id": "wife", "event_type": "cash_flow", "direction": "out", "quantity": 23076,
         "price": 1, "currency": "JPY", "source": "schedule_backfill",
         "raw_payload": {"funding_source": "broker_cash", "cash_route": "CASH_JPY_SBI_WIFE"}},
    ])

    wallets = {row["wallet_key"]: row for row in projection["wallets"]}
    assert wallets["husband|rakuten|broker_cash|JPY"]["ledger_delta_native"] == 0
    assert wallets["wife|sbi|broker_cash|JPY"]["ledger_delta_native"] == 0
    assert wallets["wife|sbi|broker_cash|JPY"]["excluded_events"] == [{
        "event_id": "wife", "code": "schedule_or_external_funding_not_broker_cash", "event_type": "cash_flow",
    }]
    assert projection["excluded_cash_events"]


def test_projection_applies_only_explicitly_routed_cash_leg_without_changing_buying_power():
    projection = build_wallet_projection(_cash_info(), now=datetime(2026, 8, 16), event_rows=[
        {"event_id": "fee", "event_type": "fee", "direction": "out", "quantity": 100,
         "price": 1, "currency": "JPY", "source": "api", "occurred_at": "2026-08-02T09:00:00",
         "raw_payload": {"cash_route": "CASH_JPY_SBI_WIFE"}},
    ])

    wife = next(row for row in projection["wallets"] if row["owner"] == "wife")
    assert wife["ledger_delta_native"] == -100
    assert wife["projected_available_native"] == 1900
    assert wife["available_for_new_buy_native"] == 2000
    assert wife["source_hash"]
    assert wife["valid_until"] is None
    assert projection["authoritative_for_new_buys"] is False


def test_projection_excludes_events_at_or_before_broker_balance_baseline():
    projection = build_wallet_projection(_cash_info(), now=datetime(2026, 8, 16), event_rows=[
        {"event_id": "historic-fee", "event_type": "fee", "direction": "out", "quantity": 100,
         "price": 1, "currency": "JPY", "source": "api", "occurred_at": "2026-08-01T00:00:00",
         "raw_payload": {"cash_route": "CASH_JPY_SBI_WIFE"}},
    ])
    wife = next(row for row in projection["wallets"] if row["owner"] == "wife")
    assert wife["ledger_delta_native"] == 0
    assert wife["excluded_events"][0]["code"] == "cash_event_reflected_in_base_balance"


def test_date_only_balance_baseline_does_not_assume_same_day_event_ordering():
    projection = build_wallet_projection(_cash_info(), now=datetime(2026, 8, 16), event_rows=[
        {"event_id": "same-day-fee", "event_type": "fee", "direction": "out", "quantity": 100,
         "price": 1, "currency": "JPY", "source": "api", "occurred_at": "2026-08-01T18:00:00",
         "raw_payload": {"cash_route": "CASH_JPY_SBI_WIFE"}},
    ])
    wife = next(row for row in projection["wallets"] if row["owner"] == "wife")
    assert wife["ledger_delta_native"] == 0


def test_projection_keeps_unknown_wallet_events_unattributed():
    projection = build_wallet_projection(_cash_info(), now=datetime(2026, 8, 16), event_rows=[
        {"event_id": "unknown", "event_type": "cash_flow", "direction": "in", "quantity": 100,
         "price": 1, "currency": "JPY", "source": "api", "raw_payload": {}},
    ])

    assert projection["unattributed_cash_events"] == [{
        "event_id": "unknown", "code": "cash_event_wallet_unattributed", "event_type": "cash_flow",
    }]

from datetime import date

import contribution_schedule as schedule


def test_only_explicit_broker_cash_schedule_reserves_its_wallet(monkeypatch):
    monkeypatch.setattr(schedule, "CONTRIBUTIONS", [
        {
            "id": "external_card_example", "label": "Example card contribution",
            "amount": 100_000, "currency": "JPY", "cadence": "monthly",
            "day_of_month": 1, "owner": "husband", "broker": "rakuten",
            "funding_source": "external_card", "cash_route": None,
        },
        {
            "id": "broker_cash_example", "label": "Example broker cash contribution",
            "amount": 25_000, "currency": "JPY", "cadence": "weekly",
            "weekday": 0, "owner": "wife", "broker": "sbi",
            "funding_source": "broker_cash", "cash_route": "CASH_JPY_SBI_WIFE",
        },
    ])

    wallet_rows = schedule.cash_route_outflows(
        owner="wife", broker="sbi", currency="JPY",
        cash_route="CASH_JPY_SBI_WIFE",
        date_from="2026-08-07", date_to="2026-08-13",
    )
    card_rows = schedule.cash_route_outflows(
        owner="husband", broker="rakuten", currency="JPY",
        cash_route="account.json",
        date_from="2026-08-01", date_to="2026-08-03",
    )

    assert wallet_rows == [(date(2026, 8, 10), schedule.CONTRIBUTIONS[1])]
    assert card_rows == []


def test_cash_schedule_does_not_guess_route(monkeypatch):
    monkeypatch.setattr(schedule, "CONTRIBUTIONS", [{
        "id": "broker_cash_example", "label": "Example",
        "amount": 25_000, "currency": "JPY", "cadence": "weekly",
        "weekday": 0, "owner": "wife", "broker": "sbi",
        "funding_source": "broker_cash", "cash_route": "CASH_JPY_SBI_WIFE",
    }])

    assert schedule.cash_route_outflows(
        owner="wife", broker="sbi", currency="JPY", cash_route="",
        date_from="2026-08-07", date_to="2026-08-13",
    ) == []

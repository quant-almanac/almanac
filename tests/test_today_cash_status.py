from api.routes.today import _build_cash_status, _build_funding_alternatives


def test_today_cash_status_includes_all_plan_wallets_not_only_sbi():
    plan = {
        "cash_info": {"wallets": [
            {"wallet_key": "husband|rakuten|broker_cash|USD", "resources": ["account.json:USD"],
             "owner": "husband", "broker": "rakuten", "settlement_pool": "broker_cash",
             "currency": "USD", "available_native": 100.0, "source_as_of": "2026-08-14"},
            {"wallet_key": "wife|sbi|broker_cash|JPY", "resources": ["CASH_JPY_SBI_WIFE"],
             "owner": "wife", "broker": "sbi", "settlement_pool": "broker_cash",
             "currency": "JPY", "available_native": 200.0, "source_as_of": "2026-08-14"},
        ]},
        "cash_wallet_projection": {
            "authoritative_for_new_buys": False,
            "wallets": [{"wallet_key": "wife|sbi|broker_cash|JPY", "projected_available_native": 150.0,
                         "status": "observed_not_authoritative"}],
        },
    }
    holdings = {"CASH_JPY_SBI_WIFE": {"balance_status": "confirmed", "shares": 200.0}}

    rows = _build_cash_status(plan, holdings)

    assert [row["wallet_key"] for row in rows] == [
        "husband|rakuten|broker_cash|USD", "wife|sbi|broker_cash|JPY",
    ]
    wife = rows[1]
    assert wife["projected_balance"] == 150.0
    assert wife["projection_authoritative_for_new_buys"] is False


def test_funding_alternatives_offer_wallet_fx_and_no_trade_without_moving_cash():
    cash_status = _build_cash_status({
        "cash_info": {"wallets": [
            {"wallet_key": "husband|rakuten|broker_cash|USD", "owner": "husband", "broker": "rakuten", "settlement_pool": "broker_cash", "currency": "USD", "available_native": 100, "resources": []},
            {"wallet_key": "husband|rakuten|broker_cash|JPY", "owner": "husband", "broker": "rakuten", "settlement_pool": "broker_cash", "currency": "JPY", "available_native": 1000, "resources": []},
        ]},
    }, {})
    options = _build_funding_alternatives([
        {"ticker": "1489.T", "type": "buy", "currency": "JPY", "execution_owner": "wife", "execution_broker": "sbi", "execution_account": "NISA成長投資枠"},
    ], cash_status)

    kinds = [row["kind"] for row in options[0]["alternatives"]]
    assert "current_route" in kinds
    assert "taxable_wallet" in kinds
    assert "cross_owner_transfer_then_current_route" in kinds
    assert "fx_then_taxable_wallet" in kinds
    assert kinds[-1] == "no_trade"

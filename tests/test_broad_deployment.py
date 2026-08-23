import json
from datetime import datetime, timezone

from broad_deployment import generate_candidate, generate_from_analysis_context
from capital_deployment import validate_scheduled_broad_permission


def _plan(*, remaining=2_000_000, wallet=3_000_000):
    return {
        "budgets": {
            "normal_pool_available_jpy": 1_500_000,
            "weekly_normal_jpy": 1_000_000,
        },
        "items": [{
            "plan_item_id": "broad-core-1",
            "objective": "deploy_surplus_broad_core",
            "status": "active",
            "remaining_jpy": remaining,
            "preferred_tickers": ["VT"],
            "constraints": {"broad_family": "global_all_country"},
        }],
        "cash_info": {
            "wallet_capacity_timeline": {
                "all_wallets_resolved": True,
                "wallets": [{
                    "wallet_key": "owner_a|broker_a|broker_cash|USD",
                    "reservation_status": "ok",
                    "available_after_all_reservations_jpy": wallet,
                }],
            },
        },
    }


def _route():
    return {
        "schema_version": 1,
        "routes": [{
            "route_id": "route-vt",
            "active": True,
            "ticker": "VT",
            "owner": "owner_a",
            "broker": "broker_a",
            "account": "taxable",
            "investment_type": "long",
            "settlement_pool": "broker_cash",
            "currency": "USD",
            "cash_route": "cash-usd",
        }],
    }


def _policy(*, vt_value=0):
    return {
        "denominator_jpy": 30_000_000,
        "positions": [
            {"canonical_instrument_id": "SLIM_ORCAN", "value_jpy": 2_000_000, "cap_basis_tier": "long"},
            {"canonical_instrument_id": "VT", "value_jpy": vt_value, "cap_basis_tier": "long"},
        ],
    }


def test_active_global_gap_generates_exact_routed_vt_candidate():
    action, observation = generate_candidate(
        execution_plan=_plan(),
        policy_observation=_policy(),
        route_config=_route(),
        price=100,
        fx_rate_usdjpy=150,
        now=datetime(2026, 8, 19, tzinfo=timezone.utc),
    )

    assert observation["status"] == "candidate_generated"
    assert action["ticker"] == "VT"
    assert action["quantity"] == 33
    assert action["estimated_notional_jpy"] == 495_000
    assert action["plan_item_id"] == "broad-core-1"
    assert action["cash_wallet_key"] == "owner_a|broker_a|broker_cash|USD"
    assert action["human_execution_only"] is True


def test_route_currency_is_canonicalized_before_fx_sizing():
    route = _route()
    route["routes"][0]["currency"] = "usd"

    action, observation = generate_candidate(
        execution_plan=_plan(), policy_observation=_policy(),
        route_config=route, price=100, fx_rate_usdjpy=150,
    )

    assert observation["status"] == "candidate_generated"
    assert action["currency"] == "USD"
    assert action["quantity"] == 33
    assert action["estimated_notional_jpy"] == 495_000


def test_block_pacing_issues_an_action_bound_human_permission():
    plan = _plan()
    plan["as_of"] = "2026-08-19T06:30:00+09:00"
    plan["budgets"].update({
        "canonical_dd_stage": "block",
        "dd_pacing_multiplier": 0.25,
        "dd_enforcement_active": True,
        "dd_promotion_history_status": "promoted_valid",
        "deployment_basis_cash_jpy": 8_000_000,
        "market_deployment_target_jpy": 4_000_000,
    })

    action, observation = generate_candidate(
        execution_plan=plan, policy_observation=_policy(),
        route_config=_route(), price=100, fx_rate_usdjpy=150,
        now=datetime(2026, 8, 19, tzinfo=timezone.utc),
    )

    assert observation["status"] == "candidate_generated"
    assert observation["capital_deployment_permission_id"]
    assert validate_scheduled_broad_permission(
        action, canonical_dd_stage="block",
        now=datetime(2026, 8, 19, 1, tzinfo=timezone.utc),
    )
    assert not validate_scheduled_broad_permission(
        {**action, "route_id": "another-route"}, canonical_dd_stage="block",
        now=datetime(2026, 8, 19, 1, tzinfo=timezone.utc),
    )


def test_no_active_global_gap_means_no_default_candidate():
    plan = _plan()
    plan["items"] = []
    action, observation = generate_candidate(
        execution_plan=plan, policy_observation=_policy(), route_config=_route(),
        price=100, fx_rate_usdjpy=150,
    )
    assert action is None
    assert observation["status"] == "no_active_global_all_country_gap"


def test_vt_caution_stays_reviewable_and_never_selects_an_alternate():
    action, observation = generate_candidate(
        execution_plan=_plan(), policy_observation=_policy(vt_value=5_600_000),
        route_config=_route(), price=100, fx_rate_usdjpy=150,
    )
    assert action is not None
    assert observation["status"] == "caution_review"
    assert action["concentration_review_required"] is True
    assert observation["alternate_selected"] is False


def test_vt_cap_or_missing_route_fails_closed_without_alternate():
    capped, cap_observation = generate_candidate(
        execution_plan=_plan(), policy_observation=_policy(vt_value=7_100_000),
        route_config=_route(), price=100, fx_rate_usdjpy=150,
    )
    missing, route_observation = generate_candidate(
        execution_plan=_plan(), policy_observation=_policy(), route_config={"routes": []},
        price=100, fx_rate_usdjpy=150,
    )
    assert capped is None
    assert cap_observation["status"] == "default_concentration_cap_reached"
    assert missing is None
    assert route_observation["status"] == "route_missing"


def test_wallet_reservations_bound_candidate_notional():
    action, observation = generate_candidate(
        execution_plan=_plan(wallet=400_000), policy_observation=_policy(),
        route_config=_route(), price=100, fx_rate_usdjpy=150,
    )
    assert observation["status"] == "candidate_generated"
    assert action["quantity"] == 26
    assert action["estimated_notional_jpy"] == 390_000


def test_missing_route_does_not_trigger_a_market_price_request(tmp_path):
    action, observation = generate_from_analysis_context(
        base_dir=tmp_path,
        execution_plan=_plan(),
        policy_observation=_policy(),
        existing_actions=[],
        fx_rate_usdjpy=150,
        now=datetime(2026, 8, 19, tzinfo=timezone.utc),
        price_provider=lambda *_args: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    assert action is None
    assert observation["status"] == "route_missing"


def test_explicit_base_dir_wins_over_process_state_redirect(tmp_path, monkeypatch):
    runtime_state = tmp_path / "runtime"
    explicit_state = tmp_path / "explicit"
    runtime_state.mkdir()
    explicit_state.mkdir()
    (runtime_state / "broad_execution_routes.json").write_text(
        json.dumps(_route()), encoding="utf-8",
    )
    monkeypatch.setenv("ALMANAC_STATE_DIR", str(runtime_state))

    action, observation = generate_from_analysis_context(
        base_dir=explicit_state,
        execution_plan=_plan(),
        policy_observation=_policy(),
        existing_actions=[],
        fx_rate_usdjpy=150,
        now=datetime(2026, 8, 19, tzinfo=timezone.utc),
        price_provider=lambda *_args: (_ for _ in ()).throw(
            AssertionError("explicit route is missing, so price must not be fetched")
        ),
    )

    assert action is None
    assert observation["status"] == "route_missing"


def test_a_float_price_is_rounded_to_an_orderable_limit():
    """本題: 証券会社が受け付けられる刻みで指値を出すこと。

    2026-08-24 の本番出力に limit_price=160.77000427246094 が出ていた。
    price は parquet/yfinance 由来の float なのでそのまま渡していたのが原因。
    人間が読む action 文だけは :g 書式で "160.77" に見えており、
    構造化フィールドの側だけ気付かれずに残っていた。
    """
    action, observation = generate_candidate(
        execution_plan=_plan(),
        policy_observation=_policy(),
        route_config=_route(),
        price=160.77000427246094,
        fx_rate_usdjpy=150,
        now=datetime(2026, 8, 19, tzinfo=timezone.utc),
    )

    assert observation["status"] == "candidate_generated"
    assert action["limit_price"] == 160.77
    assert action["decision_price"] == 160.77
    # 表示と構造化フィールドが一致していること。
    assert "160.77" in action["action"]
    assert "160.77000427246094" not in action["action"]


def test_a_price_that_rounds_to_zero_fails_closed_instead_of_dividing():
    """丸めた結果が 0 になる価格でゼロ除算しないこと。

    price_value <= 0 の検査は丸める前の値しか見ていないので、
    丸めを入れたことで 0 < price < 0.005 が新たにゼロ除算経路になった。
    generate_candidate は分析の実行中に呼ばれるため、ここで落ちると
    その日の分析ごと巻き込む。
    """
    action, observation = generate_candidate(
        execution_plan=_plan(),
        policy_observation=_policy(),
        route_config=_route(),
        price=0.004,
        fx_rate_usdjpy=150,
        now=datetime(2026, 8, 19, tzinfo=timezone.utc),
    )

    assert action is None
    assert observation["status"] == "price_or_fx_unresolved"

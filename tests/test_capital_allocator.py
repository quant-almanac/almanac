from capital_allocator import allocate_actions, record_comparison, review_comparison


def _buy(ticker: str, quantity: int, *, price: float = 355, fx: float = 159.452) -> dict:
    return {
        "ticker": ticker,
        "type": "add",
        "tier": "Long",
        "currency": "USD",
        "quantity": quantity,
        "requested_buy_quantity": quantity,
        "decision_price": price,
        "estimated_notional_jpy": round(quantity * price * fx),
        "execution_readiness": "ready",
        "execution_owner": "husband",
        "execution_broker": "rakuten",
        "execution_account": "特定",
        "confidence_pct": 70,
    }


def test_frozen_v_fixture_keeps_four_shares_and_rejects_five_over_normal_cap():
    v = _buy("V", 4)
    v["execution_plan_item_id"] = "usd-financials"
    actions, report = allocate_actions([v, _buy("OTHER", 5)], fx_rate=159.452)

    selected = next(row for row in actions if row["ticker"] == "V")
    rejected = next(row for row in actions if row["ticker"] == "OTHER")
    assert selected["execution_readiness"] == "ready"
    assert selected["quantity"] == 4
    assert report["selected_ticker"] == "V"
    assert rejected["execution_readiness"] == "review"
    assert any(row["code"] == "capital_allocator_daily_buy_limit" for row in rejected["execution_block_reasons"])


def test_allocator_resizes_within_cap_but_never_forces_below_minimum():
    # $355 * 159.452: 5 shares exceed ¥250k, 4 shares remain above the floor.
    actions, report = allocate_actions([_buy("V", 5)], fx_rate=159.452)
    row = actions[0]
    assert report["selected_ticker"] == "V"
    assert row["quantity"] == 4
    assert row["estimated_notional_jpy"] == 226_422

    tiny, tiny_report = allocate_actions([_buy("V", 1, price=2_000)], fx_rate=159.452)
    assert tiny_report["selected_ticker"] is None
    assert tiny[0]["execution_readiness"] == "review"
    assert any(row["code"] == "capital_allocator_quantity_below_minimum" for row in tiny[0]["execution_block_reasons"])


def test_legacy_mode_preserves_existing_ready_actions_and_swing_is_outside_normal_cap():
    normal = _buy("V", 4)
    swing = _buy("SWING", 10)
    swing["tier"] = "Swing"
    actions, report = allocate_actions([normal, swing], mode="legacy")

    assert report["mode"] == "legacy"
    assert all(row["execution_readiness"] == "ready" for row in actions)
    assert report["candidate_count"] == 1


def test_allocator_comparison_review_is_explicit_and_side_effect_free(tmp_path):
    record_comparison("analysis-1", {"mode": "enforce"}, base_dir=tmp_path)
    reviewed = review_comparison("analysis-1", "approved", base_dir=tmp_path)
    assert reviewed["review"]["decision"] == "approved"


def test_analyst_integration_updates_decision_summary_after_allocator(monkeypatch, tmp_path):
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    synthesis = {
        "priority_actions": [_buy("V", 4), _buy("OTHER", 4)],
        "decision_summary": {"candidate_count": 2, "executable_count": 2, "review_count": 0, "deferred_count": 0, "reason_counts": {}},
        "overall_stance": "neutral",
    }
    analyst._apply_capital_allocator(
        synthesis, {"portfolio_total": 30_639_000}, fx_rate=159.452,
        analysis_id="analysis-allocator-test", as_of="2026-08-14T06:15:00",
    )

    assert synthesis["capital_allocator"]["mode"] == "enforce"
    assert synthesis["decision_summary"]["executable_count"] == 1
    assert synthesis["decision_summary"]["review_count"] == 1
    assert synthesis["capital_allocator_comparison"]["run_id"] == "analysis-allocator-test"

from capital_allocator import allocate_actions, build_comparison, record_comparison, review_comparison


def _buy(ticker: str, quantity: int, *, price: float = 355, fx: float = 159.452) -> dict:
    estimated = round(quantity * price * fx)
    return {
        "ticker": ticker,
        "type": "add",
        "tier": "Long",
        "currency": "USD",
        "quantity": quantity,
        "requested_buy_quantity": quantity,
        "decision_price": price,
        "estimated_notional_jpy": estimated,
        "amount_hint": f"{quantity}株",
        "action": f"{ticker}を{quantity}株、約¥{estimated:,}で買付",
        "reason": f"{quantity}株を約¥{estimated:,}で通常買付",
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
    assert row["requested_buy_quantity"] == 4
    assert row["amount_hint"] == "4株"
    assert row["estimated_notional_jpy"] == 226_422
    assert "4株" in row["action"]
    assert "¥226,422" in row["action"]

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


def test_allocator_preserves_jpx_regular_lot_when_capping_quantity():
    toyota = _buy("7203.T", 200, price=1_500, fx=1)
    toyota.update({"currency": "JPY", "estimated_notional_jpy": 300_000,
                   "action": "7203.Tを200株、約¥300,000で買付", "reason": "200株を約¥300,000で通常買付"})
    actions, report = allocate_actions([toyota], fx_rate=1, min_trade_jpy=100_000)
    assert report["selected_ticker"] == "7203.T"
    assert actions[0]["quantity"] == 100
    assert actions[0]["amount_hint"] == "100株"


def test_allocator_comparison_rejects_unstructured_selected_quantity_change():
    legacy = [_buy("V", 5)]
    allocated = [_buy("V", 4)]
    allocated[0].update({"capital_allocator_selected": True, "capital_allocator_size_applied": {"quantity": {"from": 5, "to": 4}}})
    comparison = build_comparison(legacy, allocated, {"mode": "enforce", "legacy_ready_tickers": ["V"], "selected_ticker": "V"}, count_conservation_ok=True)
    assert comparison["explanation_status"] == "explainable"
    allocated[0].pop("capital_allocator_size_applied")
    comparison = build_comparison(legacy, allocated, {"mode": "enforce", "legacy_ready_tickers": ["V"], "selected_ticker": "V"}, count_conservation_ok=True)
    assert "allocator_quantity_change_unexplained" in comparison["explanation_reasons"]


def test_analyst_integration_updates_decision_summary_after_allocator(monkeypatch, tmp_path):
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    synthesis = {
        "priority_actions": [_buy("V", 4), _buy("OTHER", 4)],
        "decision_summary": {"candidate_count": 2, "executable_count": 2, "review_count": 0, "deferred_count": 0, "reason_counts": {}, "count_conservation_ok": True},
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

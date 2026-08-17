import action_stage_log
from api.routes import today


def _entry(stage, ticker="TEST", account="特定", **extra):
    return {
        "analysis_id": "analysis-flow", "stage": stage, "ticker": ticker,
        "canonical_action_type": "buy", "account": account,
        "execution_block_reason_codes": [], **extra,
    }


def test_read_analysis_entries_is_isolated_and_invalidates_after_append(tmp_path):
    # conftest redirects LOG_PATH to tmp_path; ALMANAC_STATE_DIR is irrelevant
    # to this ledger and must never be relied upon for isolation.
    action_stage_log.append_entries([_entry("tier_generated")])
    assert len(action_stage_log.read_analysis_entries("analysis-flow")) == 1
    action_stage_log.append_entries([_entry("opus_raw")])
    assert [row["stage"] for row in action_stage_log.read_analysis_entries("analysis-flow")] == [
        "tier_generated", "opus_raw",
    ]
    assert not (tmp_path / "production-action_stage_log.jsonl").exists()


def test_decision_flow_expired_board_row_is_closed_not_approved():
    action_stage_log.append_entries([
        _entry("tier_generated"), _entry("opus_raw"), _entry("policy_accepted"),
        _entry("post_filter_final", execution_block_reason_codes=["stale_price"]),
    ])
    board = [{
        "ticker": "TEST", "type": "buy", "execution_account": "特定",
        "lifecycle": {"status": "expired"}, "execution_block_reasons": [],
    }]
    flow = today._build_decision_flow(
        analysis_id="analysis-flow", synthesis={}, board=board, review_board=[],
        execution_plan={"filtered_examples": [], "filtered_summary": {}},
        decision_summary={"executable_count": 1, "review_count": 0, "deferred_count": 0},
    )
    action = flow["actions"][0]
    assert action["decision_status"] == "closed"
    assert action["execution_status"] == "expired"
    assert board[0]["decision_flow_key"] == action["key"]


def test_decision_flow_reprice_and_executed_follow_current_lifecycle_contract():
    action_stage_log.append_entries([
        _entry("tier_generated"), _entry("opus_raw"), _entry("policy_accepted"),
        _entry("post_filter_final"), _entry("executed"),
    ])
    reprice = [{
        "ticker": "REPRICE", "type": "buy", "execution_account": "特定",
        "lifecycle": {"status": "reprice_required"}, "execution_block_reasons": [],
    }]
    executed = [{
        "ticker": "TEST", "type": "buy", "execution_account": "特定",
        "lifecycle": {"status": "filled"}, "execution_block_reasons": [],
    }]
    reprice_flow = today._build_decision_flow(
        analysis_id="analysis-flow", synthesis={}, board=reprice, review_board=[],
        execution_plan={"filtered_examples": [], "filtered_summary": {}}, decision_summary={},
    )
    executed_flow = today._build_decision_flow(
        analysis_id="analysis-flow", synthesis={}, board=executed, review_board=[],
        execution_plan={"filtered_examples": [], "filtered_summary": {}}, decision_summary={},
    )
    reprice_action = next(action for action in reprice_flow["actions"] if action["ticker"] == "REPRICE")
    executed_action = next(action for action in executed_flow["actions"] if action["ticker"] == "TEST")
    assert reprice_action["decision_status"] == "review"
    assert reprice_action["execution_status"] == "reprice_required"
    assert executed_action["execution_status"] == "executed"


def test_decision_flow_lifecycle_table_and_board_review_overlap_fail_closed():
    action_stage_log.append_entries([_entry("post_filter_final", ticker="BASE")])
    expected = {
        "pending": ("ready", "not_started"),
        "proposed": ("ready", "not_started"),
        "placed": ("ready", "ordered"),
        "filled": ("ready", "filled"),
        "cancelled": ("closed", "cancelled"),
        "expired": ("closed", "expired"),
        "reprice_required": ("review", "reprice_required"),
        "unknown": ("review", "not_started"),
    }
    for lifecycle, status in expected.items():
        flow = today._build_decision_flow(
            analysis_id="analysis-flow", synthesis={},
            board=[{"ticker": lifecycle, "type": "buy", "execution_account": "特定",
                    "lifecycle": {"status": lifecycle}, "execution_block_reasons": []}],
            review_board=[], execution_plan={"filtered_examples": [], "filtered_summary": {}},
            decision_summary={},
        )
        action = next(action for action in flow["actions"] if action["ticker"] == lifecycle)
        assert (action["decision_status"], action["execution_status"]) == status

    duplicate = {"ticker": "DUP", "type": "buy", "execution_account": "特定",
                 "lifecycle": {"status": "proposed"}, "execution_block_reasons": []}
    flow = today._build_decision_flow(
        analysis_id="analysis-flow", synthesis={}, board=[duplicate], review_board=[duplicate.copy()],
        execution_plan={"filtered_examples": [], "filtered_summary": {}}, decision_summary={},
    )
    action = next(action for action in flow["actions"] if action["ticker"] == "DUP")
    assert (action["decision_status"], action["execution_status"]) == ("review", "not_started")
    assert flow["integrity"]["status"] == "mismatch"


def test_decision_flow_uses_today_reason_wording_over_log_code():
    action_stage_log.append_entries([
        _entry("post_filter_final", execution_block_reason_codes=["cash_balance_insufficient"]),
    ])
    flow = today._build_decision_flow(
        analysis_id="analysis-flow", synthesis={}, board=[],
        review_board=[{
            "ticker": "TEST", "type": "buy", "execution_account": "特定",
            "lifecycle": {"status": "proposed"},
            "execution_block_reasons": [{
                "code": "cash_balance_insufficient", "message": "現在の余力では注文できません",
            }],
        }],
        execution_plan={"filtered_examples": [], "filtered_summary": {}}, decision_summary={},
    )
    reason = flow["actions"][0]["reasons"][0]
    assert reason == {
        "code": "cash_balance_insufficient", "message": "現在の余力では注文できません",
        "provenance": "today_overlay",
    }


def test_decision_flow_sampled_filter_detail_does_not_become_partial():
    examples = [{"ticker": f"T{i}", "type": "buy", "code": "plan_gate"} for i in range(5)]
    action_stage_log.append_entries([_entry("post_filter_rejected", ticker=f"T{i}") for i in range(6)])
    flow = today._build_decision_flow(
        analysis_id="analysis-flow", synthesis={}, board=[], review_board=[],
        execution_plan={"filtered_examples": examples, "filtered_summary": {"plan_gate": 6}},
        decision_summary={"executable_count": 0, "review_count": 0, "deferred_count": 0},
    )
    assert flow["detail_coverage"] == {
        "status": "sampled", "filtered_total": 6,
        "filtered_materialized": 5, "sample_limit": 5,
    }
    post_filter = next(stage for stage in flow["stages"] if stage["key"] == "post_filter")
    assert post_filter["rejected"] == 6
    assert flow["status"] == "complete"


def _gen(ticker, tier, action_type="buy", **extra):
    """tier_generated 行。この段では account がまだ空であることが重要。"""
    return {
        "analysis_id": "analysis-flow", "stage": "tier_generated", "ticker": ticker,
        "canonical_action_type": action_type, "account": "", "tier": tier,
        "execution_block_reason_codes": [], **extra,
    }


def _picked(ticker, tier, action_type="buy", account="特定"):
    """opus_raw 行。合成で口座が決まるので account が入る。"""
    return {
        "analysis_id": "analysis-flow", "stage": "opus_raw", "ticker": ticker,
        "canonical_action_type": action_type, "account": account, "tier": tier,
        "execution_block_reason_codes": [],
    }


def test_unselected_lists_each_candidate_individually():
    entries = [_gen("AAA", "Long"), _gen("BBB", "Long"), _picked("AAA", "Long")]
    out = today._unselected_candidates(entries)
    assert [u["ticker"] for u in out] == ["BBB"]


def test_unselected_never_contains_a_candidate_that_was_picked():
    # tier_generated は account が空、opus_raw は口座付き。account を識別子に
    # 混ぜると採用された候補が不採用側にも現れ、画面が嘘をつく。
    entries = [_gen("AAA", "Long"), _picked("AAA", "Long", account="NISA成長投資枠")]
    assert today._unselected_candidates(entries) == []


def test_unselected_keeps_the_other_tier_when_only_one_lane_was_picked():
    # XLF は Long と Medium の両レーンから上がる。tier を落とすと2件が1件に
    # 潰れ、Medium が採られたときに Long の不採用が消える。
    entries = [
        _gen("XLF", "Long", "trim"), _gen("XLF", "Medium", "trim"),
        _picked("XLF", "Medium", "trim"),
    ]
    out = today._unselected_candidates(entries)
    assert [(u["ticker"], u["tier"]) for u in out] == [("XLF", "Long")]


def test_unselected_count_matches_the_funnel_drop():
    entries = [_gen(f"T{i}", "Long") for i in range(15)] + [
        _picked("T0", "Long"), _picked("T1", "Long"),
    ]
    assert len(today._unselected_candidates(entries)) == 13


def test_unselected_cancels_one_generated_row_per_pick():
    # 同じキーが2回上がって1回採られたら、残り1件は不採用として残る。
    entries = [_gen("AAA", "Long"), _gen("AAA", "Long"), _picked("AAA", "Long")]
    assert len(today._unselected_candidates(entries)) == 1


def test_unselected_carries_the_detail_the_map_shows_on_hover():
    entries = [_gen("AAA", "ShortSell", "short", confidence_pct=50,
                    estimated_notional_jpy=1234, urgency="low")]
    (row,) = today._unselected_candidates(entries)
    assert row == {
        "ticker": "AAA", "type": "short", "tier": "ShortSell",
        "confidence_pct": 50, "estimated_notional_jpy": 1234, "urgency": "low",
    }


def test_decision_flow_exposes_unselected_alongside_actions():
    action_stage_log.append_entries([
        _gen("AAA", "Long"), _gen("BBB", "Long"),
        _entry("tier_generated"), _entry("opus_raw"), _entry("policy_accepted"),
        _entry("post_filter_final"),
    ])
    flow = today._build_decision_flow(
        analysis_id="analysis-flow", synthesis={}, board=[], review_board=[],
        execution_plan={"filtered_examples": [], "filtered_summary": {}},
        decision_summary=None,
    )
    assert sorted(u["ticker"] for u in flow["unselected"]) == ["AAA", "BBB"]


def test_decision_flow_unselected_is_empty_when_the_log_is_missing():
    flow = today._build_decision_flow(
        analysis_id=None, synthesis={}, board=[], review_board=[],
        execution_plan=None, decision_summary=None,
    )
    assert flow["unselected"] == []


def test_unselected_matches_a_pick_that_switched_lane_and_action():
    # 実データ: V は Long/add と MarginLong/margin_buy で上がり、
    # 合成は Medium/buy として採用した。厳密キーだけだと相殺できず、
    # V が採用側と不採用側の両方に出て件数がファネルと食い違った。
    entries = [
        _gen("V", "Long", "add"), _gen("V", "MarginLong", "margin_buy"),
        _picked("V", "Medium", "buy"),
    ]
    out = today._unselected_candidates(entries)
    assert len(out) == 1


def test_unselected_count_always_equals_generated_minus_picked():
    # 画面の見出し（ファネルの脱落数）と地図のノード数がずれてはいけない。
    entries = [
        _gen("1489.T", "Long"), _gen("XLF", "Long", "trim"), _gen("V", "Long", "add"),
        _gen("SLIM_ORCAN", "Long", "dca"), _gen("XLF", "Medium", "trim"),
        _gen("AMAT", "Medium", "add"), _gen("V", "MarginLong", "margin_buy"),
        _gen("CRNX", "ShortSell", "short"), _gen("U", "ShortSell", "short"),
        _gen("MMSI", "ShortSell", "short"),
        _picked("1489.T", "Long"), _picked("V", "Medium", "buy"),
        _picked("XLF", "Medium", "trim"),
    ]
    assert len(today._unselected_candidates(entries)) == 10 - 3


def test_exact_matches_win_over_loose_ones_regardless_of_order():
    # 緩い規則の採用(V/Medium/buy)が先に来ても、厳密に一致する
    # XLF/Medium/trim の行を食ってはいけない。
    entries = [
        _gen("XLF", "Medium", "trim"), _gen("XLF", "Long", "trim"),
        _picked("XLF", "Long", "trim"), _picked("XLF", "Medium", "trim"),
    ]
    assert today._unselected_candidates(entries) == []


def test_a_pick_never_cancels_a_different_ticker():
    entries = [_gen("AAA", "Long"), _gen("BBB", "Long"), _picked("ZZZ", "Long")]
    out = today._unselected_candidates(entries)
    assert sorted(u["ticker"] for u in out) == ["AAA", "BBB"]

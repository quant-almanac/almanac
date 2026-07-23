import action_stage_log


def test_post_filter_log_keeps_structured_routing_and_readiness(monkeypatch, tmp_path):
    path = tmp_path / "action_stage_log.jsonl"
    monkeypatch.setattr(action_stage_log, "LOG_PATH", path)

    action_stage_log.log_post_filter_final(
        analysis_id="analysis-1",
        as_of="2026-07-23T06:23:00+09:00",
        actions=[{
            "ticker": "AVGO",
            "tier": "Long",
            "type": "trim",
            "execution_account": "特定",
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_investment_type": "long",
            "execution_position_keys": ["AVGO_toku"],
            "execution_readiness": "blocked",
            "execution_block_reasons": [{
                "code": "execution_route_text_conflict",
                "message": "route mismatch",
            }],
        }],
    )

    [row] = action_stage_log.read_entries(path, stages=["post_filter_final"])
    assert row["account"] == "特定"
    assert row["execution_account"] == "特定"
    assert row["execution_owner"] == "husband"
    assert row["execution_broker"] == "rakuten"
    assert row["execution_position_keys"] == ["AVGO_toku"]
    assert row["execution_readiness"] == "blocked"
    assert row["execution_block_reason_codes"] == ["execution_route_text_conflict"]

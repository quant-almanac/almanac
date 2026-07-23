import analyzer


def test_extract_prev_delta_state_reads_current_analysis_shape():
    prev = {
        "portfolio_total": 12_345_678,
        "scenario_key": "BULL",
        "synthesis": {
            "market_meta_snapshot": {
                "vix": 18.4,
                "vix_level": "CALM",
            },
            "dca_signals": {
                "active_tranche": "tranche_2",
            },
        },
    }

    state = analyzer._extract_prev_delta_state(prev)

    assert state == {
        "portfolio_total": 12_345_678.0,
        "vix": 18.4,
        "regime": "BULL",
        "active_tranche": "tranche_2",
    }


def test_extract_prev_delta_state_keeps_legacy_top_level_fallbacks():
    prev = {
        "portfolio_total": {"total_jpy": 10_000_000},
        "market_meta": {"vix": 22.5, "regime": "neutral"},
        "dca_signals": {"active_tranche": "legacy_tranche"},
    }

    state = analyzer._extract_prev_delta_state(prev)

    assert state["portfolio_total"] == 10_000_000.0
    assert state["vix"] == 22.5
    assert state["regime"] == "neutral"
    assert state["active_tranche"] == "legacy_tranche"

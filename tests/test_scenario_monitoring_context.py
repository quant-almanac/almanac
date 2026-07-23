import analyst


def test_scenario_monitoring_context_includes_signals_and_playbook_actions():
    text = analyst._fmt_scenario_monitoring({
        "vix_detail": {
            "level": 16.5,
            "classification": "normal",
            "change_5d": -8.0,
            "decay_from_peak_5d_pct": -10.0,
            "term_structure": "contango",
            "term_ratio": 0.83,
            "fear_greed_label": "GREED",
            "fear_greed_score": 67,
            "yield_spread_10y3m": 0.9,
            "oil_change_5d_pct": -7.5,
            "spy_vs_ma50_pct": 7.0,
            "sector_flows": [{"ticker": "XLK", "return_5d_pct": 2.0, "vs_spy_5d_pct": 1.0}],
        },
        "macro_detail": {"fed_rate": 3.6, "yield_10y": 4.5, "yield_2y": 4.0, "cpi_yoy": 3.9, "unemp_rate": 4.3, "hy_oas_bps": 278},
        "technical_detail": {"market_breadth": {"above_ma50_pct": 72}},
        "active_scenarios": [{
            "id": "war_end",
            "name": "戦争終結ラリー",
            "description": "地政学リスク解消でリスクオン",
            "priority": "high",
            "status": "watching",
            "readiness_pct": 45,
            "signals_met": 3,
            "signals_total": 8,
            "matched_signals": [{"key": "vix", "detail": "vix 16.5 < 18"}],
            "missing_signals": [{"key": "news_keywords", "detail": "No matching headlines"}],
            "playbook_actions": [
                {"phase": "phase_1", "ticker": "SOXL", "allocation_usd": 5000, "reason": "半導体3倍レバ",
                 "technical": {"price": 10, "rsi": 61, "change_5d_pct": 3.1, "change_20d_pct": 8.2,
                               "volume_ratio": 1.4, "composite_signal": "bullish"}},
                {"phase": "phase_1", "ticker": "1489.T", "allocation_jpy": 200000, "reason": "高配当ETF"},
                {"phase": "phase_3", "ticker": "TQQQ", "allocation_usd": 3000, "reason": "加速確認後"},
            ],
            "sell_triggers": ["GLD"],
            "confirmation_required": ["phase_3: momentum acceleration"],
            "first_detected": "2026-05-01T00:00:00",
        }],
        "geo_alerts": [],
        "evaluated_at": "2026-05-23T01:00:00",
    })

    assert "成立シグナル" in text
    assert "未達シグナル" in text
    assert "vix 16.5 < 18" in text
    assert "No matching headlines" in text
    assert "プレイブック候補" in text
    assert "SOXL" in text
    assert "tech price=10" in text
    assert "1489.T ¥200,000" in text
    assert "TQQQ" in text
    assert "売却トリガー" in text
    assert "GLD" in text
    assert "追加確認条件" in text
    assert "momentum acceleration" in text
    assert "セクターフロー" in text
    assert "マクロ:" in text

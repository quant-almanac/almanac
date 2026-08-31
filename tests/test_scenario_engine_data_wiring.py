import json

import scenario_engine


def _bull_pullback_scenario() -> dict:
    return {
        "id": "bull_pullback",
        "name": "強気相場の押し目買い",
        "detect": {
            "news_keywords": [],
            "indicators": {
                "spy_dist_from_ma50_pct": {
                    "condition": "between",
                    "lower": -0.08,
                    "upper": -0.03,
                    "key": "spy_dist_from_ma50_pct",
                }
            },
            "technical": {
                "SPY_above_MA50": {"condition": "true"},
                "regime_bull_confirmed": {"condition": "true"},
            },
        },
        "actions": {
            "phase_1_conservative": {"buy": [{"ticker": "SPY"}]},
            "phase_2_aggressive": {"buy": [{"ticker": "NVDA"}]},
            "phase_3_tactical": {"buy": [{"ticker": "TQQQ"}]},
        },
    }


def test_indicator_reads_spy_ma50_distance_from_market_snapshot():
    scenario = _bull_pullback_scenario()

    rows = scenario_engine._eval_indicators(
        scenario,
        vix_state={},
        macro_state={},
        market_state={"SPY": {"ma50_diff": -5.0}},
    )

    assert rows[0]["key"] == "spy_dist_from_ma50_pct"
    assert rows[0]["matched"] is True
    assert rows[0]["detail"] != scenario_engine.INCONCLUSIVE_DETAIL
    assert "-5.00%" in rows[0]["detail"]


def test_indicator_reports_above_ma50_as_data_not_missing():
    scenario = _bull_pullback_scenario()

    rows = scenario_engine._eval_indicators(
        scenario,
        vix_state={},
        macro_state={},
        market_state={"SPY": {"ma50_diff": 7.06}},
    )

    assert rows[0]["matched"] is False
    assert rows[0]["detail"] != scenario_engine.INCONCLUSIVE_DETAIL
    assert "outside" in rows[0]["detail"]


def test_technical_reads_spy_ma50_and_regime_state_without_technical_ticker():
    scenario = _bull_pullback_scenario()

    rows = scenario_engine._eval_technical(
        scenario,
        tech_state={},
        market_state={"SPY": {"price": 745.64, "ma50": 696.49, "ma50_diff": 7.06}},
        regime_state={"regime": "A_強気", "macro_score": 10, "spy_above": True, "nk_above": True},
    )
    by_key = {row["key"]: row for row in rows}

    assert by_key["SPY_above_MA50"]["matched"] is True
    assert by_key["SPY_above_MA50"]["detail"] != scenario_engine.INCONCLUSIVE_DETAIL
    assert by_key["regime_bull_confirmed"]["matched"] is True
    assert "A_強気" in by_key["regime_bull_confirmed"]["detail"]


def test_evaluate_scenarios_uses_market_snapshot_and_regime_state(tmp_path, monkeypatch):
    playbook_path = tmp_path / "scenario_playbook.json"
    playbook_path.write_text(json.dumps({"scenarios": [_bull_pullback_scenario()]}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "vix_state.json").write_text(json.dumps({"vix": {"level": 16.5}}), encoding="utf-8")
    (tmp_path / "geopolitical_state.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "technical_state.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "macro_state.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "regime_state.json").write_text(json.dumps({
        "regime": "A_強気",
        "macro_score": 10,
        "spy_above": True,
        "nk_above": True,
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "market_snapshot.json").write_text(json.dumps({
        "SPY": {"price": 745.64, "ma50": 696.49, "ma50_diff": 7.06}
    }), encoding="utf-8")
    (tmp_path / "guard_state.json").write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(scenario_engine, "PLAYBOOK_PATH", playbook_path)
    monkeypatch.setattr(scenario_engine, "VIX_STATE_PATH", tmp_path / "vix_state.json")
    monkeypatch.setattr(scenario_engine, "GEO_STATE_PATH", tmp_path / "geopolitical_state.json")
    monkeypatch.setattr(scenario_engine, "TECH_STATE_PATH", tmp_path / "technical_state.json")
    monkeypatch.setattr(scenario_engine, "MACRO_STATE_PATH", tmp_path / "macro_state.json")
    monkeypatch.setattr(scenario_engine, "REGIME_STATE_PATH", tmp_path / "regime_state.json")
    monkeypatch.setattr(scenario_engine, "MARKET_SNAPSHOT_PATH", tmp_path / "market_snapshot.json")
    monkeypatch.setattr(scenario_engine, "GUARD_STATE_PATH", tmp_path / "guard_state.json")
    monkeypatch.setattr(scenario_engine, "SCENARIO_STATE_PATH", tmp_path / "scenario_state.json")

    state = scenario_engine.evaluate_scenarios()
    bull = state["scenarios"]["bull_pullback"]
    details = {row["key"]: row for row in bull["signal_details"]}

    assert details["spy_dist_from_ma50_pct"]["matched"] is False
    assert details["SPY_above_MA50"]["matched"] is True
    assert details["regime_bull_confirmed"]["matched"] is True
    assert "spy_dist_from_ma50_pct" in bull["missing_required_signals"]
    assert bull["status"] == "watching"
    assert bull["recommended_actions"]["phase_1"] == [{"ticker": "SPY"}]
    assert bull["recommended_actions"]["phase_2"] == [{"ticker": "NVDA"}]
    assert bull["recommended_actions"]["phase_3"] == [{"ticker": "TQQQ"}]


def test_ma50_pullback_and_above_ma50_contract_cannot_match_together():
    scenario = _bull_pullback_scenario()
    for diff in (-5.0, 5.0):
        indicator = scenario_engine._eval_indicators(
            scenario,
            vix_state={},
            macro_state={},
            market_state={"SPY": {"ma50_diff": diff}},
        )[0]
        technical = scenario_engine._eval_technical(
            scenario,
            tech_state={},
            market_state={"SPY": {"ma50_diff": diff}},
            regime_state={"regime": "A_強気"},
        )
        above = {row["key"]: row for row in technical}["SPY_above_MA50"]
        assert not (indicator["matched"] and above["matched"])


def test_an_unresolved_technical_row_cannot_satisfy_a_condition():
    """引き継がれた行 (rebuild_unresolved) の指標値は現在値ではない。

    直近の全再計算がその銘柄を取得できず、前回取得分の行をそのまま
    引き継いだ場合、RSI/MACD 等は取得できた時点の値で凍結されている。
    それで条件を成立させると、数日前の oversold で今日の scenario が
    発火する (Codex レビュー round 7 で「RSI 10 < 20 → matched=True」を再現)。
    data_quality_status=blocked と同じ inconclusive 扱いになること。
    """
    scenario = {
        "id": "oversold_entry",
        "detect": {"technical": {"MDB_rsi": {"condition": "below", "threshold": 20,
                                             "ticker": "MDB", "indicator": "rsi"}}},
    }
    stale_row = {"rsi": 10.0, "data_quality_status": "ok", "freshness_status": "fresh", "data_as_of": "2026-08-01"}

    # 印が無ければ従来どおり成立する = 対照群。
    without_marker = scenario_engine._eval_technical(
        scenario, {"tickers": {"MDB": dict(stale_row)}})
    assert without_marker[0]["matched"] is True

    # 同じ値でも、引き継ぎ行なら成立させない。
    with_marker = scenario_engine._eval_technical(
        scenario, {"tickers": {"MDB": {**stale_row, "rebuild_unresolved": True}}})
    assert with_marker[0]["matched"] is False
    assert with_marker[0]["detail"] == scenario_engine.INCONCLUSIVE_DETAIL
    assert with_marker[0]["rebuild_unresolved"] is True


def test_ai_input_omits_indicator_values_for_an_unresolved_row():
    """AI へ渡すテクニカル要約も、引き継ぎ行の指標値を現在値として出さない。

    execution_readiness は risk-increasing 注文しか止めないので、AI の
    売却判断や配分判断はこの入力を通じて古い値の影響を受ける
    (Codex レビュー round 7: 「MDB: RSI=10(oversold)」がそのまま渡っていた)。
    """
    import analyst

    stale_row = {
        "rsi": 10.0, "rsi_signal": "oversold", "macd_histogram": -1.0,
        "bb_pct_b": 0.02, "volume_ratio": 1.0, "composite_score": -80,
        "data_quality_status": "ok", "freshness_status": "fresh", "data_as_of": "2026-08-01",
    }

    # 印が無ければ従来どおり数値が出る = 対照群。
    without_marker = analyst._fmt_technical_state(["MDB"], {"MDB": dict(stale_row)})
    assert "10" in without_marker

    with_marker = analyst._fmt_technical_state(
        ["MDB"], {"MDB": {**stale_row, "rebuild_unresolved": True}})
    assert "判定不能" in with_marker
    assert "oversold" not in with_marker, "凍結された指標シグナルがAIへ渡っている"
    assert "2026-08-01" in with_marker, "基準日は示すべき"


def test_indicator_alias_path_rejects_an_unresolved_row():
    """_eval_indicators → _resolve_ticker_change 経由も未解決行を弾くこと。

    _eval_technical だけを直しても、指標エイリアス経由 (defense_etf_ita 等)
    は別関数なので素通りしていた (Codex レビュー round 8 で
    「ITA rebuild_unresolved + 5日-20% → matched=True」を再現)。
    """
    cond = {"condition": "drop_pct_5d", "threshold": -10}
    stale = {"change_5d_pct": -20.0, "data_quality_status": "ok", "freshness_status": "fresh"}

    # 印が無ければ値が返る = 対照群。
    assert scenario_engine._resolve_ticker_change(
        "defense_etf_ita", cond, {"tickers": {"ITA": dict(stale)}}) == -20.0

    assert scenario_engine._resolve_ticker_change(
        "defense_etf_ita", cond,
        {"tickers": {"ITA": {**stale, "rebuild_unresolved": True}}}) is None


def test_special_conditions_reject_unresolved_and_blocked_rows():
    """特殊条件ハンドラも同じ品質契約に従うこと。

    ewj_outperforms_spy_20d / nikkei_or_topix_above_ma50 は
    data_quality_status=blocked すら見ておらず、引き継がれた凍結値で
    条件が成立していた (Codex レビュー round 8)。
    """
    scenario = {"detect": {"technical": {
        "ewj_outperforms_spy_20d": {"condition": "true"},
    }}}
    ewj_good = {"change_20d_pct": 20.0, "data_quality_status": "ok", "freshness_status": "fresh"}
    spy_flat = {"change_20d_pct": 0.0, "data_quality_status": "ok", "freshness_status": "fresh"}

    # 対照群: 印が無ければ成立する。
    ok = scenario_engine._eval_technical(
        scenario, {"tickers": {"EWJ": dict(ewj_good), "SPY": dict(spy_flat)}})
    assert ok[0]["matched"] is True

    # 引き継がれた EWJ 行では成立させない (market_state にも無いので
    # inconclusive)。
    unresolved = scenario_engine._eval_technical(
        scenario,
        {"tickers": {"EWJ": {**ewj_good, "rebuild_unresolved": True},
                     "SPY": dict(spy_flat)}})
    assert unresolved[0]["matched"] is False
    assert unresolved[0]["detail"] == scenario_engine.INCONCLUSIVE_DETAIL

    # blocked も同様に弾かれる (以前は見ていなかった)。
    blocked = scenario_engine._eval_technical(
        scenario,
        {"tickers": {"EWJ": {**ewj_good, "data_quality_status": "blocked"},
                     "SPY": dict(spy_flat)}})
    assert blocked[0]["matched"] is False


def test_nikkei_ma50_condition_rejects_an_unresolved_index_row():
    scenario = {"detect": {"technical": {
        "nikkei_or_topix_above_ma50": {"condition": "true"},
    }}}
    above = {"ma50_diff": 10.0, "data_quality_status": "ok", "freshness_status": "fresh"}

    ok = scenario_engine._eval_technical(
        scenario, {"tickers": {"1306.T": dict(above)}})
    assert ok[0]["matched"] is True

    unresolved = scenario_engine._eval_technical(
        scenario,
        {"tickers": {"1306.T": {**above, "rebuild_unresolved": True}}})
    assert unresolved[0]["matched"] is False


def test_the_ai_scenario_snapshot_omits_values_for_an_unusable_row():
    """scenario action / sell trigger に付く技術スナップショットも、
    使えない行では数値を出さないこと。_fmt_technical_state を直しても
    こちらが素通しなら凍結値が AI へ届く (Codex レビュー round 8)。
    """
    from analyst.data_gatherer import technical_snapshot_for_ai

    row = {"price": 100.0, "rsi": 10.0, "change_5d_pct": -20.0,
           "composite_signal": "bearish", "data_quality_status": "ok", "freshness_status": "fresh",
           "data_as_of": "2026-08-01"}

    # 対照群: 使える行なら従来どおり数値が出る。
    ok = technical_snapshot_for_ai({"tickers": {"MDB": dict(row)}}, "MDB")
    assert ok["rsi"] == 10.0 and ok["price"] == 100.0

    for bad, expected_reason in [
        ({**row, "rebuild_unresolved": True}, "rebuild_unresolved"),
        ({**row, "data_quality_status": "blocked"}, "data_quality_blocked"),
    ]:
        snap = technical_snapshot_for_ai({"tickers": {"MDB": bad}}, "MDB")
        assert snap["usable"] is False
        assert snap["reason"] == expected_reason
        assert snap["data_as_of"] == "2026-08-01"
        # 指標値が1つも漏れていないこと。
        for leaked in ("price", "rsi", "change_5d_pct", "composite_signal"):
            assert leaked not in snap, f"{leaked} が AI スナップショットへ漏れている"

"""
tests/test_policy_engine.py — P1-17 + P1-21 Policy Engine
"""
import kabu_mini_eligibility
import policy_engine as pe


def _action(type_="buy", urgency="high", ticker="AAPL"):
    return {"type": type_, "urgency": urgency, "ticker": ticker, "tier": "Medium"}


# ────────────────────────────────────────────────────────
# VaR budget
# ────────────────────────────────────────────────────────

def test_var_budget_rejects_buy_when_exceeded():
    ctx = pe.PolicyContext(var_1d_95=0.020)  # 2% > threshold 1.2%
    d = pe.apply_policy_gate([_action("buy")], ctx)
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_var_budget"
    assert "VaR" in d.rejected[0]["reason"]


def test_var_budget_passes_when_within():
    ctx = pe.PolicyContext(var_1d_95=0.008)  # 0.8% < 1.2%
    d = pe.apply_policy_gate([_action("buy")], ctx)
    assert len(d.accepted) == 1
    assert len(d.rejected) == 0


def test_var_budget_ignores_sell():
    ctx = pe.PolicyContext(var_1d_95=0.020)
    d = pe.apply_policy_gate([_action("sell")], ctx)
    assert len(d.accepted) == 1  # sell は対象外


def test_ledger_integrity_failure_rejects_executable_actions():
    ctx = pe.PolicyContext(
        ledger_integrity_ok=False,
        ledger_blocking_issue_count=24,
        ledger_unapplied_executed_count=22,
    )
    actions = [_action("buy", ticker="META"), _action("trim", ticker="GLD")]

    d = pe.apply_policy_gate(actions, ctx)

    assert len(d.accepted) == 0
    assert len(d.rejected) == 2
    assert {r["rule"] for r in d.rejected} == {"_rule_ledger_integrity"}
    assert "Ledger Integrity ok=False" in d.rejected[0]["reason"]


def test_ledger_integrity_failure_rejects_reduce_as_executable_action():
    ctx = pe.PolicyContext(
        ledger_integrity_ok=False,
        ledger_blocking_issue_count=1,
        ledger_unapplied_executed_count=0,
    )

    d = pe.apply_policy_gate([_action("reduce")], ctx)

    assert len(d.accepted) == 0
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_ledger_integrity"


def test_ledger_integrity_unknown_does_not_block():
    ctx = pe.PolicyContext(ledger_integrity_ok=None)
    d = pe.apply_policy_gate([_action("buy")], ctx)
    assert len(d.accepted) == 1


# ────────────────────────────────────────────────────────
# DD stage
# ────────────────────────────────────────────────────────

def test_dd_block_threshold_rejects_buy():
    ctx = pe.PolicyContext(current_dd=-0.10)  # -10% < -8%
    d = pe.apply_policy_gate([_action("buy")], ctx)
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_dd_stage"


def test_actual_dd_stage_blocks_buy_when_synthetic_dd_missing():
    ctx = pe.PolicyContext(current_dd=None, actual_dd_stage="stage_1")
    d = pe.apply_policy_gate([_action("buy")], ctx)
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_dd_stage"


def test_actual_dd_stage_allows_dca_ladder_exception_half_size():
    ctx = pe.PolicyContext(
        current_dd=None,
        actual_dd_stage="stage_2",
        allow_dca_tranche=True,
        actual_trading_allowed=True,
        dca_active_tranche="T2",
    )
    action = {
        "type": "dca",
        "source": "dca_ladder",
        "ticker": "GLD",
        "urgency": "high",
        "amount_hint": "4株",
    }

    d = pe.apply_policy_gate([action], ctx)

    assert len(d.rejected) == 0
    assert len(d.accepted) == 1
    accepted = d.accepted[0]
    assert accepted["urgency"] == "medium"
    assert accepted["policy_dca_dd_exception"] is True
    assert accepted["policy_dca_active_tranche"] == "T2"
    assert accepted["policy_size_adj"] == 0.5
    assert accepted["amount_hint"] == "2株"


def test_dca_ladder_exception_does_not_apply_when_trading_stopped():
    ctx = pe.PolicyContext(
        actual_dd_stage="stage_3",
        allow_dca_tranche=True,
        actual_trading_allowed=False,
    )
    action = {"type": "dca", "source": "dca_ladder", "ticker": "GLD", "amount_hint": "4株"}

    d = pe.apply_policy_gate([action], ctx)

    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_dd_stage"
    assert "DCA 例外も停止" in d.rejected[0]["reason"]


def test_dca_ladder_exception_requires_confirmed_trading_allowed():
    """trading_allowed が欠落 (None) の場合は fail-closed で DCA 例外を許さない。"""
    ctx = pe.PolicyContext(
        actual_dd_stage="monthly_block",
        allow_dca_tranche=True,
        actual_trading_allowed=None,
    )
    action = {"type": "dca", "source": "dca_ladder", "ticker": "GLD", "amount_hint": "4株"}

    d = pe.apply_policy_gate([action], ctx)

    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_dd_stage"
    assert "DCA 例外も停止" in d.rejected[0]["reason"]


def test_dca_without_ladder_source_still_blocked_by_actual_dd_stage():
    ctx = pe.PolicyContext(actual_dd_stage="stage_1", allow_dca_tranche=True)
    d = pe.apply_policy_gate([_action("dca")], ctx)
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_dd_stage"


def test_dd_caution_modifies_to_medium_with_size_adj():
    ctx = pe.PolicyContext(current_dd=-0.06)  # -6% (caution band)
    d = pe.apply_policy_gate([_action("buy", urgency="high")], ctx)
    assert len(d.accepted) == 1
    a = d.accepted[0]
    assert a["urgency"] == "medium"
    assert a["policy_size_adj"] == 0.5
    assert len(d.modified) == 1


def test_dd_no_change_when_safe():
    ctx = pe.PolicyContext(current_dd=-0.02)
    d = pe.apply_policy_gate([_action("buy", urgency="high")], ctx)
    assert d.accepted[0]["urgency"] == "high"


def test_actual_dd_stage_ok_overrides_numeric_current_dd():
    """実損益ガードが ok なら、数値 current_dd（単位誤読/合成系列）で buy を止めない。

    2026-07-07 事故の回帰テスト: guard は daily -0.1% / stage=ok なのに、
    percent 表記 -0.1 が小数 (-10%) と誤読され buy 全滅した。stage=ok が権威。
    """
    ctx = pe.PolicyContext(current_dd=-0.10, actual_dd_stage="ok")
    d = pe.apply_policy_gate([_action("buy")], ctx)
    assert len(d.rejected) == 0
    assert len(d.accepted) == 1
    assert d.accepted[0]["urgency"] == "high"  # caution 降格もされない


def test_actual_dd_stage_caution_halves_size_deterministically():
    """実損益ガード caution → 数値再判定なしでサイズ半減 + urgency 降格。"""
    ctx = pe.PolicyContext(current_dd=None, actual_dd_stage="caution")
    d = pe.apply_policy_gate([_action("buy", urgency="high")], ctx)
    assert len(d.rejected) == 0
    assert d.accepted[0]["urgency"] == "medium"
    assert d.accepted[0]["policy_size_adj"] == 0.5


def test_build_context_actual_dd_small_percent_not_misread_as_decimal():
    """actual_current_dd は常に percent 表記契約 — -0.1 は -0.1% (= -0.001) であり -10% ではない。"""
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"actual_current_dd": -0.1, "actual_dd_stage": "ok"},
    )
    assert abs(ctx.current_dd - (-0.001)) < 1e-12
    d = pe.apply_policy_gate([_action("buy")], ctx)
    assert len(d.rejected) == 0


def test_build_context_actual_dd_caution_band_percent():
    """-6.0 (percent) は -6% として caution 帯に入る (従来動作の維持)。"""
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"actual_current_dd": -6.0},
    )
    assert abs(ctx.current_dd - (-0.06)) < 1e-9


# ────────────────────────────────────────────────────────
# Leverage health
# ────────────────────────────────────────────────────────

def test_leverage_warning_blocks_margin_buy():
    ctx = pe.PolicyContext(leverage_status="warning")
    d = pe.apply_policy_gate([_action("margin_buy")], ctx)
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_leverage_block"


def test_leverage_warning_blocks_new_short_but_not_cover():
    ctx = pe.PolicyContext(leverage_status="warning")
    d = pe.apply_policy_gate([_action("short"), _action("cover")], ctx)
    assert len(d.rejected) == 1
    assert d.rejected[0]["action"]["type"] == "short"
    assert d.accepted[0]["type"] == "cover"


def test_leverage_safe_passes_margin_buy():
    ctx = pe.PolicyContext(leverage_status="safe")
    d = pe.apply_policy_gate([_action("margin_buy")], ctx)
    assert len(d.accepted) == 1


def test_leverage_does_not_affect_regular_buy():
    ctx = pe.PolicyContext(leverage_status="emergency")
    d = pe.apply_policy_gate([_action("buy")], ctx)
    # 通常 buy はパスする (他のルールに該当なければ)
    assert len(d.accepted) == 1


# ────────────────────────────────────────────────────────
# Earnings blackout
# ────────────────────────────────────────────────────────

def test_earnings_blackout_rejects_buy_for_listed_ticker():
    ctx = pe.PolicyContext(earnings_blackout={"AAPL", "NVDA"})
    d = pe.apply_policy_gate([_action("buy", ticker="AAPL")], ctx)
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_earnings_blackout"


def test_earnings_blackout_allows_explicit_ai_bounded_event_trade():
    ctx = pe.PolicyContext(earnings_blackout={"AAPL"})
    action = {
        **_action("buy", ticker="AAPL"),
        "confidence_pct": 78,
        "earnings_event_trade": True,
        "earnings_event_reason": "決算ガイダンス上方修正をイベントとして小ロットで取る",
    }
    d = pe.apply_policy_gate([action], ctx)

    assert len(d.accepted) == 1
    assert d.accepted[0]["ai_bounded_gate"] == "earnings_blackout"
    assert d.accepted[0]["policy_earnings_blackout_override"] is True
    assert d.accepted[0]["provisional_decision"] is True
    assert len(d.modified) == 1


def test_earnings_blackout_rejects_event_trade_without_high_confidence():
    ctx = pe.PolicyContext(earnings_blackout={"AAPL"})
    action = {
        **_action("buy", ticker="AAPL"),
        "confidence_pct": 70,
        "earnings_event_trade": True,
        "earnings_event_reason": "決算イベント",
    }
    d = pe.apply_policy_gate([action], ctx)

    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_earnings_blackout"


def test_earnings_blackout_allows_sell():
    ctx = pe.PolicyContext(earnings_blackout={"AAPL"})
    d = pe.apply_policy_gate([_action("sell", ticker="AAPL")], ctx)
    assert len(d.accepted) == 1


def test_earnings_blackout_allows_unrelated_ticker():
    ctx = pe.PolicyContext(earnings_blackout={"AAPL"})
    d = pe.apply_policy_gate([_action("buy", ticker="MSFT")], ctx)
    assert len(d.accepted) == 1


# ────────────────────────────────────────────────────────
# VIX extreme
# ────────────────────────────────────────────────────────

def test_vix_capitulation_rejects_margin_buy():
    ctx = pe.PolicyContext(vix=42.0)
    d = pe.apply_policy_gate([_action("margin_buy")], ctx)
    assert len(d.rejected) == 1


def test_vix_capitulation_downgrades_high_buy():
    ctx = pe.PolicyContext(vix=42.0)
    d = pe.apply_policy_gate([_action("buy", urgency="high")], ctx)
    assert len(d.accepted) == 1
    assert d.accepted[0]["urgency"] == "medium"
    assert d.accepted[0]["policy_vix_downgraded"] is True


# ────────────────────────────────────────────────────────
# Freshness
# ────────────────────────────────────────────────────────

def test_freshness_downgrades_high_to_medium():
    ctx = pe.PolicyContext(data_freshness=0.5)
    d = pe.apply_policy_gate([_action("buy", urgency="high")], ctx)
    assert d.accepted[0]["urgency"] == "medium"
    assert d.accepted[0]["policy_freshness_downgraded"] is True


def test_freshness_does_not_affect_medium():
    ctx = pe.PolicyContext(data_freshness=0.5)
    d = pe.apply_policy_gate([_action("buy", urgency="medium")], ctx)
    assert d.accepted[0]["urgency"] == "medium"
    assert "policy_freshness_downgraded" not in d.accepted[0]


def test_cvar_unstable_blocks_margin_buy():
    ctx = pe.PolicyContext(cvar_unstable=True)
    d = pe.apply_policy_gate([_action("margin_buy")], ctx)
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_cvar_unstable"


def test_cvar_unstable_downgrades_regular_buy():
    ctx = pe.PolicyContext(cvar_unstable=True)
    d = pe.apply_policy_gate([_action("buy", urgency="high")], ctx)
    assert len(d.accepted) == 1
    assert d.accepted[0]["urgency"] == "medium"
    assert d.accepted[0]["policy_size_adj"] == 0.5
    assert d.accepted[0]["policy_cvar_unstable_downgraded"] is True


def test_cvar_unstable_tail_small_still_blocks_margin_buy():
    # 実データはあるがテールが薄い (tail_small_sample) → 従来どおり margin_buy は hard reject。
    ctx = pe.PolicyContext(cvar_unstable=True, cvar_reason="tail_small_sample")
    d = pe.apply_policy_gate([_action("margin_buy")], ctx)
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"] == "_rule_cvar_unstable"


def test_cvar_insufficient_clean_history_softens_margin_buy():
    # P1-2: クリーン履歴不足は恒久ブロックを避け、margin_buy も half-size + 降格に緩和。
    ctx = pe.PolicyContext(cvar_unstable=True, cvar_reason="insufficient_clean_history")
    d = pe.apply_policy_gate([_action("margin_buy", urgency="high")], ctx)
    assert len(d.rejected) == 0
    assert len(d.accepted) == 1
    assert d.accepted[0]["urgency"] == "medium"
    assert d.accepted[0]["policy_size_adj"] == 0.5
    assert d.accepted[0]["policy_cvar_unstable_downgraded"] is True


# ────────────────────────────────────────────────────────
# Composition: multiple rules together
# ────────────────────────────────────────────────────────

def test_multiple_rules_compose_dd_and_freshness():
    """DD caution + freshness 低下 → 両方の修正が適用される (順序: dd_stage → freshness)。"""
    ctx = pe.PolicyContext(current_dd=-0.06, data_freshness=0.5)
    d = pe.apply_policy_gate([_action("buy", urgency="high")], ctx)
    assert len(d.accepted) == 1
    a = d.accepted[0]
    # dd_stage が先に urgency=medium に降格、policy_size_adj=0.5 を付与
    # freshness rule は urgency!=high になっているため作動しない
    assert a["urgency"] == "medium"
    assert a["policy_size_adj"] == 0.5


def test_reject_short_circuits_subsequent_rules():
    """VaR で reject されたら後続ルールは評価されない。"""
    ctx = pe.PolicyContext(var_1d_95=0.020, current_dd=-0.06)
    d = pe.apply_policy_gate([_action("buy")], ctx)
    assert len(d.rejected) == 1
    # var_budget が最初に発火
    assert d.rejected[0]["rule"] == "_rule_var_budget"


def test_empty_input_returns_empty_decision():
    ctx = pe.PolicyContext()
    d = pe.apply_policy_gate([], ctx)
    assert d.accepted == []
    assert d.rejected == []
    assert d.modified == []


def test_non_dict_actions_are_skipped():
    ctx = pe.PolicyContext()
    d = pe.apply_policy_gate([None, "not-a-dict", _action("buy")], ctx)
    assert len(d.accepted) == 1


# ────────────────────────────────────────────────────────
# Context builder
# ────────────────────────────────────────────────────────

def test_build_context_from_pct_format():
    """risk["var_95"] が % 単位 (0.8 = 0.8%) のとき小数化される。"""
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"var_95": 0.8, "current_dd": -5.0},
        macro={"vix": 25.0},
        leverage_health={"status": "safe"},
        freshness_score=0.85,
    )
    assert abs(ctx.var_1d_95 - 0.008) < 1e-9
    assert abs(ctx.current_dd - (-0.05)) < 1e-9
    assert ctx.vix == 25.0
    assert ctx.leverage_status == "safe"
    assert ctx.data_freshness == 0.85


def test_build_context_accepts_decimal_format():
    """risk["var_95"] が小数 (0.008) のときそのまま使う。"""
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"var_95": 0.008, "current_dd": -0.05},
    )
    assert abs(ctx.var_1d_95 - 0.008) < 1e-9
    assert abs(ctx.current_dd - (-0.05)) < 1e-9


def test_build_context_handles_missing_fields():
    ctx = pe.build_context_from_synthesis_inputs()
    assert ctx.var_1d_95 is None
    assert ctx.current_dd is None
    assert ctx.vix is None
    assert ctx.leverage_status is None


def test_build_context_includes_portfolio_integrity_and_cvar_unstable():
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"cvar_unstable": True, "cvar_95": 2.5},
        portfolio_integrity={
            "ok": False,
            "blocking_issue_count": 3,
            "summary": {"unapplied_executed_count": 2},
        },
    )
    assert ctx.cvar_unstable is True
    assert ctx.cvar_1d_95 == 0.025
    assert ctx.ledger_integrity_ok is False
    assert ctx.ledger_blocking_issue_count == 3
    assert ctx.ledger_unapplied_executed_count == 2


def test_build_context_prefers_actual_dd_over_synthetic_current_dd():
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"current_dd": None, "actual_current_dd": -8.5, "actual_dd_stage": "stage_1"}
    )
    assert ctx.current_dd == -0.085
    assert ctx.actual_dd_stage == "stage_1"


def test_build_context_relaxes_var_threshold_in_bull_regime(monkeypatch):
    """BULL/A_強気 + calm VIX should allow buys through VaR, then DD caution sizes down."""
    monkeypatch.delenv("POLICY_VAR_THRESHOLD", raising=False)
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"var_95": 1.82, "current_dd": -5.88},
        macro={"vix": 18.0, "scenario_key": "BULL", "regime": "A_強気"},
    )
    assert abs(ctx.var_threshold - 0.020) < 1e-9

    d = pe.apply_policy_gate([_action("buy", urgency="high")], ctx)
    assert len(d.rejected) == 0
    assert len(d.accepted) == 1
    assert d.accepted[0]["urgency"] == "medium"
    assert d.accepted[0]["policy_size_adj"] == 0.5


def test_build_context_uses_normal_var_threshold(monkeypatch):
    monkeypatch.delenv("POLICY_VAR_THRESHOLD", raising=False)
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"var_95": 1.50},
        macro={"vix": 22.0, "scenario_key": "BASE", "regime": "B_通常"},
    )
    assert abs(ctx.var_threshold - 0.016) < 1e-9


def test_build_context_uses_stress_var_threshold(monkeypatch):
    monkeypatch.delenv("POLICY_VAR_THRESHOLD", raising=False)
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"var_95": 1.50, "actual_dd_stage": "stage_1"},
        macro={"vix": 31.0, "scenario_key": "BASE", "regime": "B_通常"},
    )
    assert abs(ctx.var_threshold - 0.012) < 1e-9


def test_var_threshold_env_override_is_capped_by_absolute_max(monkeypatch):
    monkeypatch.setenv("POLICY_VAR_THRESHOLD", "0.050")
    monkeypatch.delenv("POLICY_VAR_MAX_THRESHOLD", raising=False)
    ctx = pe.build_context_from_synthesis_inputs(risk={"var_95": 1.0})
    assert abs(ctx.var_threshold - 0.023) < 1e-9


def test_env_var_threshold_override_wins(monkeypatch):
    monkeypatch.setenv("POLICY_VAR_THRESHOLD", "0.012")
    ctx = pe.build_context_from_synthesis_inputs(
        risk={"var_95": 1.82},
        macro={"vix": 18.0, "scenario_key": "BULL", "regime": "A_強気"},
    )
    assert abs(ctx.var_threshold - 0.012) < 1e-9


# ────────────────────────────────────────────────────────
# Codex P1 #5 — fail-closed gate (exception / unknown verdict / unknown type)
# ────────────────────────────────────────────────────────

def test_rule_exception_fails_closed_to_reject():
    def _boom(action, ctx):
        raise RuntimeError("rule bug")
    saved = pe.RULES
    pe.RULES = [_boom]
    try:
        d = pe.apply_policy_gate([_action("buy")], pe.PolicyContext())
    finally:
        pe.RULES = saved
    assert len(d.accepted) == 0
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"].startswith("rule_error:")


def test_unknown_verdict_fails_closed_to_reject():
    def _weird(action, ctx):
        return ("frobnicate", "???")
    saved = pe.RULES
    pe.RULES = [_weird]
    try:
        d = pe.apply_policy_gate([_action("buy")], pe.PolicyContext())
    finally:
        pe.RULES = saved
    assert len(d.accepted) == 0
    assert len(d.rejected) == 1
    assert d.rejected[0]["rule"].startswith("unknown_verdict:")


def test_unknown_action_type_rejected():
    d = pe.apply_policy_gate([_action("frobnicate")], pe.PolicyContext())
    assert len(d.accepted) == 0
    assert d.rejected[0]["rule"] == "unknown_action_type"


def test_reduce_action_passes_known_action_type_gate():
    d = pe.apply_policy_gate([_action("reduce")], pe.PolicyContext())
    assert len(d.accepted) == 1
    assert len(d.rejected) == 0


def test_hold_action_passes_known_noop():
    d = pe.apply_policy_gate([_action("hold")], pe.PolicyContext())
    assert len(d.accepted) == 1


# ────────────────────────────────────────────────────────
# Codex P1 #6 — policy_size_adj must hit real quantities
# ────────────────────────────────────────────────────────

def test_size_adj_applied_to_real_quantities():
    ctx = pe.PolicyContext(current_dd=-0.06)  # caution (≤ -5%, > -8%) → size 0.5x
    a = _action("buy")
    a["amount_hint"] = "4株"
    a["shares"] = 4
    d = pe.apply_policy_gate([a], ctx)
    assert len(d.accepted) == 1
    acc = d.accepted[0]
    assert acc["policy_size_adj"] == 0.5
    assert acc["shares"] == 2
    assert acc["amount_hint"] == "2株"
    assert "policy_size_applied" in acc


def test_size_adj_collapse_rejects_sub_share():
    ctx = pe.PolicyContext(current_dd=-0.06)
    a = _action("buy")
    a["shares"] = 1
    a["amount_hint"] = "1株"
    d = pe.apply_policy_gate([a], ctx)
    assert len(d.accepted) == 0
    assert d.rejected[0]["rule"] == "policy_size_collapsed"


def test_size_adj_yen_amount_rounds_no_collapse():
    ctx = pe.PolicyContext(current_dd=-0.06)
    a = _action("buy")
    a["amount_hint"] = "¥150,000"
    d = pe.apply_policy_gate([a], ctx)
    assert len(d.accepted) == 1
    assert d.accepted[0]["amount_hint"] == "¥75000"


def test_size_adj_noop_when_no_reduction():
    ctx = pe.PolicyContext()  # no rule sets size_adj
    a = _action("buy")
    a["shares"] = 3
    d = pe.apply_policy_gate([a], ctx)
    assert len(d.accepted) == 1
    assert d.accepted[0]["shares"] == 3
    assert "policy_size_applied" not in d.accepted[0]


# Codex re-review #6 — 日本株は単元(100株)まで policy 内で確定し後段で増額されない

def test_size_adj_jp_100_share_halved_rejects():
    """100株×0.5=50株=0単元 → 発注不能で reject (後段で100株へ戻さない)。"""
    ctx = pe.PolicyContext(current_dd=-0.06)  # caution → 0.5x
    a = _action("buy", ticker="7203.T")
    a["amount_hint"] = "100株"
    a["shares"] = 100
    d = pe.apply_policy_gate([a], ctx)
    assert len(d.accepted) == 0
    assert d.rejected[0]["rule"] == "policy_size_collapsed"


def test_size_adj_jp_200_share_halved_to_one_unit():
    """200株×0.5=100株=1単元 → 100株で確定し policy_size_final を立てる。"""
    ctx = pe.PolicyContext(current_dd=-0.06)
    a = _action("buy", ticker="7203.T")
    a["amount_hint"] = "200株"
    a["shares"] = 200
    d = pe.apply_policy_gate([a], ctx)
    assert len(d.accepted) == 1
    acc = d.accepted[0]
    assert acc["amount_hint"] == "100株"
    assert acc["shares"] == 100
    assert acc["policy_size_final"] is True


def test_size_adj_jpx_etfs_use_official_units() -> None:
    ctx = pe.PolicyContext(current_dd=-0.06)
    high_dividend = _action("buy", ticker="1489.T")
    high_dividend.update({"amount_hint": "17口", "shares": 17})
    topix = _action("buy", ticker="1306.T")
    topix.update({"amount_hint": "30口", "shares": 30})

    high_dividend_result = pe.apply_policy_gate([high_dividend], ctx).accepted[0]
    topix_result = pe.apply_policy_gate([topix], ctx).accepted[0]

    assert high_dividend_result["amount_hint"] == "8口"
    assert high_dividend_result["shares"] == 8
    assert topix_result["amount_hint"] == "10口"
    assert topix_result["shares"] == 10


def test_size_adj_jp_comma_quantity_floors_to_lot_consistently():
    """Codex re-re-review #6: '1,100株' はカンマでも数量扱い → 100株単元 floor。
    amount_hint と shares が同じ単元結果 (500) に揃う。"""
    ctx = pe.PolicyContext(current_dd=-0.06)  # caution → 0.5x
    a = _action("buy", ticker="7203.T")
    a["amount_hint"] = "1,100株"
    a["shares"] = 1100
    d = pe.apply_policy_gate([a], ctx)
    assert len(d.accepted) == 1
    acc = d.accepted[0]
    assert acc["amount_hint"] == "500株"   # 1100*0.5=550 → 100株単元 floor = 500
    assert acc["shares"] == 500
    assert acc["shares"] % 100 == 0
    assert acc["policy_size_final"] is True


def test_size_adj_jp_kabu_mini_cash_buy_uses_single_share_lot(monkeypatch):
    """かぶミニ指定の日本株現物 buy は 100株単元ではなく1株単位で縮小する。"""
    monkeypatch.setattr(kabu_mini_eligibility, "is_kabu_mini_eligible", lambda ticker, channel=None: ticker == "7203.T")
    ctx = pe.PolicyContext(current_dd=-0.06)  # caution → 0.5x
    a = _action("buy", ticker="7203.T")
    a["amount_hint"] = "20株"
    a["shares"] = 20
    a["execution_channel"] = "rakuten_kabu_mini_open"

    d = pe.apply_policy_gate([a], ctx)

    assert len(d.accepted) == 1
    acc = d.accepted[0]
    assert acc["amount_hint"] == "10株"
    assert acc["shares"] == 10
    assert acc["policy_size_final"] is True


def test_size_adj_jp_kabu_mini_requires_local_eligibility():
    """execution_channelだけではかぶミニ扱いにしない。未確認なら100株単元でfail-closed。"""
    ctx = pe.PolicyContext(current_dd=-0.06)
    a = _action("buy", ticker="7203.T")
    a["amount_hint"] = "20株"
    a["shares"] = 20
    a["execution_channel"] = "rakuten_kabu_mini_open"

    d = pe.apply_policy_gate([a], ctx)

    assert len(d.accepted) == 0
    assert d.rejected[0]["rule"] == "policy_size_collapsed"


def test_size_adj_jp_non_kabu_mini_sub_lot_rejects_instead_of_zero_share():
    """通常の日本株20株は100株未満なので、0株に縮小して通さずrejectする。"""
    ctx = pe.PolicyContext(current_dd=-0.06)
    a = _action("buy", ticker="7203.T")
    a["amount_hint"] = "20株"
    a["shares"] = 20

    d = pe.apply_policy_gate([a], ctx)

    assert len(d.accepted) == 0
    assert d.rejected[0]["rule"] == "policy_size_collapsed"

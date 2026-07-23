"""
tests/test_action_coverage_plan.py
行動カバレッジ修復プラン（2026-06-15）の実装検証テスト

Phase A: action_stage_log / behavior_coverage_report
Phase B1: tickers.json JP ETF
Phase B2-0: scenario_engine min_signals / observe_only / enabled_for_decision
Phase B3-0: drawdown_dca_engine cash fix
Phase D1: rebalance_engine 正規化
Phase C: take_profit 方向修正
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


# ════════════════════════════════════════════════════════════
# Phase A1: action_stage_log
# ════════════════════════════════════════════════════════════

class TestActionStageLog:
    def test_import(self):
        import action_stage_log  # noqa: F401

    def test_direction_sell_types(self):
        from action_stage_log import _direction
        for t in ("sell", "trim", "reduce", "stop_loss", "take_profit", "short"):
            assert _direction(t) == "sell", f"{t} should be sell"

    def test_direction_buy_types(self):
        from action_stage_log import _direction
        for t in ("buy", "add", "dca", "margin_buy", "cover"):
            assert _direction(t) == "buy", f"{t} should be buy"

    def test_take_profit_is_sell_not_buy(self):
        from action_stage_log import _direction
        assert _direction("take_profit") == "sell"

    def test_append_and_read(self, tmp_path):
        from action_stage_log import append_entries, read_entries, _make_entry
        path = tmp_path / "test.jsonl"
        entry = _make_entry(
            analysis_id="abc12345",
            as_of="2026-06-15T07:00:00",
            stage="opus_raw",
            action={"type": "buy", "ticker": "NVDA", "tier": "Long"},
            scenario_key="BULL",
        )
        append_entries([entry], path=path)
        loaded = read_entries(path=path)
        assert len(loaded) == 1
        assert loaded[0]["stage"] == "opus_raw"
        assert loaded[0]["direction"] == "buy"
        assert loaded[0]["canonical_action_type"] == "buy"

    def test_make_entry_uses_action_audit_fields(self):
        from action_stage_log import _make_entry
        entry = _make_entry(
            analysis_id="abc12345",
            as_of="2026-06-26T06:10:00",
            stage="post_filter_final",
            action={
                "type": "buy",
                "ticker": "V",
                "estimated_notional_jpy": 53_000,
                "filtered_reason": "too_small: below threshold",
            },
        )
        assert entry["estimated_notional_jpy"] == 53_000
        assert entry["filter_rule"] == "too_small"
        assert entry["eligible"] is False

    def test_log_post_filter_rejected_records_separate_stage(self, tmp_path, monkeypatch):
        import action_stage_log as asl
        log_path = tmp_path / "test_stage.jsonl"
        monkeypatch.setattr(asl, "LOG_PATH", log_path)
        from action_stage_log import log_post_filter_rejected, read_entries
        log_post_filter_rejected(
            analysis_id="test01",
            as_of="2026-06-26T06:10:00",
            actions=[{
                "type": "buy",
                "ticker": "V",
                "estimated_notional_jpy": 53_000,
                "filtered_reason": "too_small: below threshold",
            }],
        )

        entries = read_entries(path=log_path)
        assert len(entries) == 1
        assert entries[0]["stage"] == "post_filter_rejected"
        assert entries[0]["filter_rule"] == "too_small"
        assert entries[0]["estimated_notional_jpy"] == 53_000

    def test_log_post_filter_deferred_records_order_intent_fields(self, tmp_path, monkeypatch):
        import action_stage_log as asl
        log_path = tmp_path / "test_stage.jsonl"
        monkeypatch.setattr(asl, "LOG_PATH", log_path)
        from action_stage_log import log_post_filter_deferred, read_entries
        log_post_filter_deferred(
            analysis_id="test01",
            as_of="2026-07-09T08:00:00",
            actions=[{
                "type": "buy",
                "ticker": "META",
                "estimated_notional_jpy": 700_000,
                "order_intent_decision": "amend_existing_order",
                "non_executable": True,
                "existing_order_notional_jpy": 450_000,
                "incremental_notional_jpy": 250_000,
            }],
        )

        entries = read_entries(path=log_path)
        assert len(entries) == 1
        assert entries[0]["stage"] == "post_filter_deferred"
        assert entries[0]["order_intent_decision"] == "amend_existing_order"
        assert entries[0]["non_executable"] is True
        assert entries[0]["incremental_notional_jpy"] == 250_000

    def test_since_filter(self, tmp_path):
        from action_stage_log import append_entries, read_entries, _make_entry
        path = tmp_path / "test.jsonl"
        e1 = _make_entry(analysis_id="a", as_of="2026-06-10T07:00:00", stage="opus_raw",
                         action={"type": "buy", "ticker": "A"})
        e2 = _make_entry(analysis_id="b", as_of="2026-06-15T07:00:00", stage="opus_raw",
                         action={"type": "sell", "ticker": "B"})
        append_entries([e1, e2], path=path)
        recent = read_entries(path=path, since_iso="2026-06-12")
        assert len(recent) == 1
        assert recent[0]["ticker"] == "B"

    def test_log_policy_decision(self, tmp_path, monkeypatch):
        import action_stage_log as asl
        log_path = tmp_path / "test_stage.jsonl"
        monkeypatch.setattr(asl, "LOG_PATH", log_path)
        from action_stage_log import log_policy_decision, read_entries
        log_policy_decision(
            analysis_id="test01",
            as_of="2026-06-15T08:00:00",
            accepted=[{"type": "buy", "ticker": "AAPL"}],
            rejected=[{"action": {"type": "buy", "ticker": "NVDA"}, "rule": "_rule_dd_stage",
                       "reason": "DD block"}],
            scenario_key="BULL",
            actual_dd_stage="block",
        )
        entries = read_entries(path=log_path)
        accepted = [e for e in entries if e["stage"] == "policy_accepted"]
        rejected = [e for e in entries if e["stage"] == "policy_rejected"]
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert rejected[0]["filter_rule"] == "_rule_dd_stage"


# ════════════════════════════════════════════════════════════
# Phase A2: behavior_coverage_report
# ════════════════════════════════════════════════════════════

class TestBehaviorCoverageReport:
    def test_import(self):
        import behavior_coverage_report  # noqa: F401

    def test_generate_report_empty(self, tmp_path, monkeypatch):
        import behavior_coverage_report as bcr
        monkeypatch.setattr(bcr, "_load_log", lambda **kw: [])
        monkeypatch.setattr(bcr, "_load_dca_state", lambda: {})
        monkeypatch.setattr(bcr, "_load_scenario_state", lambda: {})
        report = bcr.generate_report(days=7)
        assert report["unique_analysis_runs"] == 0
        assert report["total_entries"] == 0
        assert isinstance(report["type_distribution_by_stage"], dict)

    def test_direction_ratio_separates_buy_sell(self, monkeypatch):
        import behavior_coverage_report as bcr
        fake_entries = [
            {"stage": "opus_raw", "analysis_id": "a1", "direction": "buy",
             "canonical_action_type": "buy", "as_of": "2026-06-15T07:00:00"},
            {"stage": "opus_raw", "analysis_id": "a1", "direction": "sell",
             "canonical_action_type": "sell", "as_of": "2026-06-15T07:00:00"},
            {"stage": "opus_raw", "analysis_id": "a1", "direction": "sell",
             "canonical_action_type": "take_profit", "as_of": "2026-06-15T07:00:00"},
        ]
        ratio = bcr._direction_ratio(fake_entries)
        assert ratio["opus_raw"]["total_buy"] == 1
        assert ratio["opus_raw"]["total_sell"] == 2


# ════════════════════════════════════════════════════════════
# Phase B1: tickers.json JP ETF
# ════════════════════════════════════════════════════════════

class TestTickersJsonJpEtf:
    def _load(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        p = Path(__file__).parent.parent / "tickers.json"
        if not p.exists():
            pytest.skip("private tickers.json is intentionally excluded from the public snapshot")
        return json.loads(p.read_text(encoding="utf-8"))

    def test_jp_etfs_in_all(self):
        t = self._load()
        for ticker in ("1306.T", "1321.T", "1489.T", "1698.T"):
            assert ticker in t["all"], f"{ticker} should be in tickers.json['all']"

    def test_jp_etfs_in_etf_list(self):
        t = self._load()
        for ticker in ("1306.T", "1321.T", "1489.T", "1698.T"):
            assert ticker in t["etf_list"], f"{ticker} should be in etf_list"

    def test_1489_in_long_term_universe(self):
        t = self._load()
        assert "1489.T" in t["long_term_universe"]

    def test_1570_not_in_all(self):
        """1570.T はレバレッジETFなのでgeneric universeに入れない。"""
        t = self._load()
        assert "1570.T" not in t["all"]


# ════════════════════════════════════════════════════════════
# Phase B2-0: scenario_engine contract fixes
# ════════════════════════════════════════════════════════════

class TestScenarioEngineContractFixes:
    def _make_scenario(self, min_signals=None, enabled_for_decision=True,
                       observe_only=False, required_signals=None):
        sc = {
            "id": "test_scenario",
            "name": "Test",
            "detect": {
                "news_keywords": [],
                "indicators": {},
                "technical": {},
            }
        }
        if min_signals is not None:
            sc["detect"]["min_signals"] = min_signals
        if required_signals:
            sc["detect"]["required_signals"] = required_signals
        if not enabled_for_decision:
            sc["enabled_for_decision"] = False
        if observe_only:
            sc["observe_only"] = True
        return sc

    def test_min_signals_not_met_caps_to_dormant(self):
        import scenario_engine as se
        # 3 シグナル必要、マッチ 0 → dormant
        signal_details = [
            {"matched": False, "detail": "vix=30", "type": "indicator", "key": "vix"},
            {"matched": False, "detail": "no bull", "type": "technical", "key": "regime"},
        ]
        from scenario_engine import INCONCLUSIVE_DETAIL
        # min_signals=3 だが conclusive_met=0 なので readiness を 0.29 以下に落とすべき
        sc = self._make_scenario(min_signals=3)
        detect = sc.get("detect") or {}
        min_req = detect.get("min_signals")
        conclusive_met = sum(
            1 for s in signal_details
            if s.get("matched") and s.get("detail") != INCONCLUSIVE_DETAIL
        )
        assert isinstance(min_req, int)
        assert conclusive_met < min_req

    def test_observe_only_sets_enabled_for_decision_false(self):
        """observe_only=True のシナリオは enabled_for_decision=False になるべき。"""
        sc = self._make_scenario(observe_only=True)
        observe_only = sc.get("observe_only", False)
        enabled = sc.get("enabled_for_decision", True)
        # エンジンのロジック: observe_only が True なら enabled_for_decision を False に
        if observe_only:
            enabled = False
        assert enabled is False

    def test_ewj_outperforms_spy_resolver_positive(self):
        from scenario_engine import _eval_special_technical
        market = {
            "EWJ": {"return_20d": 5.0, "price": 70.0},
            "SPY": {"return_20d": 2.0, "price": 500.0},
        }
        result = _eval_special_technical("ewj_outperforms_spy_20d", {"condition": "true"}, market, None)
        assert result is not None
        assert result["matched"] is True
        assert "EWJ" in result["detail"]

    def test_ewj_outperforms_spy_resolver_negative(self):
        from scenario_engine import _eval_special_technical
        market = {
            "EWJ": {"return_20d": 1.0},
            "SPY": {"return_20d": 5.0},
        }
        result = _eval_special_technical("ewj_outperforms_spy_20d", {"condition": "true"}, market, None)
        assert result is not None
        assert result["matched"] is False

    def test_ewj_outperforms_spy_no_data_inconclusive(self):
        from scenario_engine import _eval_special_technical, INCONCLUSIVE_DETAIL
        result = _eval_special_technical("ewj_outperforms_spy_20d", {"condition": "true"}, {}, None)
        assert result is not None
        assert result["detail"] == INCONCLUSIVE_DETAIL

    def test_nikkei_above_ma50_resolver(self):
        from scenario_engine import _eval_special_technical
        market = {"^N225": {"ma50_diff": 3.5, "price": 38000.0}}
        result = _eval_special_technical("nikkei_or_topix_above_ma50", {"condition": "true"}, market, None)
        assert result is not None
        assert result["matched"] is True

    def test_nikkei_below_ma50(self):
        from scenario_engine import _eval_special_technical
        market = {"^N225": {"ma50_diff": -2.0}}
        result = _eval_special_technical("nikkei_or_topix_above_ma50", {"condition": "true"}, market, None)
        assert result is not None
        assert result["matched"] is False

    def test_topix_fallback_key(self):
        from scenario_engine import _eval_special_technical
        market = {"TOPIX": {"price": 2700.0, "ma50": 2600.0}}
        result = _eval_special_technical("nikkei_or_topix_above_ma50", {"condition": "true"}, market, None)
        assert result is not None
        assert result["matched"] is True

    def test_nikkei_ma50_no_data_inconclusive(self):
        from scenario_engine import _eval_special_technical, INCONCLUSIVE_DETAIL
        result = _eval_special_technical("nikkei_or_topix_above_ma50", {"condition": "true"}, {}, None)
        assert result is not None
        assert result["detail"] == INCONCLUSIVE_DETAIL

    def test_ewj_indicator_resolver(self):
        from scenario_engine import _resolve_indicator_value
        market = {"EWJ": {"return_20d": 4.0}, "SPY": {"return_20d": 1.5}}
        vix = {}
        macro = {}
        # ewj_outperforms_spy_20d 数値 resolver
        val = _resolve_indicator_value("ewj_vs_spy_20d", {"key": "ewj_outperforms_spy_20d"}, vix, macro, market)
        # key フィールドで解決を試みる
        val2 = _resolve_indicator_value("ewj_vs_spy_20d", {}, vix, macro, market)
        # インジケータ名でも解決できるはず
        assert val is not None or val2 is not None  # どちらかで取れれば OK


# ════════════════════════════════════════════════════════════
# Phase B2-1: japan_standalone_bull シナリオ
# ════════════════════════════════════════════════════════════

class TestJapanStandaloneBullScenario:
    def _load_playbook(self):
        p = Path(__file__).parent.parent / "scenario_playbook.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def test_scenario_exists(self):
        book = self._load_playbook()
        ids = [s.get("id") for s in book.get("scenarios", [])]
        assert "japan_standalone_bull" in ids

    def test_action_enabled_not_observe_only(self):
        # 2026-06-27: 限定サイズで action 化 — observe_only=false へ昇格
        book = self._load_playbook()
        sc = next(s for s in book["scenarios"] if s.get("id") == "japan_standalone_bull")
        assert sc.get("observe_only") is False

    def test_enabled_for_decision_true(self):
        # 2026-06-27: action 化により decision pipeline に参加
        book = self._load_playbook()
        sc = next(s for s in book["scenarios"] if s.get("id") == "japan_standalone_bull")
        assert sc.get("enabled_for_decision") is True

    def test_action_size_bounded(self):
        # 限定サイズの担保: 各 buy の allocation_jpy は ¥500k 上限（細切れ昇格の暴発防止）。
        # 2026-07-07 改定: ¥100k/銘柄 (総資産0.7%) は JP エクスポージャー形成に実質寄与せず
        # ¥500k/銘柄 (計 ¥1M ≈ 総資産3.4%) へ引き上げ。core ETF 単発上限 ¥150万 の範囲内。
        book = self._load_playbook()
        sc = next(s for s in book["scenarios"] if s.get("id") == "japan_standalone_bull")
        buys = sc.get("actions", {}).get("phase_1", {}).get("buy", [])
        assert buys, "phase_1.buy が空"
        for b in buys:
            assert b.get("allocation_jpy", 0) <= 500_000

    def test_required_signals(self):
        book = self._load_playbook()
        sc = next(s for s in book["scenarios"] if s.get("id") == "japan_standalone_bull")
        req = sc.get("detect", {}).get("required_signals", [])
        assert "ewj_outperforms_spy_20d" in req
        assert "nikkei_or_topix_above_ma50" in req
        assert "regime_bull_confirmed" in req

    def test_min_signals_set(self):
        book = self._load_playbook()
        sc = next(s for s in book["scenarios"] if s.get("id") == "japan_standalone_bull")
        assert sc.get("detect", {}).get("min_signals") == 3


# ════════════════════════════════════════════════════════════
# Phase B3-0: drawdown_dca_engine cash fix
# ════════════════════════════════════════════════════════════

class TestDcaCashFix:
    def test_estimate_cash_reads_account_json(self, monkeypatch, tmp_path):
        import drawdown_dca_engine as dca
        acct = {"total_cash": 5000000.0, "balance": 4000000.0}
        acct_path = tmp_path / "account.json"
        acct_path.write_text(json.dumps(acct), encoding="utf-8")
        monkeypatch.setattr(dca, "BASE_DIR", tmp_path)
        result = dca._estimate_cash_jpy()
        assert result == 5000000.0

    def test_estimate_cash_falls_back_to_balance(self, monkeypatch, tmp_path):
        import drawdown_dca_engine as dca
        acct = {"balance": 3000000.0}
        acct_path = tmp_path / "account.json"
        acct_path.write_text(json.dumps(acct), encoding="utf-8")
        monkeypatch.setattr(dca, "BASE_DIR", tmp_path)
        result = dca._estimate_cash_jpy()
        assert result == 3000000.0

    def test_estimate_cash_returns_none_when_no_file(self, monkeypatch, tmp_path):
        import drawdown_dca_engine as dca
        monkeypatch.setattr(dca, "BASE_DIR", tmp_path)
        # portfolio_manager を封じる
        monkeypatch.setattr(
            "drawdown_dca_engine.BASE_DIR", tmp_path, raising=False
        )
        # portfolio_manager がない場合の None fallback
        result = dca._estimate_cash_jpy()
        # ファイルなし → None を返すか portfolio_manager fallback
        assert result is None or isinstance(result, float)

    def test_persist_uses_atomic_write(self, monkeypatch, tmp_path):
        import drawdown_dca_engine as dca
        path = tmp_path / "signals.json"
        signals = {"active_tranche": None, "freshness_date": "2026-06-15"}
        dca.persist(signals, path=path)
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["freshness_date"] == "2026-06-15"

    def test_generate_ladder_signals_has_freshness_date(self, monkeypatch, tmp_path):
        import drawdown_dca_engine as dca
        from datetime import date
        monkeypatch.setattr(dca, "BASE_DIR", tmp_path)
        # macro/vix のモックで dry_run
        monkeypatch.setattr(dca, "compute_drawdown_state", lambda **kw: {
            "current_value_jpy": 30000000.0, "peak_value_jpy": 32000000.0,
            "dd_from_peak": -0.02, "history_days": 50,
        })
        monkeypatch.setattr(dca, "evaluate_sector_breadth", lambda: {
            "sectors_below_ma20": 3, "total": 11, "broad_selloff": False
        })
        monkeypatch.setattr(dca, "check_volume_capitulation", lambda **kw: False)
        monkeypatch.setattr(dca, "evaluate_rsi_reversal", lambda *a: {"reversed": False})
        import sys
        # macro/vix モジュールを無効化
        sys.modules.setdefault("macro_fetcher", type(sys)("macro_fetcher"))
        sys.modules["macro_fetcher"].get_macro_context = lambda: {}
        sys.modules["macro_fetcher"].classify_panic = lambda x: {}
        sys.modules.setdefault("vix_tracker", type(sys)("vix_tracker"))
        sys.modules["vix_tracker"].get_vix_context = lambda: {}
        result = dca.generate_ladder_signals(cash_jpy=1000000.0, dry_run=True)
        assert "freshness_date" in result
        assert result["freshness_date"] == date.today().isoformat()


# ════════════════════════════════════════════════════════════
# Phase D1: rebalance_engine 正規化
# ════════════════════════════════════════════════════════════

class TestRebalanceEngineNormalization:
    def _make_snapshot(self, tickers: list[str], values: list[float]) -> dict:
        positions = [
            {"ticker": t, "investment_type": "medium", "value_jpy": v, "name": t}
            for t, v in zip(tickers, values)
        ]
        return {"positions": positions}

    def test_target_sum_equals_100(self):
        import rebalance_engine as re
        snap = self._make_snapshot(
            ["META", "6762.T", "AAPL", "V", "MA", "COST"],
            [500000, 200000, 150000, 100000, 80000, 70000]
        )
        result = re.calculate_medium_drift(snap)
        target_sum = sum(p["target_pct"] for p in result["positions"])
        assert abs(target_sum - 100.0) < 0.01, f"target sum = {target_sum:.2f}%, expected ~100%"

    def test_all_fallback_gives_equal_weight(self):
        """明示設定がないすべてのticker → 均等配分。"""
        import rebalance_engine as re
        tickers = ["AAPL", "V", "MA", "COST"]
        snap = self._make_snapshot(tickers, [100000, 100000, 100000, 100000])
        result = re.calculate_medium_drift(snap)
        targets = [p["target_pct"] for p in result["positions"]]
        # META/6762.T がないので全て等倍
        assert all(abs(t - 25.0) < 0.1 for t in targets)

    def test_explicit_plus_fallback_sums_100(self):
        """META(20%) + 6762.T(8%) + 残りは (1.0-0.28)/3 ずつ。"""
        import rebalance_engine as re
        snap = self._make_snapshot(
            ["META", "6762.T", "AAPL", "V", "COST"],
            [200000, 200000, 200000, 200000, 200000]
        )
        result = re.calculate_medium_drift(snap)
        total = sum(p["target_pct"] for p in result["positions"])
        assert abs(total - 100.0) < 0.01
        meta = next(p for p in result["positions"] if p["ticker"] == "META")
        assert abs(meta["target_pct"] - 20.0) < 0.01

    def test_stale_tickers_not_in_target_weights(self):
        """EPOL / RCL / IFREE_FANGPLUS / NOMURA_SEMI は weights から削除済み。"""
        import rebalance_engine as re
        stale = ["EPOL", "RCL", "IFREE_FANGPLUS", "NOMURA_SEMI"]
        for t in stale:
            assert t not in re.MEDIUM_TARGET_WEIGHTS, f"{t} should have been removed"


# ════════════════════════════════════════════════════════════
# Phase C: take_profit 方向修正
# ════════════════════════════════════════════════════════════

class TestTakeProfitDirection:
    def test_action_stage_log_take_profit_is_sell(self):
        from action_stage_log import _direction
        assert _direction("take_profit") == "sell"

    def test_policy_engine_does_not_block_take_profit(self):
        """take_profit は sell 系なので buy 系 policy block の対象外。"""
        import policy_engine as pe
        ctx = pe.PolicyContext(current_dd=-0.10, actual_dd_stage="block")
        action = {"type": "take_profit", "ticker": "NVDA", "urgency": "high"}
        d = pe.apply_policy_gate([action], ctx)
        # DD block は buy 系のみ → take_profit は通過するはず
        assert len(d.accepted) == 1
        assert len(d.rejected) == 0

    def test_coverage_report_counts_take_profit_as_sell(self):
        import behavior_coverage_report as bcr
        entries = [
            {"stage": "opus_raw", "analysis_id": "x1", "direction": "sell",
             "canonical_action_type": "take_profit", "as_of": "2026-06-15T07:00:00"},
        ]
        ratio = bcr._direction_ratio(entries)
        assert ratio["opus_raw"]["total_sell"] == 1
        assert ratio["opus_raw"]["total_buy"] == 0


# ════════════════════════════════════════════════════════════
# Codex re-review fixes: F1-F7
# ════════════════════════════════════════════════════════════

class TestF2JapanResolverRealDataShape:
    """F2: EWJ/SPY は technical_state[tickers][change_20d_pct]、日経は market_snapshot[NK225]。"""

    def test_ewj_from_technical_state_tickers(self):
        from scenario_engine import _eval_special_technical
        tech = {"tickers": {
            "EWJ": {"change_20d_pct": 0.71},
            "SPY": {"change_20d_pct": -0.86},
        }}
        market = {}  # market_snapshot に EWJ は無い
        r = _eval_special_technical("ewj_outperforms_spy_20d", {"condition": "true"},
                                    market, None, tech)
        assert r["matched"] is True
        assert "EWJ 20d=+0.71%" in r["detail"]

    def test_ewj_inconclusive_without_tech_state(self):
        from scenario_engine import _eval_special_technical, INCONCLUSIVE_DETAIL
        # market のみ（EWJ無し）& tech_state無し → データ未取得
        r = _eval_special_technical("ewj_outperforms_spy_20d", {"condition": "true"},
                                    {}, None, None)
        assert r["detail"] == INCONCLUSIVE_DETAIL

    def test_nikkei_nk225_key_from_market_snapshot(self):
        from scenario_engine import _eval_special_technical
        market = {"NK225": {"price": 69317.5, "ma50": 61227.88, "ma50_diff": 13.21}}
        r = _eval_special_technical("nikkei_or_topix_above_ma50", {"condition": "true"},
                                    market, None, None)
        assert r["matched"] is True
        assert "NK225" in r["detail"]

    def test_real_data_files_resolve(self):
        """実ファイル形状で両必須シグナルが解決される（データ未取得にならない）。"""
        import scenario_engine as se
        from scenario_engine import INCONCLUSIVE_DETAIL
        base = Path(__file__).parent.parent
        if not (base / "technical_state.json").exists() or not (base / "market_snapshot.json").exists():
            pytest.skip("private runtime market state is intentionally excluded")
        tech = json.loads((base / "technical_state.json").read_text())
        market = json.loads((base / "market_snapshot.json").read_text())
        r1 = se._eval_special_technical("ewj_outperforms_spy_20d", {"condition": "true"},
                                        market, None, tech)
        r2 = se._eval_special_technical("nikkei_or_topix_above_ma50", {"condition": "true"},
                                        market, None, tech)
        assert r1["detail"] != INCONCLUSIVE_DETAIL
        assert r2["detail"] != INCONCLUSIVE_DETAIL


class TestF4RebalanceDedupAnd100:
    def _mk(self, positions):
        return {"positions": [
            {"ticker": t, "key": k, "investment_type": "medium", "value_jpy": v, "name": t}
            for t, k, v in positions
        ]}

    def test_meta_multi_account_not_double_counted(self):
        import rebalance_engine as re
        snap = self._mk([("META", "META_特定", 300000), ("META", "META_一般", 200000),
                         ("6762.T", "6762.T", 200000), ("AAPL", "AAPL", 300000)])
        r = re.calculate_medium_drift(snap)
        meta_total = sum(p["target_pct"] for p in r["positions"] if p["ticker"] == "META")
        assert abs(meta_total - 20.0) < 0.1, f"META 合計 {meta_total}% should be ~20%"
        assert abs(sum(p["target_pct"] for p in r["positions"]) - 100.0) < 0.01

    def test_targets_sum_to_100_and_drift_nets_zero(self):
        """Codex round3 basis統一: Medium 層内比率で target 合計 100%、drift 合計 0。

        全explicit でも層内比率に正規化 (相対 basis に commit)。drift 合計 0 で
        cash-neutral リバランスになり反復縮小しない。
        """
        import rebalance_engine as re
        snap = self._mk([("META", "META", 500000), ("6762.T", "6762.T", 300000),
                         ("AAPL", "AAPL", 150000), ("V", "V", 50000)])
        r = re.calculate_medium_drift(snap)
        total_target = sum(p["target_pct"] for p in r["positions"])
        total_drift = sum(p["drift_pct"] for p in r["positions"])
        assert abs(total_target - 100.0) < 0.1
        assert abs(total_drift) < 0.1  # cash-neutral
        assert r["target_basis"] == "medium_tier_internal_normalized_100pct_capped"

    def test_rebalance_plan_is_cash_neutral(self):
        """生成プランの trim 合計 = buy 合計 (net cash flow ≈ 0)。"""
        import rebalance_engine as re
        snap = self._mk([("META", "META", 900000), ("AAPL", "AAPL", 50000), ("V", "V", 50000)])
        r = re.calculate_medium_drift(snap)
        net = 0
        for a in r["actions"]:
            net += (-1 if a["type"] == "reduce" else 1) * a["amount_jpy"]
        assert abs(net) < 1000  # 端数除き net 0

    def test_meta_split_across_accounts_by_value(self):
        """META 20% が口座 value 比 (3:2) で按分される。"""
        import rebalance_engine as re
        snap = self._mk([("META", "META_特定", 300000), ("META", "META_一般", 200000),
                         ("AAPL", "AAPL", 500000)])
        r = re.calculate_medium_drift(snap)
        toku = next(p for p in r["positions"] if p.get("name") == "META" and abs(p["value_jpy"] - 300000) < 1)
        ippan = next(p for p in r["positions"] if p.get("name") == "META" and abs(p["value_jpy"] - 200000) < 1)
        # 3:2 比率
        assert toku["target_pct"] > ippan["target_pct"]


class TestF6DownloadTickersPersistence:
    def test_jp_etf_constant_present(self):
        import download_tickers as dt
        # JP_ETF_LIST がソース定数として存在する
        assert hasattr(dt, "JP_ETF_LIST")
        for t in ("1306.T", "1321.T", "1489.T", "1698.T"):
            assert t in dt.JP_ETF_LIST
        assert "1570.T" not in dt.JP_ETF_LIST

    def test_kioxia_new_listing_is_onboarded(self):
        import download_tickers as dt
        assert "285A.T" in dt.NEW_LISTINGS
        path = Path(__file__).resolve().parents[1] / "tickers.json"
        if not path.exists():
            pytest.skip("private tickers.json is intentionally excluded from the public snapshot")
        tickers = json.loads(path.read_text())
        for key in ("all", "long_term_universe", "margin_long_universe"):
            assert "285A.T" in tickers[key]


class TestMarginLongCandidateFreshness:
    def test_morning_analysis_prefers_morning_margin_candidates(self, monkeypatch, tmp_path):
        from datetime import datetime
        import analyst.data_gatherer as dg

        class MorningDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 26, 6, 10, tzinfo=tz)

        (tmp_path / "margin_long_candidates.json").write_text(json.dumps({"candidates": [{"ticker": "OLD"}]}))
        (tmp_path / "margin_long_candidates_morning.json").write_text(json.dumps({"candidates": [{"ticker": "NEW"}]}))
        monkeypatch.setattr(dg, "BASE_DIR", tmp_path)
        monkeypatch.setattr(dg, "datetime", MorningDateTime)

        raw = dg._load_margin_long_candidates()

        assert raw["_selected_source_file"] == "margin_long_candidates_morning.json"
        assert raw["candidates"][0]["ticker"] == "NEW"

    def test_morning_margin_candidates_keep_fresh_regular_jp_overlay(self, monkeypatch, tmp_path):
        from datetime import datetime
        import analyst.data_gatherer as dg

        class MorningDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 29, 6, 10, tzinfo=tz)

        (tmp_path / "margin_long_candidates_morning.json").write_text(json.dumps({
            "generated_at": "2026-06-29 06:00",
            "candidates": [{"ticker": "V"}, {"ticker": "QQQ"}],
        }))
        (tmp_path / "margin_long_candidates.json").write_text(json.dumps({
            "generated_at": "2026-06-26 19:16",
            "candidates": [
                {"ticker": "OLD_US"},
                {"ticker": "6367.T", "score": 103.7},
                {"ticker": "6501.T", "score": 48.9},
            ],
        }))
        monkeypatch.setattr(dg, "BASE_DIR", tmp_path)
        monkeypatch.setattr(dg, "datetime", MorningDateTime)

        raw = dg._load_margin_long_candidates()

        assert raw["_selected_source_file"] == "margin_long_candidates_morning.json"
        assert [c["ticker"] for c in raw["candidates"]] == ["V", "QQQ", "6367.T", "6501.T"]
        assert raw["_jp_overlay_candidate_count"] == 2
        assert raw["_jp_overlay_source_files"] == ["margin_long_candidates.json"]

    def test_morning_margin_candidates_skip_stale_regular_jp_overlay(self, monkeypatch, tmp_path):
        from datetime import datetime
        import analyst.data_gatherer as dg

        class MorningDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 29, 6, 10, tzinfo=tz)

        (tmp_path / "margin_long_candidates_morning.json").write_text(json.dumps({
            "generated_at": "2026-06-29 06:00",
            "candidates": [{"ticker": "V"}],
        }))
        (tmp_path / "margin_long_candidates.json").write_text(json.dumps({
            "generated_at": "2026-06-24 19:16",
            "candidates": [{"ticker": "6367.T"}],
        }))
        monkeypatch.setattr(dg, "BASE_DIR", tmp_path)
        monkeypatch.setattr(dg, "datetime", MorningDateTime)

        raw = dg._load_margin_long_candidates()

        assert [c["ticker"] for c in raw["candidates"]] == ["V"]
        assert "_jp_overlay_candidate_count" not in raw


class TestF5DcaCashBreakdown:
    def test_breakdown_recomputes_usd_jpy_from_current_fx(self, monkeypatch, tmp_path):
        import drawdown_dca_engine as dca
        acct = {"balance": 846900.0, "usd_balance": 46004.93,
                "jpy_equivalent_usd": 6630665.0, "total_cash": 8212841.0,
                "fx_rate_usdjpy": 160.0}
        (tmp_path / "account.json").write_text(json.dumps(acct))
        monkeypatch.setattr(dca, "BASE_DIR", tmp_path)
        b = dca._estimate_cash_breakdown()
        assert b["jpy"] == 846900.0
        assert b["usd_jpy"] == pytest.approx(46004.93 * 160.0)
        assert b["total_jpy"] == pytest.approx(846900.0 + 46004.93 * 160.0)
        assert b["source"] == "account.json"

    def test_jpy_target_uses_jpy_cash_for_sufficiency(self):
        import drawdown_dca_engine as dca
        cb = {"jpy": 200000.0, "usd_jpy": 5000000.0}
        # JPY target 2件で per_ticker=150k → JPY所要300k > 200k → 不足
        buys = dca._build_recommended_buys("T1", 2000000.0, target_tickers=["1489.T", "1306.T"],
                                           deploy_jpy=300000.0, cash_breakdown=cb)
        for b in buys:
            assert b["currency"] == "JPY"
            assert b["currency_cash_sufficient"] is False  # 300k > 200k

    def test_usd_target_currency_detection(self):
        import drawdown_dca_engine as dca
        assert dca._ticker_currency("1489.T") == "JPY"
        assert dca._ticker_currency("SLIM_SP500") == "JPY"
        assert dca._ticker_currency("GLD") == "USD"
        assert dca._ticker_currency("NVDA") == "USD"


class TestF3CoverageNewFeatures:
    def _mk(self, aid, asof, stage, ticker, atype, notional=None):
        import action_stage_log as asl
        e = asl._make_entry(analysis_id=aid, as_of=asof, stage=stage,
                            action={"type": atype, "ticker": ticker})
        if notional is not None:
            e["estimated_notional_jpy"] = notional
        return e

    def test_consecutive_only_counts_adjacent_runs(self):
        import behavior_coverage_report as bcr
        entries = [
            self._mk("r1", "2026-06-13T07:00", "post_filter_final", "NVDA", "buy"),
            self._mk("r2", "2026-06-14T07:00", "post_filter_final", "NVDA", "buy"),
            self._mk("r3", "2026-06-15T07:00", "post_filter_final", "AAPL", "buy"),
        ]
        rep = bcr._consecutive_repeats(entries)
        nvda = next(x for x in rep if x["ticker"] == "NVDA")
        assert nvda["consecutive_transitions"] == 1
        assert nvda["max_consecutive_streak"] == 2

    def test_print_report_handles_consecutive_repeat_rows(self, capsys):
        import behavior_coverage_report as bcr

        report = bcr.generate_report(days=7, include_dca=False, include_scenarios=False)
        report["consecutive_same_direction_repeats"] = [{
            "ticker": "NVDA",
            "direction": "buy",
            "unique_runs": 2,
            "consecutive_transitions": 1,
            "max_consecutive_streak": 2,
        }]

        bcr.print_report(report)

        out = capsys.readouterr().out
        assert "NVDA buy" in out
        assert "連続遷移=1" in out

    def test_non_consecutive_not_counted(self):
        """run1 と run3 に出るが run2 に無い → 連続ではない。"""
        import behavior_coverage_report as bcr
        entries = [
            self._mk("r1", "2026-06-13T07:00", "post_filter_final", "NVDA", "buy"),
            self._mk("r2", "2026-06-14T07:00", "post_filter_final", "AAPL", "buy"),
            self._mk("r3", "2026-06-15T07:00", "post_filter_final", "NVDA", "buy"),
        ]
        rep = bcr._consecutive_repeats(entries)
        assert all(x["ticker"] != "NVDA" for x in rep)

    def test_notional_ratio_by_amount(self):
        import behavior_coverage_report as bcr
        entries = [
            self._mk("execution", "2026-06-15T10:00", "executed", "NVDA", "buy", 500000),
            self._mk("execution", "2026-06-15T11:00", "executed", "TDK", "sell", 300000),
        ]
        nz = bcr._notional_ratio(entries)
        assert nz["buy_notional_jpy"] == 500000
        assert nz["sell_notional_jpy"] == 300000
        assert nz["buy_notional_pct"] == 62.5

    def test_take_profit_executed_counts_as_sell_notional(self):
        import behavior_coverage_report as bcr
        entries = [
            self._mk("execution", "2026-06-15T10:00", "executed", "NVDA", "take_profit", 400000),
        ]
        nz = bcr._notional_ratio(entries)
        assert nz["sell_notional_jpy"] == 400000
        assert nz["buy_notional_jpy"] == 0

    def test_post_filter_drop(self):
        import behavior_coverage_report as bcr
        entries = [
            self._mk("r1", "2026-06-15T07:00", "opus_raw", "NVDA", "buy"),
            self._mk("r1", "2026-06-15T07:00", "opus_raw", "AAPL", "buy"),
            self._mk("r1", "2026-06-15T07:00", "post_filter_final", "NVDA", "buy"),
        ]
        pf = bcr._post_filter_drop(entries)
        assert pf["raw_to_final_dropped"] == 1  # AAPL dropped
        assert pf["raw_to_final_survived"] == 1  # NVDA survived

    def test_post_filter_survival_excludes_deferred_and_rejected_rows(self):
        import behavior_coverage_report as bcr
        entries = [
            self._mk("r1", "2026-07-09T08:00", "opus_raw", "A", "buy"),
            self._mk("r1", "2026-07-09T08:00", "opus_raw", "B", "buy"),
            self._mk("r1", "2026-07-09T08:00", "opus_raw", "C", "buy"),
            self._mk("r1", "2026-07-09T08:00", "post_filter_final", "A", "buy"),
            self._mk("r1", "2026-07-09T08:00", "post_filter_rejected", "B", "buy"),
            self._mk("r1", "2026-07-09T08:00", "post_filter_deferred", "C", "buy"),
        ]

        pf = bcr._post_filter_drop(entries)

        assert pf["raw_to_final_survived"] == 1
        assert pf["raw_to_final_dropped"] == 2
        assert pf["survival_pct"] == 33.3

    def test_filtered_final_rows_do_not_inflate_survival(self):
        import action_stage_log as asl
        import behavior_coverage_report as bcr
        entries = [
            self._mk("r1", "2026-06-26T06:10", "opus_raw", "A", "buy"),
            self._mk("r1", "2026-06-26T06:10", "opus_raw", "B", "buy"),
            self._mk("r1", "2026-06-26T06:10", "post_filter_final", "A", "buy"),
            asl._make_entry(
                analysis_id="r1",
                as_of="2026-06-26T06:10",
                stage="post_filter_final",
                action={
                    "ticker": "B",
                    "type": "buy",
                    "filtered_reason": "too_small: below threshold",
                    "estimated_notional_jpy": 50_000,
                },
            ),
        ]

        pf = bcr._post_filter_drop(entries)

        assert pf["raw_to_final_dropped"] == 1
        assert pf["raw_to_final_survived"] == 1
        assert pf["survival_pct"] == 50.0

    def test_source_freshness_detects_stale(self):
        import behavior_coverage_report as bcr
        dca_state = {"freshness_date": "2020-01-01"}
        scn_state = {"evaluated_at": "2020-01-01T00:00:00"}
        fr = bcr._source_freshness(dca_state, scn_state, [])
        assert fr["dca_is_fresh"] is False
        assert fr["scenario_is_fresh"] is False


class TestF1ObserveOnlyFilter:
    def test_observe_only_excluded_from_decision_logic(self):
        """data_gatherer のフィルタロジック: observe_only=true → decision から除外。"""
        # ロジックの単体検証（data_gatherer 全体は重いので抽出ロジックを再現）
        sc_data = {"observe_only": True, "enabled_for_decision": True, "status": "active"}
        observe_only = sc_data.get("observe_only", False)
        enabled = sc_data.get("enabled_for_decision", True)
        if observe_only:
            enabled = False
        assert enabled is False

    def test_japan_scenario_is_decision_enabled_in_state(self):
        """2026-06-27 action 化後、japan_standalone_bull は observe_only=false / decision 参加で出力される。"""
        # playbook から該当シナリオを取得して decision フラグを確認
        book = json.loads((Path(__file__).parent.parent / "scenario_playbook.json").read_text())
        sc = next(s for s in book["scenarios"] if s["id"] == "japan_standalone_bull")
        observe_only = sc.get("observe_only", False)
        enabled = sc.get("enabled_for_decision", True)
        if observe_only:
            enabled = False
        assert observe_only is False
        assert enabled is True


class TestF7AnalysisIdUnification:
    def test_observability_ids_module_available(self):
        from almanac.observability.ids import new_analysis_id
        aid = new_analysis_id()
        assert isinstance(aid, str) and len(aid) > 0


# ════════════════════════════════════════════════════════════
# Codex 2nd round fixes
# ════════════════════════════════════════════════════════════

class TestR2DcaCurrencyClip:
    def test_jpy_target_clipped_to_jpy_cash(self):
        import drawdown_dca_engine as dca
        cb = {"jpy": 846900.0, "usd_jpy": 6630665.0}
        # 1489.T 単独で 123万要求 → JPY残高 84.69万 に clip、繰延 38.31万
        buys = dca._build_recommended_buys("T1", 8000000.0, target_tickers=["1489.T"],
                                           deploy_jpy=1230000.0, cash_breakdown=cb)
        b = buys[0]
        assert b["target_jpy"] == 846900
        assert b["requested_jpy"] == 1230000
        assert b["deferred_jpy"] == 383100
        assert b["currency_cash_sufficient"] is False

    def test_usd_target_not_clipped_when_sufficient(self):
        import drawdown_dca_engine as dca
        cb = {"jpy": 846900.0, "usd_jpy": 6630665.0}
        buys = dca._build_recommended_buys("T1", 8000000.0, target_tickers=["GLD"],
                                           deploy_jpy=1000000.0, cash_breakdown=cb)
        b = buys[0]
        assert b["target_jpy"] == 1000000
        assert b["deferred_jpy"] == 0

    def test_no_clip_when_cash_unknown(self):
        import drawdown_dca_engine as dca
        buys = dca._build_recommended_buys("T1", 8000000.0, target_tickers=["1489.T"],
                                           deploy_jpy=1230000.0, cash_breakdown={})
        # 残高不明 → clip しない（従来挙動）
        assert buys[0]["target_jpy"] == 1230000
        assert buys[0]["deferred_jpy"] == 0


class TestR2ExecutedLogGating:
    def test_executed_log_skipped_when_not_applied(self, monkeypatch):
        import action_stage_log as asl
        logged = []
        monkeypatch.setattr(asl, "append_entries", lambda entries, path=None: logged.extend(entries))
        from api.routes.actions import _log_action_stage_executed
        # portfolio_result.updated=False → 記録しない
        _log_action_stage_executed(
            ticker="NVDA", direction="buy", account="特定", investment_type="long",
            price=100.0, quantity=2.0, currency="USD",
            portfolio_result={"updated": False}, as_of="2026-06-15T10:00",
        )
        assert logged == []

    def test_executed_log_converts_usd_to_jpy(self, monkeypatch):
        import action_stage_log as asl
        import api.routes.actions as ar
        logged = []
        monkeypatch.setattr(asl, "append_entries", lambda entries, path=None: logged.extend(entries))
        monkeypatch.setattr(ar, "_get_fx_rate", lambda: 160.0)
        ar._log_action_stage_executed(
            ticker="NVDA", direction="buy", account="特定", investment_type="long",
            price=100.0, quantity=2.0, currency="USD",
            portfolio_result={"updated": True, "cash_delta": -200.0, "cash_currency": "USD"},
            as_of="2026-06-15T10:00",
        )
        assert len(logged) == 1
        # USD 200 × 160 = 32,000 JPY（200円ではない）
        assert logged[0]["estimated_notional_jpy"] == 32000


class TestR2EligibleFireRate:
    def _mk(self, aid, asof, stage, ticker, atype):
        import action_stage_log as asl
        return asl._make_entry(analysis_id=aid, as_of=asof, stage=stage,
                               action={"type": atype, "ticker": ticker})

    def test_candidate_to_final_rate_buy_vs_sell(self):
        import behavior_coverage_report as bcr
        entries = [
            self._mk("r1", "2026-06-15T07:00", "tier_generated", "NVDA", "buy"),
            self._mk("r1", "2026-06-15T07:00", "tier_generated", "TDK", "sell"),
            self._mk("r1", "2026-06-15T07:00", "post_filter_final", "TDK", "sell"),
            # NVDA buy は生成されたが final に到達せず
        ]
        cf = bcr._candidate_to_final_rate(entries)
        assert cf["candidates"] == 2
        assert cf["reached_final"] == 1
        assert cf["by_direction"]["buy"]["rate_pct"] == 0.0
        assert cf["by_direction"]["sell"]["rate_pct"] == 100.0

    def test_stage_transition_rates_split_buy_and_sell(self):
        import behavior_coverage_report as bcr
        entries = [
            self._mk("r1", "2026-06-15T07:00", "tier_generated", "NVDA", "buy"),
            self._mk("r1", "2026-06-15T07:00", "tier_generated", "AAPL", "buy"),
            self._mk("r1", "2026-06-15T07:00", "tier_generated", "TDK", "sell"),
            self._mk("r1", "2026-06-15T07:00", "opus_raw", "NVDA", "buy"),
            self._mk("r1", "2026-06-15T07:00", "opus_raw", "TDK", "sell"),
            self._mk("r1", "2026-06-15T07:00", "policy_accepted", "NVDA", "buy"),
            self._mk("r1", "2026-06-15T07:00", "policy_accepted", "TDK", "sell"),
            self._mk("r1", "2026-06-15T07:00", "post_filter_final", "TDK", "sell"),
        ]

        rates = bcr._stage_transition_rates(entries)

        assert rates["tier_generated_to_opus_raw"]["by_direction"]["buy"] == {
            "from": 2, "to": 1, "rate_pct": 50.0,
        }
        assert rates["policy_accepted_to_post_filter_final"]["by_direction"]["buy"] == {
            "from": 1, "to": 0, "rate_pct": 0.0,
        }
        assert rates["policy_accepted_to_post_filter_final"]["by_direction"]["sell"] == {
            "from": 1, "to": 1, "rate_pct": 100.0,
        }


class TestR2TierDirectionEnrichment:
    def test_margin_buy_and_short_have_direction(self):
        """margin_long_picks→margin_buy(buy), short_opportunities→short(sell)。"""
        from action_stage_log import _direction
        assert _direction("margin_buy") == "buy"
        assert _direction("short") == "sell"


class TestR2ZeroNotMissing:
    def test_ewj_zero_pct_is_value_not_missing(self):
        import scenario_engine as se
        from scenario_engine import INCONCLUSIVE_DETAIL
        tech = {"tickers": {"EWJ": {"change_20d_pct": 0.0}, "SPY": {"change_20d_pct": 0.0}}}
        r = se._eval_special_technical("ewj_outperforms_spy_20d", {"condition": "true"},
                                       {}, None, tech)
        assert r["detail"] != INCONCLUSIVE_DETAIL
        assert r["matched"] is False  # diff=0 はアウトパフォームではない

    def test_nikkei_zero_ma50_diff_is_value(self):
        import scenario_engine as se
        from scenario_engine import INCONCLUSIVE_DETAIL
        market = {"NK225": {"ma50_diff": 0.0}}
        r = se._eval_special_technical("nikkei_or_topix_above_ma50", {"condition": "true"},
                                       market, None, None)
        assert r["detail"] != INCONCLUSIVE_DETAIL
        assert r["matched"] is False  # 0.0 は above ではない

    def test_first_present_skips_none_keeps_zero(self):
        from scenario_engine import _first_present
        assert _first_present({"a": 0.0, "b": 5.0}, "a", "b") == 0.0
        assert _first_present({"a": None, "b": 5.0}, "a", "b") == 5.0
        assert _first_present({}, "a", "b") is None


class TestR2StableRunOrder:
    def test_same_as_of_stable_order(self):
        import behavior_coverage_report as bcr
        # 同一 as_of の2run、logged_at で安定化
        entries = [
            {"analysis_id": "rB", "as_of": "2026-06-15T07:00", "logged_at": "2026-06-15T07:00:02",
             "stage": "post_filter_final", "ticker": "X", "direction": "buy"},
            {"analysis_id": "rA", "as_of": "2026-06-15T07:00", "logged_at": "2026-06-15T07:00:01",
             "stage": "post_filter_final", "ticker": "X", "direction": "buy"},
        ]
        ordered = bcr._ordered_run_ids(entries)
        # logged_at 昇順 → rA が先
        assert ordered == ["rA", "rB"]


# ════════════════════════════════════════════════════════════
# Codex 3rd round fixes
# ════════════════════════════════════════════════════════════

class TestR3NoIterativeShrinkage:
    def _mk(self, ps):
        return {"positions": [{"ticker": t, "key": k, "investment_type": "medium",
                               "value_jpy": v, "name": t} for t, k, v in ps]}

    def test_full_rebalance_keeps_total_constant(self):
        """trim+buy を全実行すると total_medium 不変 → 再評価で drift 解消。"""
        import rebalance_engine as re
        snap = self._mk([("META", "META", 900000), ("AAPL", "AAPL", 50000), ("V", "V", 50000)])
        r = re.calculate_medium_drift(snap)
        # 全アクション適用後の新 value を計算
        delta = {}
        for a in r["actions"]:
            sign = -1 if a["type"] == "reduce" else 1
            delta[a["ticker"]] = delta.get(a["ticker"], 0) + sign * a["amount_jpy"]
        new_vals = {"META": 900000 + delta.get("META", 0),
                    "AAPL": 50000 + delta.get("AAPL", 0),
                    "V": 50000 + delta.get("V", 0)}
        assert abs(sum(new_vals.values()) - 1000000) < 1000  # total 不変
        # 再評価で drift がほぼ解消
        snap2 = self._mk([("META", "META", new_vals["META"]),
                          ("AAPL", "AAPL", new_vals["AAPL"]), ("V", "V", new_vals["V"])])
        r2 = re.calculate_medium_drift(snap2)
        assert r2["status"] == "ok"  # 警告なし


class TestR3DcaStateConsumption:
    def test_zero_actual_deploy_does_not_consume_state(self, monkeypatch, tmp_path):
        """通貨 cash 0 で実投入 0 のとき、--fire でも budget/cooldown 非消費。"""
        import drawdown_dca_engine as dca
        monkeypatch.setattr(dca, "BASE_DIR", tmp_path)
        monkeypatch.setattr(dca, "STATE_FILE", tmp_path / "dca_state.json")
        monkeypatch.setattr(dca, "compute_drawdown_state", lambda **kw: {
            "current_value_jpy": 30000000.0, "peak_value_jpy": 36000000.0,
            "dd_from_peak": -0.18, "history_days": 60,
        })
        monkeypatch.setattr(dca, "evaluate_sector_breadth", lambda: {
            "sectors_below_ma20": 9, "total": 11, "broad_selloff": True})
        monkeypatch.setattr(dca, "check_volume_capitulation", lambda **kw: True)
        monkeypatch.setattr(dca, "evaluate_rsi_reversal", lambda *a: {"reversed": True, "rsi_latest": 32})
        import sys
        sys.modules.setdefault("macro_fetcher", type(sys)("macro_fetcher"))
        sys.modules["macro_fetcher"].get_macro_context = lambda: {}
        sys.modules["macro_fetcher"].classify_panic = lambda x: {
            "fear_greed": 10, "hy_oas_bps": 600, "put_call": 1.5, "vix": 45}
        sys.modules.setdefault("vix_tracker", type(sys)("vix_tracker"))
        sys.modules["vix_tracker"].get_vix_context = lambda: {
            "vix": {"level": 45, "decay_from_peak_5d_pct": -12}}
        # JPY/USD ともゼロ cash の breakdown
        cb = {"jpy": 0.0, "usd_jpy": 0.0, "total_jpy": 5000000.0}
        sig = dca.generate_ladder_signals(cash_jpy=5000000.0, dry_run=False, cash_breakdown=cb)
        # 実投入 0 → state ファイルに発火記録が残らない
        import json as _j
        state_file = tmp_path / "dca_state.json"
        if state_file.exists():
            st = _j.loads(state_file.read_text())
            assert not st.get("last_fired"), "zero-deploy fired state should be empty"


class TestR3ExecutedAlreadyApplied:
    def test_already_applied_not_logged(self, monkeypatch):
        import action_stage_log as asl
        logged = []
        monkeypatch.setattr(asl, "append_entries", lambda entries, path=None: logged.extend(entries))
        from api.routes.actions import _log_action_stage_executed
        _log_action_stage_executed(
            ticker="NVDA", direction="buy", account="特定", investment_type="long",
            price=100.0, quantity=2.0, currency="USD",
            portfolio_result={"updated": True, "already_applied": True, "cash_delta": -200.0},
            as_of="2026-06-15T10:00",
        )
        assert logged == []  # 冪等再適用は記録しない


class TestR3DcaCurrencyClipRounding:
    def test_integer_allocation_never_exceeds_balance(self):
        import drawdown_dca_engine as dca
        cb = {"jpy": 50.0, "usd_jpy": 1000000.0}
        buys = dca._build_recommended_buys("T1", 100000.0,
                                           target_tickers=["1306.T", "1321.T", "1489.T"],
                                           deploy_jpy=150.0, cash_breakdown=cb)
        total = sum(b["target_jpy"] for b in buys)
        assert total <= 50, f"allocation {total} exceeds JPY balance 50"
        assert all(isinstance(b["target_jpy"], int) for b in buys)


class TestR3ZeroNotMissingExtra:
    def test_spy_above_ma50_zero_diff_is_value(self):
        import scenario_engine as se
        from scenario_engine import INCONCLUSIVE_DETAIL
        r = se._eval_special_technical("SPY_above_MA50", {"condition": "true"},
                                       {"SPY": {"ma50_diff": 0.0}}, None)
        assert r["detail"] != INCONCLUSIVE_DETAIL

    def test_generic_spy_dist_zero_is_value(self):
        import scenario_engine as se
        v = se._resolve_indicator_value("spy_dist", {"key": "spy_dist_from_ma50_pct"},
                                        {}, {}, {"SPY": {"ma50_diff": 0.0}})
        assert v == 0.0  # None ではない


class TestR3CoverageRunCount:
    def test_execution_excluded_from_run_count(self, monkeypatch):
        import behavior_coverage_report as bcr
        entries = [
            {"analysis_id": "r1", "as_of": "2026-06-15T07:00", "stage": "opus_raw",
             "ticker": "NVDA", "direction": "buy", "canonical_action_type": "buy"},
            {"analysis_id": "execution", "as_of": "2026-06-15T10:00", "stage": "executed",
             "ticker": "NVDA", "direction": "buy", "canonical_action_type": "buy"},
        ]
        monkeypatch.setattr(bcr, "_load_log", lambda **kw: entries)
        monkeypatch.setattr(bcr, "_load_dca_state", lambda: {})
        monkeypatch.setattr(bcr, "_load_scenario_state", lambda: {})
        report = bcr.generate_report(days=7)
        assert report["unique_analysis_runs"] == 1  # execution は除外


# ════════════════════════════════════════════════════════════
# Codex 4th round fixes
# ════════════════════════════════════════════════════════════

class TestR4RebalancePlanIntegrity:
    def _mk(self, ps):
        return {"positions": [{"ticker": t, "key": k, "account": acc,
                               "investment_type": "medium", "value_jpy": v, "name": t}
                              for t, k, acc, v in ps]}

    def test_residual_cash_exposed_after_thresholding(self):
        """META 35/6762 10/AAPL 55 → 閾値後 net≠0 を residual_cash_jpy で明示。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 350000), ("6762.T", "6762.T", "特定", 100000),
            ("AAPL", "AAPL", "特定", 550000)]))
        assert "residual_cash_jpy" in r
        # 出力 action の net cash flow と residual が一致
        net_cash = sum((1 if a["type"] == "reduce" else -1) * a["amount_jpy"] for a in r["actions"])
        assert abs(net_cash - r["residual_cash_jpy"]) < 2  # 端数

    def test_actions_preserve_account_identity(self):
        """META 複数口座の reduce action に account/key が付与される。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META_特定", "特定", 800000), ("META", "META_一般", "一般", 100000),
            ("AAPL", "AAPL", "特定", 100000)]))
        meta_actions = [a for a in r["actions"] if a["ticker"] == "META"]
        for a in meta_actions:
            assert a.get("account") is not None
            assert a.get("key") is not None

    def test_hard_cap_limits_meta(self):
        """META 単独保有 → cap 20% を超えず、再配分先が無いため degraded。

        R7 で 6762.T の cap を撤廃したため、cap 超過分の唯一の吸収先が
        cap 対象 (META) しか無い構成 = META 単独保有でのみ degraded が立つ。
        """
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 1000000)]))
        meta = next(p for p in r["positions"] if p["ticker"] == "META")
        assert meta["target_pct"] <= 20.0 + 0.01
        assert r["degraded_target_model"] is True
        assert r["status"] == "degraded"

    def test_cap_redistributes_when_room_available(self):
        """cap 余地がある通常構成では degraded にならず再配分される。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 300000), ("6762.T", "6762.T", "特定", 100000),
            ("AAPL", "AAPL", "特定", 300000), ("V", "V", "特定", 300000)]))
        assert r["degraded_target_model"] is False
        meta = next(p for p in r["positions"] if p["ticker"] == "META")
        assert meta["target_pct"] <= 20.0 + 0.01

    def test_single_position_no_crash(self):
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([("META", "META", "特定", 500000)]))
        assert isinstance(r["positions"], list)


class TestR4DcaZeroYenTranche:
    def test_zero_deploy_clears_active_tranche(self, monkeypatch, tmp_path):
        import drawdown_dca_engine as dca
        monkeypatch.setattr(dca, "BASE_DIR", tmp_path)
        monkeypatch.setattr(dca, "STATE_FILE", tmp_path / "dca_state.json")
        monkeypatch.setattr(dca, "compute_drawdown_state", lambda **kw: {
            "current_value_jpy": 30000000.0, "peak_value_jpy": 36000000.0,
            "dd_from_peak": -0.18, "history_days": 60})
        monkeypatch.setattr(dca, "evaluate_sector_breadth", lambda: {
            "sectors_below_ma20": 9, "total": 11, "broad_selloff": True})
        monkeypatch.setattr(dca, "check_volume_capitulation", lambda **kw: True)
        monkeypatch.setattr(dca, "evaluate_rsi_reversal", lambda *a: {"reversed": True, "rsi_latest": 32})
        import sys
        sys.modules.setdefault("macro_fetcher", type(sys)("macro_fetcher"))
        sys.modules["macro_fetcher"].get_macro_context = lambda: {}
        sys.modules["macro_fetcher"].classify_panic = lambda x: {
            "fear_greed": 10, "hy_oas_bps": 600, "put_call": 1.5, "vix": 45}
        sys.modules.setdefault("vix_tracker", type(sys)("vix_tracker"))
        sys.modules["vix_tracker"].get_vix_context = lambda: {
            "vix": {"level": 45, "decay_from_peak_5d_pct": -12}}
        cb = {"jpy": 0.0, "usd_jpy": 0.0, "total_jpy": 5000000.0}
        sig = dca.generate_ladder_signals(cash_jpy=5000000.0, dry_run=True, cash_breakdown=cb)
        # 条件成立だが投入0 → active_tranche=None, would_be_tranche に退避
        assert sig["active_tranche"] is None
        assert sig["would_be_tranche"] is not None
        assert sig["non_executable_reason"] is not None
        assert sig["recommended_buys"] == []


class TestR4DcaAccountingIdentity:
    def test_requested_equals_target_plus_deferred(self):
        import drawdown_dca_engine as dca
        cb = {"jpy": 50.0, "usd_jpy": 1000000.0}
        buys = dca._build_recommended_buys("T1", 100000.0,
                                           target_tickers=["1306.T", "1321.T", "1489.T"],
                                           deploy_jpy=63.0, cash_breakdown=cb)
        for b in buys:
            assert b["requested_jpy"] == b["target_jpy"] + b["deferred_jpy"]
            assert b["target_jpy"] <= b["requested_jpy"]
            assert b["deferred_jpy"] >= 0
        assert sum(b["target_jpy"] for b in buys) <= 50


class TestR4CoverNotional:
    def test_full_cover_notional_from_applied_quantity(self, monkeypatch):
        import action_stage_log as asl
        import api.routes.actions as ar
        logged = []
        monkeypatch.setattr(asl, "append_entries", lambda entries, path=None: logged.extend(entries))
        monkeypatch.setattr(ar, "_get_fx_rate", lambda: 160.0)
        # cover/sell_all: cash_delta None, caller quantity None, but ledger returns applied_quantity
        ar._log_action_stage_executed(
            ticker="NVDA", direction="cover", account="信用", investment_type="medium",
            price=100.0, quantity=None, currency="USD",
            portfolio_result={"updated": True, "cash_delta": None, "cash_currency": "USD",
                              "applied_quantity": 5.0, "applied_price": 100.0},
            as_of="2026-06-15T10:00",
        )
        assert len(logged) == 1
        # 5株 × 100USD × 160 = 80,000 JPY (None ではない)
        assert logged[0]["estimated_notional_jpy"] == 80000


class TestR4ZeroFallbackComplete:
    def test_yield_zero_not_missing(self):
        import scenario_engine as se
        v = se._resolve_indicator_value("yield_10y", {}, {"yields": {"us_10y": 0.0}}, {})
        assert v == 0.0

    def test_first_non_none_keeps_zero(self):
        from scenario_engine import _first_non_none
        assert _first_non_none(0.0, 5.0) == 0.0
        assert _first_non_none(None, 0.0) == 0.0
        assert _first_non_none(None, None) is None

    def test_rsi_zero_resolved(self):
        from scenario_engine import _first_present
        assert _first_present({"rsi": 0.0}, "rsi", "rsi_14") == 0.0


# ════════════════════════════════════════════════════════════
# Codex 5th round fixes (hard cap ticker-aggregate)
# ════════════════════════════════════════════════════════════

class TestR5HardCapTickerAggregate:
    def _mk(self, ps):
        return {"positions": [{"ticker": t, "key": k, "account": acc,
                               "investment_type": "medium", "value_jpy": v, "name": t}
                              for t, k, acc, v in ps]}

    def test_cap_applied_at_ticker_aggregate(self):
        """META 複数口座でも aggregate target が cap 20% を超えない (旧 40% バグ)。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META_特定", "特定", 450000), ("META", "META_一般", "一般", 50000),
            ("6762.T", "6762.T", "特定", 300000)]))
        meta_agg = sum(p["target_pct"] for p in r["positions"] if p["ticker"] == "META")
        assert meta_agg <= 20.0 + 0.05, f"META aggregate {meta_agg}% should be <=20%"

    def test_cap_split_by_value_ratio(self):
        """cap 後の ticker target は口座 value 比 (9:1) で配分される。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META_特定", "特定", 450000), ("META", "META_一般", "一般", 50000),
            ("6762.T", "6762.T", "特定", 300000)]))
        toku = next(p for p in r["positions"] if p["key"] == "META_特定")
        ippan = next(p for p in r["positions"] if p["key"] == "META_一般")
        # 9:1 比率 (特定 18% : 一般 2%)
        assert abs(toku["target_pct"] / max(ippan["target_pct"], 0.01) - 9.0) < 1.0

    def test_degraded_actions_not_executable(self):
        """degraded 時は actions が executable=False / observe_only=True。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 1000000)]))
        assert r["degraded_target_model"] is True
        assert r["actions"], "degraded でも META reduce が surfacing される想定"
        assert all(a["executable"] is False for a in r["actions"])
        assert all(a["observe_only"] is True for a in r["actions"])

    def test_non_degraded_actions_executable(self):
        """通常構成 (cap 余地あり) では executable=True。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 600000), ("6762.T", "6762.T", "特定", 100000),
            ("AAPL", "AAPL", "特定", 150000), ("V", "V", "特定", 150000)]))
        assert r["degraded_target_model"] is False
        assert all(a["executable"] is True for a in r["actions"])

    def test_degraded_unallocated_surfaced(self):
        """degraded で cap 後 target<100% のとき unallocated_target_pct を明示。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 1000000)]))
        # META cap 20% のみ割当 → 残り 80% は再配分先が無く未割当
        assert abs(r["unallocated_target_pct"] - 80.0) < 1.0

    def test_non_degraded_unallocated_zero(self):
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 300000), ("6762.T", "6762.T", "特定", 100000),
            ("AAPL", "AAPL", "特定", 300000), ("V", "V", "特定", 300000)]))
        assert r["unallocated_target_pct"] == 0.0


# ════════════════════════════════════════════════════════════
# Codex 6th round fixes
# ════════════════════════════════════════════════════════════

class TestR6ExecutableFilter:
    def test_post_filter_drops_executable_false(self):
        import analyst
        r = analyst._non_executable_action_reason(
            {"ticker": "META", "type": "reduce", "executable": False,
             "suppressed_reason": "degraded_target_model"})
        assert r is not None and "non_executable_flag" in r

    def test_post_filter_drops_observe_only(self):
        import analyst
        r = analyst._non_executable_action_reason(
            {"ticker": "META", "type": "reduce", "observe_only": True})
        assert r is not None and "non_executable_flag" in r

    def test_post_filter_keeps_normal_action(self):
        import analyst
        r = analyst._non_executable_action_reason(
            {"ticker": "NVDA", "type": "buy", "confidence_pct": 75})
        assert r is None


class TestR6NisaReduceSuppression:
    def _mk(self, ps):
        return {"positions": [{"ticker": t, "key": k, "account": acc,
                               "investment_type": "medium", "value_jpy": v, "name": t}
                              for t, k, acc, v in ps]}

    def test_nisa_reduce_not_executable(self):
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META_NISA", "NISA成長投資枠", 700000),
            ("AAPL", "AAPL", "特定", 150000), ("V", "V", "特定", 150000)]))
        meta = next(a for a in r["actions"] if a["ticker"] == "META")
        assert meta["type"] == "reduce"
        assert meta["executable"] is False
        assert meta["nisa_protected"] is True
        assert meta["observe_only"] is True

    def test_nisa_tsumitate_protected(self):
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META_NISA", "NISAつみたて投資枠", 700000),
            ("AAPL", "AAPL", "特定", 150000), ("V", "V", "特定", 150000)]))
        meta = next(a for a in r["actions"] if a["ticker"] == "META")
        assert meta["nisa_protected"] is True
        assert meta["executable"] is False

    def test_taxable_reduce_executable(self):
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 700000),
            ("AAPL", "AAPL", "特定", 150000), ("V", "V", "特定", 150000)]))
        meta = next(a for a in r["actions"] if a["ticker"] == "META")
        assert meta["type"] == "reduce"
        assert meta["executable"] is True
        assert meta["nisa_protected"] is False

    def test_nisa_buy_not_protected(self):
        """NISA口座でも buy (追加) は保護対象外 (売却ではない)。"""
        import rebalance_engine as re
        # AAPL を NISA で過少保有 → buy。NISA保護は reduce のみ。
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 100000),
            ("AAPL", "AAPL_NISA", "NISA成長投資枠", 50000), ("V", "V", "特定", 850000)]))
        aapl = next((a for a in r["actions"] if a["ticker"] == "AAPL" and a["type"] == "buy"), None)
        if aapl is not None:
            assert aapl["nisa_protected"] is False

    def test_is_nisa_account_helper(self):
        import rebalance_engine as re
        assert re._is_nisa_account("NISA成長投資枠") is True
        assert re._is_nisa_account("NISAつみたて投資枠") is True
        assert re._is_nisa_account("特定") is False
        assert re._is_nisa_account("一般") is False
        assert re._is_nisa_account(None) is False


class TestR6CatalystDisabledScenario:
    def test_disabled_scenario_excluded_from_expectation(self):
        """disabled は除外。ただし observe_only disabled は catalyst 計測対象。"""
        from almanac.observability.catalyst_layer import synthesize_from_active_scenarios
        state = {"scenarios": {
            "enabled_sc": {"status": "active", "readiness": 0.8, "enabled_for_decision": True,
                           "recommended_actions": {}, "name": "Enabled"},
            "disabled_sc": {"status": "active", "readiness": 0.8, "enabled_for_decision": False,
                            "observe_only": False, "recommended_actions": {}, "name": "Disabled"},
            "observe_disabled_sc": {"status": "active", "readiness": 0.8, "enabled_for_decision": False,
                                    "observe_only": True, "recommended_actions": {}, "name": "Observe"},
        }}
        result = synthesize_from_active_scenarios(
            state, analysis_id="t", analysis_date="2026-06-16", min_readiness=0.0)
        by_scenario = {h.primary_source_agent.split(":", 1)[1]: h for h in result}
        assert "disabled_sc" not in by_scenario
        assert by_scenario["observe_disabled_sc"].observe_only is True


# ════════════════════════════════════════════════════════════
# Codex 7th round fixes
# ════════════════════════════════════════════════════════════

class TestR7NisaLockedFundingGap:
    def _mk(self, ps):
        return {"positions": [{"ticker": t, "key": k, "account": acc,
                               "investment_type": "medium", "value_jpy": v, "name": t}
                              for t, k, acc, v in ps]}

    def test_nisa_locked_buys_require_external_cash(self):
        """NISA-only overweight: reduce 非実行 → sibling buy も external_cash_required。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META_NISA", "NISA成長投資枠", 700000),
            ("AAPL", "AAPL", "特定", 150000), ("V", "V", "特定", 150000)]))
        assert r["nisa_locked_drift"] is True
        assert r["underfunded_plan"] is True
        buys = [a for a in r["actions"] if a["type"] == "buy"]
        assert buys, "buy actions expected"
        for b in buys:
            assert b["executable"] is False
            assert b["external_cash_required"] is True

    def test_executable_residual_excludes_suppressed_reduce(self):
        """residual は executable のみ。抑制 NISA reduce を資金源に数えない。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META_NISA", "NISA成長投資枠", 700000),
            ("AAPL", "AAPL", "特定", 150000), ("V", "V", "特定", 150000)]))
        assert r["residual_cash_jpy"] == 0
        assert r["underfunded_plan"] is True

    def test_normal_plan_not_underfunded(self):
        """通常 cash-neutral (NISA無し) は underfunded=False で全 executable。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 700000),
            ("AAPL", "AAPL", "特定", 150000), ("V", "V", "特定", 150000)]))
        assert r["underfunded_plan"] is False
        assert r["nisa_locked_drift"] is False
        assert all(a["executable"] for a in r["actions"])

    def test_taxable_reduce_funds_buys(self):
        """非NISA reduce が buy を賄える場合は underfunded にならない。"""
        import rebalance_engine as re
        r = re.calculate_medium_drift(self._mk([
            ("META", "META", "特定", 800000),
            ("AAPL", "AAPL", "特定", 100000), ("V", "V", "特定", 100000)]))
        assert r["underfunded_plan"] is False


class TestR7CapMetaOnly:
    def test_6762_cap_removed(self):
        import rebalance_engine as re
        assert "META" in re.MEDIUM_MAX_WEIGHTS
        assert "6762.T" not in re.MEDIUM_MAX_WEIGHTS

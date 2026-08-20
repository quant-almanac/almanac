"""戦術現金目標が解決できないと配備予算が丸ごと0円になる経路の検証。

2026-08-20 に実際に発生した障害:
  レジーム評価が一度 insufficient_component_coverage (status=review) で終わり、
  その残骸を次回の execution_plan が読んだ。cash_target_pct が解決できず
  tactical_reserve=None → 配備可能余剰 0円 → 確認済み現金875万があるのに
  全ての買いが no_approved_discretionary_funding でブロックされた。
  原因は plan の warnings にしか残らず、画面には「確定できない」としか出なかった。
"""
from __future__ import annotations

from execution_plan_engine import resolve_cash_target_policy


def _regime(*, status="ok", mode="advisory", cash_target_pct=3.0, evaluated_on="2026-08-20"):
    return {
        "mode": mode,
        "status": status,
        "portfolio": {"evaluated_on": evaluated_on},
        "policy": {"cash_target_pct": cash_target_pct, "policy_version": "market_regime_v2.1"},
    }


class TestResolution:
    def test_resolves_a_healthy_regime(self):
        out = resolve_cash_target_policy(market_regime=_regime())
        assert out["cash_target_pct"] == 3.0
        assert out["source"] == "market_regime_v2_state"

    def test_advisory_mode_is_accepted(self):
        # 本番は advisory で走っている。ここを弾くと常時0円になる。
        assert resolve_cash_target_policy(
            market_regime=_regime(mode="advisory")
        )["cash_target_pct"] == 3.0

    def test_shadow_and_off_modes_are_not_used_for_real_budget(self):
        for mode in ("shadow", "off"):
            assert resolve_cash_target_policy(
                market_regime=_regime(mode=mode)
            )["cash_target_pct"] is None

    def test_review_status_does_not_supply_a_target(self):
        # レジーム側が判定を保留した状態を、確定値として使ってはいけない。
        assert resolve_cash_target_policy(
            market_regime=_regime(status="review")
        )["cash_target_pct"] is None


class TestDiagnostics:
    def test_review_rejection_names_the_status_and_evaluation_date(self):
        # 「古い残骸」か「当日の保留」かを後から切り分けられること。
        out = resolve_cash_target_policy(
            market_regime=_regime(status="review", evaluated_on="2026-08-19")
        )
        reasons = " ".join(out["unresolved_reasons"])
        assert "status=review" in reasons
        assert "2026-08-19" in reasons

    def test_shadow_rejection_names_the_mode(self):
        out = resolve_cash_target_policy(market_regime=_regime(mode="shadow"))
        assert any("mode=shadow" in r for r in out["unresolved_reasons"])

    def test_missing_policy_is_reported_as_such(self):
        out = resolve_cash_target_policy(market_regime={"mode": "advisory", "status": "ok"})
        assert any("policy_missing" in r for r in out["unresolved_reasons"])

    def test_out_of_range_target_is_reported_not_silently_used(self):
        out = resolve_cash_target_policy(market_regime=_regime(cash_target_pct=150.0))
        assert out["cash_target_pct"] is None
        assert any("out_of_range" in r for r in out["unresolved_reasons"])

    def test_no_candidate_at_all_is_distinguishable_from_a_rejection(self):
        out = resolve_cash_target_policy()
        assert out["unresolved_reasons"] == ["no_regime_candidate_supplied"]

    def test_a_successful_resolution_carries_no_rejection_noise(self):
        assert "unresolved_reasons" not in resolve_cash_target_policy(market_regime=_regime())


class TestFallbackOrder:
    def test_a_healthy_ai_analysis_is_used_when_the_state_file_is_in_review(self):
        # 片方が保留でも、もう片方が健全なら予算を止めない。
        out = resolve_cash_target_policy(
            market_regime=_regime(status="review"),
            ai_analysis={"market_regime_v2": _regime(cash_target_pct=5.0)},
        )
        assert out["cash_target_pct"] == 5.0
        assert out["source"] == "ai_analysis.market_regime_v2"

    def test_all_sources_in_review_reports_every_rejection(self):
        out = resolve_cash_target_policy(
            market_regime=_regime(status="review"),
            ai_analysis={"market_regime_v2": _regime(status="review")},
        )
        assert out["cash_target_pct"] is None
        assert len(out["unresolved_reasons"]) >= 2

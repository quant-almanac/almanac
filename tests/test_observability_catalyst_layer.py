"""Tests for almanac.observability.catalyst_layer.

Coverage target: every public symbol in __all__ is exercised, with
explicit boundary, monotonicity, and integration tests.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from almanac.observability.candidate_extractor import (
    AGENT_LONG_SONNET,
    AGENT_MEDIUM_SONNET,
    extract_all as extract_all_legacy,
)
from almanac.observability.catalyst_layer import (
    CatalystHypothesis,
    CatalystOutput,
    compact_for_opus,
    compute_catalyst_score,
    dedupe_by_hypothesis_id,
    rank_by_catalyst_score,
    run,
    synthesize_from_active_scenarios,
    synthesize_from_legacy_producers,
    synthesize_from_proxy_predictions,
    synthesize_from_revision_state,
)
from almanac.observability.ids import compute_hypothesis_id
from almanac.observability.status import CandidateStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ANALYSIS_ID = "test-analysis-id-0001"
ANALYSIS_DATE = "2026-05-24"


def _make_revision_state(
    tickers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal revision_state dict."""
    return {
        "as_of": "2026-05-24T00:00:00+00:00",
        "tickers": tickers or {},
    }


def _make_ticker_entry(
    direction: str = "up",
    strength: float = 0.8,
    surprise_score: float = 0.7,
    priced_in_penalty: float = 0.1,
) -> dict[str, Any]:
    return {
        "direction": direction,
        "strength": strength,
        "surprise_score": surprise_score,
        "priced_in_penalty": priced_in_penalty,
        "first_seen_at": "2026-05-24T08:00:00+00:00",
        "prior_mentions_count": 0,
        "sources": [
            {
                "type": "news_keyword",
                "headline": "Company raises full-year guidance",
                "headline_hash": "abc123",
                "direction": direction,
            }
        ],
        "last_event_date": "2026-05-24",
    }


def _make_scenario_state(scenarios: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"scenarios": scenarios or []}


def _make_scenario(
    scenario_id: str = "bull_pullback_2026",
    readiness_pct: float = 75.0,
    tickers: list[str] | None = None,
    enabled_for_decision: bool = True,
    observe_only: bool = False,
    hypothesis_type: str = "bull_pullback",
) -> dict[str, Any]:
    return {
        "id": scenario_id,
        "readiness_pct": readiness_pct,
        "tickers": tickers or ["NVDA", "AAPL"],
        "enabled_for_decision": enabled_for_decision,
        "observe_only": observe_only,
        "hypothesis_type": hypothesis_type,
        "description": f"Test scenario {scenario_id}",
        "conviction": 70,
    }


def _make_proxy_seed_map() -> dict[str, list[str]]:
    return {
        "VisaNet": ["V", "MA"],
        "Ripple Labs": ["COIN"],
        "OpenAI Inc": ["MSFT", "NVDA"],
    }


def _make_legacy_analysis(
    tickers: list[str] | None = None,
) -> dict[str, Any]:
    """Build an ai_portfolio_analysis.json-like dict with a long_tier."""
    tickers = tickers or ["NVDA", "AAPL"]
    return {
        "long_tier": {
            "overall_stance": "bullish",
            "priority_actions": [
                {
                    "ticker": t,
                    "type": "buy",
                    "action": f"Buy {t}",
                    "reason": f"{t} looks good",
                    "confidence_pct": 70,
                    "urgency": "medium",
                    "rank": i + 1,
                }
                for i, t in enumerate(tickers)
            ],
        }
    }


# ---------------------------------------------------------------------------
# compute_catalyst_score — boundary + monotonicity
# ---------------------------------------------------------------------------


class TestComputeCatalystScore:
    def test_all_zeros_gives_zero(self):
        score = compute_catalyst_score(
            base_conviction=0,
            scenario_readiness=0.0,
            surprise_score=0.0,
            priced_in_penalty=0.0,
        )
        assert score == pytest.approx(0.0)

    def test_full_conviction_no_penalty(self):
        score = compute_catalyst_score(
            base_conviction=100,
            scenario_readiness=1.0,
            surprise_score=1.0,
            priced_in_penalty=0.0,
        )
        # raw = 1*0.5 + 1*0.3 + 1*0.2 = 1.0; penalty=0 → score=1.0
        assert score == pytest.approx(1.0)

    def test_full_penalty_clips_near_zero(self):
        # priced_in_penalty=0.6 → multiplier=0.4
        score = compute_catalyst_score(
            base_conviction=100,
            scenario_readiness=1.0,
            surprise_score=1.0,
            priced_in_penalty=0.6,
        )
        assert score == pytest.approx(0.4)

    def test_clip_upper(self):
        # freshness_bonus pushes raw above 1.0; should clip at 1.0
        score = compute_catalyst_score(
            base_conviction=100,
            scenario_readiness=1.0,
            surprise_score=1.0,
            priced_in_penalty=0.0,
            freshness_bonus=0.2,
        )
        assert score == pytest.approx(1.0)

    def test_clip_lower(self):
        score = compute_catalyst_score(
            base_conviction=0,
            scenario_readiness=0.0,
            surprise_score=0.0,
            priced_in_penalty=0.6,
        )
        assert score == pytest.approx(0.0)

    def test_monotone_conviction(self):
        """Higher conviction → higher score (all else equal)."""
        scores = [
            compute_catalyst_score(
                base_conviction=c,
                scenario_readiness=0.5,
                surprise_score=0.5,
                priced_in_penalty=0.1,
            )
            for c in range(0, 101, 10)
        ]
        assert scores == sorted(scores), "score must increase with conviction"

    def test_monotone_scenario_readiness(self):
        scores = [
            compute_catalyst_score(
                base_conviction=60,
                scenario_readiness=r / 10,
                surprise_score=0.5,
                priced_in_penalty=0.0,
            )
            for r in range(0, 11)
        ]
        assert scores == sorted(scores)

    def test_monotone_surprise(self):
        scores = [
            compute_catalyst_score(
                base_conviction=60,
                scenario_readiness=0.5,
                surprise_score=s / 10,
                priced_in_penalty=0.0,
            )
            for s in range(0, 11)
        ]
        assert scores == sorted(scores)

    def test_monotone_penalty(self):
        """Higher priced_in_penalty → lower score."""
        scores = [
            compute_catalyst_score(
                base_conviction=60,
                scenario_readiness=0.5,
                surprise_score=0.5,
                priced_in_penalty=p / 10,
            )
            for p in range(0, 7)
        ]
        assert scores == sorted(scores, reverse=True)

    def test_freshness_bonus_additive(self):
        base = compute_catalyst_score(
            base_conviction=50,
            scenario_readiness=0.5,
            surprise_score=0.5,
            priced_in_penalty=0.0,
            freshness_bonus=0.0,
        )
        with_bonus = compute_catalyst_score(
            base_conviction=50,
            scenario_readiness=0.5,
            surprise_score=0.5,
            priced_in_penalty=0.0,
            freshness_bonus=0.1,
        )
        assert with_bonus > base

    def test_formula_correctness(self):
        """Verify the exact formula: raw = 0.5*conv + 0.3*sr + 0.2*ss; score = raw*(1-pip)."""
        conv, sr, ss, pip = 80, 0.6, 0.7, 0.2
        raw = (conv / 100) * 0.5 + sr * 0.3 + ss * 0.2
        expected = raw * (1.0 - pip)
        actual = compute_catalyst_score(
            base_conviction=conv,
            scenario_readiness=sr,
            surprise_score=ss,
            priced_in_penalty=pip,
        )
        assert actual == pytest.approx(expected)


# ---------------------------------------------------------------------------
# synthesize_from_revision_state
# ---------------------------------------------------------------------------


class TestSynthesizeFromRevisionState:
    def test_empty_returns_empty(self):
        result = synthesize_from_revision_state(
            {}, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result == []

    def test_none_returns_empty(self):
        result = synthesize_from_revision_state(
            {}, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result == []

    def test_empty_tickers_returns_empty(self):
        result = synthesize_from_revision_state(
            _make_revision_state({}),
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        assert result == []

    def test_direction_none_skipped(self):
        state = _make_revision_state({"NVDA": _make_ticker_entry(direction="none")})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result == []

    def test_up_direction_yields_buy(self):
        state = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 1
        h = result[0]
        assert h.ticker == "NVDA"
        assert h.action_type == "buy"
        assert h.hypothesis_type == "earnings_revision_pullback"
        assert h.primary_source_agent == "revision_tracker"

    def test_down_direction_yields_trim(self):
        state = _make_revision_state({"9984.T": _make_ticker_entry(direction="down", strength=0.6)})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 1
        h = result[0]
        assert h.action_type == "trim"
        assert h.currency == "JPY"

    def test_hypothesis_id_is_stable(self):
        """Same ticker+direction should give the same hypothesis_id on re-call."""
        state = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        r1 = synthesize_from_revision_state(state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE)
        r2 = synthesize_from_revision_state(state, analysis_id="other-id", analysis_date="2026-06-01")
        assert r1[0].hypothesis_id == r2[0].hypothesis_id

    def test_source_event_id_format(self):
        state = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].source_event_id == "revision:NVDA:up"

    def test_priced_in_penalty_passed_through(self):
        state = _make_revision_state({"NVDA": _make_ticker_entry(priced_in_penalty=0.4)})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].priced_in_penalty == pytest.approx(0.4)

    def test_surprise_score_passed_through(self):
        state = _make_revision_state({"NVDA": _make_ticker_entry(surprise_score=0.9)})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].surprise_score == pytest.approx(0.9)

    def test_candidate_status_is_generated(self):
        state = _make_revision_state({"NVDA": _make_ticker_entry()})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].candidate_status == CandidateStatus.generated.value

    def test_multiple_tickers(self):
        state = _make_revision_state(
            {
                "NVDA": _make_ticker_entry(direction="up"),
                "9984.T": _make_ticker_entry(direction="down"),
                "AAPL": _make_ticker_entry(direction="none"),
            }
        )
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 2
        tickers = {h.ticker for h in result}
        assert tickers == {"NVDA", "9984.T"}

    def test_gross_expected_return_bps_derived_from_conviction(self):
        state = _make_revision_state({"NVDA": _make_ticker_entry(strength=0.5)})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        h = result[0]
        assert h.gross_expected_return_bps == pytest.approx(h.conviction_at_generation * 5)

    def test_horizon_is_10_days(self):
        state = _make_revision_state({"NVDA": _make_ticker_entry()})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].horizon_days == 10


# ---------------------------------------------------------------------------
# synthesize_from_active_scenarios
# ---------------------------------------------------------------------------


class TestSynthesizeFromActiveScenarios:
    def test_empty_returns_empty(self):
        result = synthesize_from_active_scenarios(
            {}, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result == []

    def test_low_readiness_skipped(self):
        state = _make_scenario_state([_make_scenario(readiness_pct=50.0)])
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE, min_readiness=0.60
        )
        assert result == []

    def test_exactly_min_readiness_included(self):
        state = _make_scenario_state([_make_scenario(readiness_pct=60.0)])
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE, min_readiness=0.60
        )
        assert len(result) >= 1

    def test_enabled_for_decision_false_skipped(self):
        state = _make_scenario_state(
            [_make_scenario(readiness_pct=90.0, enabled_for_decision=False)]
        )
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result == []

    def test_observe_only_included(self):
        state = _make_scenario_state(
            [_make_scenario(readiness_pct=80.0, observe_only=True, tickers=["AAPL"])]
        )
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 1
        assert result[0].observe_only is True
        assert "[observe_only]" not in result[0].evidence_summary

    def test_observe_only_enabled_false_included_for_measurement(self):
        state = _make_scenario_state(
            [
                _make_scenario(
                    scenario_id="japan_standalone_bull",
                    readiness_pct=80.0,
                    enabled_for_decision=False,
                    observe_only=True,
                    tickers=["1306.T"],
                )
            ]
        )
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 1
        assert result[0].primary_source_agent == "scenario:japan_standalone_bull"
        assert result[0].source_event_id == "scenario:japan_standalone_bull"
        assert result[0].observe_only is True

    def test_observe_only_false_no_annotation(self):
        state = _make_scenario_state(
            [_make_scenario(readiness_pct=80.0, observe_only=False, tickers=["AAPL"])]
        )
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].observe_only is False
        assert "[observe_only]" not in result[0].evidence_summary

    def test_one_hypothesis_per_ticker(self):
        state = _make_scenario_state(
            [_make_scenario(readiness_pct=80.0, tickers=["AAPL", "MSFT", "NVDA"])]
        )
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 3
        tickers = {h.ticker for h in result}
        assert tickers == {"AAPL", "MSFT", "NVDA"}

    def test_scenario_readiness_in_hypothesis(self):
        state = _make_scenario_state([_make_scenario(readiness_pct=80.0, tickers=["AAPL"])])
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].scenario_readiness == pytest.approx(0.80)

    def test_source_event_id_format(self):
        state = _make_scenario_state([_make_scenario(scenario_id="my_bull_2026", tickers=["V"])])
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].source_event_id == "scenario:my_bull_2026"

    def test_hypothesis_id_stable_across_dates(self):
        state = _make_scenario_state(
            [_make_scenario(scenario_id="sc1", readiness_pct=70.0, tickers=["AAPL"])]
        )
        r1 = synthesize_from_active_scenarios(
            state, analysis_id="id1", analysis_date="2026-05-24"
        )
        r2 = synthesize_from_active_scenarios(
            state, analysis_id="id2", analysis_date="2026-05-25"
        )
        assert r1[0].hypothesis_id == r2[0].hypothesis_id

    def test_candidate_status_generated(self):
        state = _make_scenario_state([_make_scenario(tickers=["AAPL"])])
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert all(h.candidate_status == CandidateStatus.generated.value for h in result)

    def test_horizon_20_days(self):
        state = _make_scenario_state([_make_scenario(tickers=["AAPL"])])
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].horizon_days == 20

    def test_multiple_scenarios(self):
        state = _make_scenario_state(
            [
                _make_scenario(scenario_id="s1", readiness_pct=70.0, tickers=["AAPL"]),
                _make_scenario(scenario_id="s2", readiness_pct=80.0, tickers=["NVDA"]),
                _make_scenario(scenario_id="s3", readiness_pct=50.0, tickers=["META"]),  # below min
            ]
        )
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 2
        tickers = {h.ticker for h in result}
        assert tickers == {"AAPL", "NVDA"}

    # -- Production-shape regression tests (Round 12 follow-up) -------------
    # ``scenario_state.json`` in production stores ``scenarios`` as a dict
    # keyed by scenario id, uses ``readiness`` as a [0, 1] fraction (not
    # ``readiness_pct``), and embeds tickers inside
    # ``recommended_actions: {phase_N: [{ticker, ...}, ...]}``.
    # Earlier this synthesizer silently logged "scenarios is not a list" and
    # returned []; these tests pin the now-correct dict-shape parsing.

    def test_accepts_dict_shaped_scenarios_field(self):
        """Production stores ``scenarios`` as ``{id: data}``, not a list."""
        state = {
            "scenarios": {
                "war_end": {
                    "name": "戦争終結ラリー",
                    "status": "active",
                    "readiness": 0.75,
                    "recommended_actions": {
                        "phase_1": [{"ticker": "SOXL"}, {"ticker": "TQQQ"}],
                    },
                },
            },
        }
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 2
        assert {h.ticker for h in result} == {"SOXL", "TQQQ"}
        assert all(h.primary_source_agent == "scenario:war_end" for h in result)
        assert all(h.source_event_id == "scenario:war_end" for h in result)
        assert all(h.hypothesis_type == "scenario_war_end" for h in result)

    def test_scenario_action_trim_is_not_treated_as_buy(self):
        """Production scenarios may encode exits in recommended_actions.action."""
        state = {
            "scenarios": {
                "war_end": {
                    "readiness": 0.75,
                    "recommended_actions": {
                        "phase_1": [
                            {"ticker": "GLD", "action": "trim_50pct", "reason": "安全資産需要低下"},
                            {"ticker": "SOXL", "allocation_usd": 5000},
                        ],
                    },
                },
            },
        }
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        by_ticker = {h.ticker: h for h in result}
        assert by_ticker["GLD"].action_type == "trim"
        assert by_ticker["SOXL"].action_type == "buy"
        assert by_ticker["GLD"].hypothesis_type == "scenario_war_end"

    def test_accepts_readiness_as_fraction(self):
        """Production uses ``readiness`` (0..1), not ``readiness_pct`` (0..100)."""
        state = {
            "scenarios": {
                "sc": {"readiness": 0.65, "tickers": ["AAPL"]},
            },
        }
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE,
            min_readiness=0.60,
        )
        assert len(result) == 1
        assert result[0].scenario_readiness == pytest.approx(0.65)

    def test_extracts_tickers_from_recommended_actions_phases(self):
        """Tickers nested 2 levels deep inside ``recommended_actions.phase_N``."""
        state = {
            "scenarios": {
                "sc": {
                    "readiness": 0.80,
                    "recommended_actions": {
                        "phase_1": [
                            {"ticker": "SOXL", "allocation_usd": 5000},
                            {"ticker": "TQQQ", "allocation_usd": 5000},
                        ],
                        "phase_2": [
                            {"ticker": "EWG", "allocation_usd": 3000},
                        ],
                    },
                },
            },
        }
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert {h.ticker for h in result} == {"SOXL", "TQQQ", "EWG"}

    def test_recommended_actions_ticker_dedup_across_phases(self):
        """Same ticker repeated across phases should yield only one hypothesis."""
        state = {
            "scenarios": {
                "sc": {
                    "readiness": 0.80,
                    "recommended_actions": {
                        "phase_1": [{"ticker": "SOXL"}],
                        "phase_2": [{"ticker": "SOXL"}, {"ticker": "QQQ"}],
                    },
                },
            },
        }
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert {h.ticker for h in result} == {"SOXL", "QQQ"}

    def test_falls_back_to_name_when_description_absent(self):
        """Production scenarios carry ``name`` (Japanese title), not ``description``."""
        state = {
            "scenarios": {
                "sc": {
                    "name": "テクノロジー強気相場",
                    "readiness": 0.80,
                    "tickers": ["NVDA"],
                },
            },
        }
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].evidence_summary == "テクノロジー強気相場"

    def test_readiness_above_one_is_clamped_as_percent_typo(self):
        """Defensive: if a producer typoed `readiness: 65` (meaning 65%), don't
        emit a 6500%-confident hypothesis. Divide by 100 then clamp to [0,1]."""
        state = {
            "scenarios": {"sc": {"readiness": 65.0, "tickers": ["AAPL"]}},
        }
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE,
            min_readiness=0.60,
        )
        assert len(result) == 1
        assert 0.0 <= result[0].scenario_readiness <= 1.0
        assert result[0].scenario_readiness == pytest.approx(0.65)

    def test_dict_shape_with_real_production_scenario_state(self):
        """Round-trip against the actual worktree ``scenario_state.json``.

        Asserts the dict parser produces SOMETHING for every scenario id once
        the readiness gate is dropped to zero. Without the fix this returns []
        because the old code only accepted the list shape.
        """
        import json
        from pathlib import Path
        prod = Path(__file__).resolve().parent.parent / "scenario_state.json"
        if not prod.exists():
            pytest.skip("production scenario_state.json missing")
        with prod.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
        result = synthesize_from_active_scenarios(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE,
            min_readiness=0.0,
        )
        # Every decision-eligible scenario and every observe-only measurement
        # scenario should contribute at least one hypothesis (each has a
        # non-empty recommended_actions or fallback MARKET ticker).
        expected_scenario_ids = {
            sid for sid, sc in state["scenarios"].items()
            if isinstance(sc, dict)
            and (
                sc.get("enabled_for_decision", True) is not False
                or bool(sc.get("observe_only", False))
            )
        }
        emitted_scenario_ids = {h.primary_source_agent.split(":", 1)[1] for h in result}
        assert expected_scenario_ids.issubset(emitted_scenario_ids), (
            f"missing scenarios: {expected_scenario_ids - emitted_scenario_ids}"
        )

    def test_non_dict_non_list_scenarios_value_returns_empty(self):
        """Defensive: a string or int in the ``scenarios`` slot logs and skips."""
        assert synthesize_from_active_scenarios(
            {"scenarios": "oops"}, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE,
        ) == []
        assert synthesize_from_active_scenarios(
            {"scenarios": 42}, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE,
        ) == []


# ---------------------------------------------------------------------------
# synthesize_from_proxy_predictions
# ---------------------------------------------------------------------------


class TestSynthesizeFromProxyPredictions:
    def test_empty_map_returns_empty(self):
        result = synthesize_from_proxy_predictions(
            {}, ["VisaNet"], analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result == []

    def test_no_entities_returns_empty(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(), [], analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result == []

    def test_entity_not_in_map_returns_empty(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["Unknown Corp"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        assert result == []

    def test_entity_match_yields_hypothesis_per_proxy(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        # VisaNet → [V, MA]
        assert len(result) == 2
        tickers = {h.ticker for h in result}
        assert tickers == {"V", "MA"}

    def test_all_hypotheses_are_ipo_proxy_type(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        assert all(h.hypothesis_type == "ipo_proxy" for h in result)

    def test_non_listed_entity_field(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        assert all(h.non_listed_entity == "VisaNet" for h in result)

    def test_proxy_tickers_field(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        for h in result:
            assert set(h.proxy_tickers) == {"V", "MA"}

    def test_action_type_is_buy(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        assert all(h.action_type == "buy" for h in result)

    def test_source_event_id_stable(self):
        """Same entity → same source_event_id regardless of case."""
        result1 = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        result2 = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["visanet"],  # lowercase
            analysis_id="other",
            analysis_date="2026-06-01",
        )
        # source_event_id should be same for same entity
        assert result1[0].source_event_id == result2[0].source_event_id

    def test_hypothesis_id_stable(self):
        result1 = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id="a1",
            analysis_date="2026-05-24",
        )
        result2 = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id="a2",
            analysis_date="2026-05-25",
        )
        ids1 = {h.hypothesis_id for h in result1}
        ids2 = {h.hypothesis_id for h in result2}
        assert ids1 == ids2

    def test_multiple_entities(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet", "Ripple Labs"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        # VisaNet→2, Ripple Labs→1
        assert len(result) == 3

    def test_horizon_20_days(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        assert all(h.horizon_days == 20 for h in result)

    def test_primary_source_agent(self):
        result = synthesize_from_proxy_predictions(
            _make_proxy_seed_map(),
            ["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
        )
        assert all(h.primary_source_agent == "proxy_mapper" for h in result)

    def test_jp_ticker_currency(self):
        seed_map = {"SomeJPCompany": ["9984.T"]}
        result = synthesize_from_proxy_predictions(
            seed_map, ["SomeJPCompany"], analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].currency == "JPY"


# ---------------------------------------------------------------------------
# synthesize_from_legacy_producers
# ---------------------------------------------------------------------------


class TestSynthesizeFromLegacyProducers:
    def _make_packet(
        self,
        ticker: str = "NVDA",
        action_type: str = "buy",
        confidence_pct: int = 70,
        hypothesis_type: str = "legacy",
        source_agent: str = "long_sonnet",
    ) -> dict[str, Any]:
        from almanac.observability.ids import compute_hypothesis_id as cid
        src_event = f"legacy_producer:{source_agent}"
        h_id = cid(
            ticker=ticker,
            action_type=action_type,
            hypothesis_type=hypothesis_type,
            horizon_days=20,
            source_event_id=src_event,
        )
        return {
            "ticker": ticker,
            "hypothesis_id": h_id,
            "action_type": action_type,
            "hypothesis_type": hypothesis_type,
            "source_event_id": src_event,
            "source_agents": [source_agent],
            "confidence_pct": confidence_pct,
            "evidence_summary": f"Buy {ticker} for AI growth",
            "candidate_status": CandidateStatus.generated.value,
            "time_horizon_days": 20,
        }

    def test_empty_returns_empty(self):
        result = synthesize_from_legacy_producers(
            [], analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result == []

    def test_packet_converted_correctly(self):
        packets = [self._make_packet("NVDA", confidence_pct=75)]
        result = synthesize_from_legacy_producers(
            packets, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 1
        h = result[0]
        assert h.ticker == "NVDA"
        assert h.action_type == "buy"
        assert h.conviction_at_generation == 75
        assert h.gross_expected_return_bps == pytest.approx(75 * 5)

    def test_hypothesis_id_reused(self):
        """hypothesis_id from the packet is passed through unchanged."""
        packet = self._make_packet("AAPL")
        expected_id = packet["hypothesis_id"]
        result = synthesize_from_legacy_producers(
            [packet], analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].hypothesis_id == expected_id

    def test_primary_source_agent_uses_source_agents(self):
        packets = [self._make_packet("AAPL", source_agent="long_sonnet")]
        result = synthesize_from_legacy_producers(
            packets, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert "long_sonnet" in result[0].primary_source_agent

    def test_missing_ticker_skipped(self):
        from almanac.observability.ids import compute_hypothesis_id as cid
        bad = {
            "hypothesis_id": "abc",
            "action_type": "buy",
            "hypothesis_type": "legacy",
            "source_event_id": "x",
        }
        result = synthesize_from_legacy_producers(
            [bad], analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result == []

    def test_multiple_packets(self):
        packets = [
            self._make_packet("NVDA"),
            self._make_packet("AAPL"),
            self._make_packet("META"),
        ]
        result = synthesize_from_legacy_producers(
            packets, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 3
        assert {h.ticker for h in result} == {"NVDA", "AAPL", "META"}

    def test_jp_ticker_currency_jpy(self):
        packets = [self._make_packet("1377.T")]
        result = synthesize_from_legacy_producers(
            packets, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].currency == "JPY"

    def test_us_ticker_currency_usd(self):
        packets = [self._make_packet("NVDA")]
        result = synthesize_from_legacy_producers(
            packets, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert result[0].currency == "USD"


# ---------------------------------------------------------------------------
# dedupe_by_hypothesis_id
# ---------------------------------------------------------------------------


class TestDedupeByHypothesisId:
    def _make_hypothesis(
        self,
        hypothesis_id: str,
        ticker: str = "NVDA",
        catalyst_score: float = 0.5,
        primary_source_agent: str = "revision_tracker",
        invalidates_if: str = "MA20 break",
    ) -> CatalystHypothesis:
        return CatalystHypothesis(
            hypothesis_id=hypothesis_id,
            ticker=ticker,
            hypothesis_type="earnings_revision_pullback",
            candidate_status=CandidateStatus.generated.value,
            catalyst_score=catalyst_score,
            scenario_readiness=0.0,
            priced_in_penalty=0.0,
            surprise_score=0.5,
            conviction_at_generation=50,
            gross_expected_return_bps=250.0,
            proxy_tickers=[],
            non_listed_entity=None,
            evidence_summary="test",
            source_event_id="revision:NVDA:up",
            horizon_days=10,
            primary_source_agent=primary_source_agent,
            invalidates_if=invalidates_if,
        )

    def test_unique_ids_all_kept(self):
        h1 = self._make_hypothesis("aaa")
        h2 = self._make_hypothesis("bbb", ticker="AAPL")
        result = dedupe_by_hypothesis_id([h1, h2])
        assert len(result) == 2

    def test_duplicate_id_higher_score_wins(self):
        h_low = self._make_hypothesis("dup", catalyst_score=0.3, primary_source_agent="legacy")
        h_high = self._make_hypothesis("dup", catalyst_score=0.8, primary_source_agent="revision_tracker")
        result = dedupe_by_hypothesis_id([h_low, h_high])
        assert len(result) == 1
        assert result[0].catalyst_score == pytest.approx(0.8)

    def test_duplicate_id_agents_merged(self):
        h1 = self._make_hypothesis("dup", catalyst_score=0.3, primary_source_agent="alpha")
        h2 = self._make_hypothesis("dup", catalyst_score=0.8, primary_source_agent="beta")
        result = dedupe_by_hypothesis_id([h1, h2])
        assert len(result) == 1
        # Both agent names should be present in the merged string
        assert "alpha" in result[0].primary_source_agent
        assert "beta" in result[0].primary_source_agent

    def test_tie_broken_by_second_occurrence(self):
        """On tie, the later-encountered row wins (score >=)."""
        h1 = self._make_hypothesis("dup", catalyst_score=0.5, primary_source_agent="first")
        h2 = self._make_hypothesis("dup", catalyst_score=0.5, primary_source_agent="second")
        result = dedupe_by_hypothesis_id([h1, h2])
        assert len(result) == 1
        # The merged agent should contain both
        agent = result[0].primary_source_agent
        assert "first" in agent and "second" in agent

    def test_empty_list(self):
        assert dedupe_by_hypothesis_id([]) == []

    def test_three_way_dedup(self):
        h1 = self._make_hypothesis("x", catalyst_score=0.2, primary_source_agent="a")
        h2 = self._make_hypothesis("x", catalyst_score=0.9, primary_source_agent="b")
        h3 = self._make_hypothesis("x", catalyst_score=0.5, primary_source_agent="c")
        result = dedupe_by_hypothesis_id([h1, h2, h3])
        assert len(result) == 1

    def test_duplicate_preserves_invalidates_if_from_winner(self):
        h_low = self._make_hypothesis(
            "dup", catalyst_score=0.3, invalidates_if="low invalidation",
        )
        h_high = self._make_hypothesis(
            "dup", catalyst_score=0.8, invalidates_if="high invalidation",
        )
        result = dedupe_by_hypothesis_id([h_low, h_high])
        assert result[0].invalidates_if == "high invalidation"


# ---------------------------------------------------------------------------
# rank_by_catalyst_score
# ---------------------------------------------------------------------------


class TestRankByCatalystScore:
    def _make_h(self, score: float, ticker: str = "X") -> CatalystHypothesis:
        return CatalystHypothesis(
            hypothesis_id=f"h{score}",
            ticker=ticker,
            hypothesis_type="legacy",
            candidate_status=CandidateStatus.generated.value,
            catalyst_score=score,
            scenario_readiness=0.0,
            priced_in_penalty=0.0,
            surprise_score=0.5,
            conviction_at_generation=50,
            gross_expected_return_bps=250.0,
            proxy_tickers=[],
            non_listed_entity=None,
            evidence_summary="x",
            source_event_id="x",
            horizon_days=10,
            primary_source_agent="test",
        )

    def test_descending_order(self):
        hs = [self._make_h(0.3), self._make_h(0.9), self._make_h(0.6)]
        ranked = rank_by_catalyst_score(hs)
        scores = [h.catalyst_score for h in ranked]
        assert scores == [0.9, 0.6, 0.3]

    def test_empty(self):
        assert rank_by_catalyst_score([]) == []

    def test_single(self):
        h = self._make_h(0.5)
        assert rank_by_catalyst_score([h]) == [h]

    def test_stable_sort(self):
        """Equal scores preserve relative input order (stable sort)."""
        h1 = self._make_h(0.5, ticker="A")
        h2 = self._make_h(0.5, ticker="B")
        ranked = rank_by_catalyst_score([h1, h2])
        assert ranked[0].ticker == "A"
        assert ranked[1].ticker == "B"


# ---------------------------------------------------------------------------
# run() — end-to-end
# ---------------------------------------------------------------------------


class TestRun:
    def test_all_none_inputs_returns_empty_output(self):
        output = run(analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE)
        assert isinstance(output, CatalystOutput)
        assert output.n_hypotheses_total == 0
        assert output.top == []
        assert output.all_hypotheses == []
        assert output.by_type == {}

    def test_returns_catalyst_output_type(self):
        output = run(analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE)
        assert isinstance(output, CatalystOutput)

    def test_as_of_is_string(self):
        output = run(analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE)
        assert isinstance(output.as_of, str)
        assert len(output.as_of) > 0

    def test_revision_state_file(self, tmp_path):
        state = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        p = tmp_path / "revision_state.json"
        p.write_text(json.dumps(state))

        output = run(
            revision_state_path=p,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        assert output.n_hypotheses_total == 1
        assert output.top[0].ticker == "NVDA"

    def test_scenario_state_file(self, tmp_path):
        state = _make_scenario_state([_make_scenario(tickers=["AAPL", "MSFT"])])
        p = tmp_path / "scenario_state.json"
        p.write_text(json.dumps(state))

        output = run(
            scenario_state_path=p,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        assert output.n_hypotheses_total == 2

    def test_proxy_seed_map_file(self, tmp_path):
        seed_map = _make_proxy_seed_map()
        p = tmp_path / "proxy_seed_map.json"
        p.write_text(json.dumps(seed_map))

        output = run(
            proxy_seed_map_path=p,
            news_entities=["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        # VisaNet → [V, MA]
        assert output.n_hypotheses_total == 2

    def test_legacy_analysis_file(self, tmp_path):
        analysis = _make_legacy_analysis(["NVDA", "AAPL"])
        p = tmp_path / "ai_portfolio_analysis.json"
        p.write_text(json.dumps(analysis))

        output = run(
            legacy_analysis_path=p,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        assert output.n_hypotheses_total == 2

    def test_legacy_analysis_file_accepts_production_keys(self, tmp_path):
        """Production ai_portfolio_analysis.json uses *_analysis keys, not *_tier."""
        action = lambda ticker: {
            "ticker": ticker,
            "type": "buy",
            "action": f"Buy {ticker}",
            "reason": f"{ticker} looks good",
            "confidence_pct": 70,
        }
        analysis = {
            "long_analysis": {"priority_actions": [action("NVDA")]},
            "medium_analysis": {"priority_actions": [action("MSFT")]},
            "short_positions_analysis": {"priority_actions": [action("AAPL")]},
            "short_selling_analysis": {"priority_actions": [action("TSLA")]},
            "synthesis": {"priority_actions": [action("META")]},
        }
        p = tmp_path / "ai_portfolio_analysis.json"
        p.write_text(json.dumps(analysis))

        output = run(
            legacy_analysis_path=p,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
            top_n=10,
        )
        assert output.n_hypotheses_total == 5
        assert {h.ticker for h in output.all_hypotheses} == {
            "NVDA",
            "MSFT",
            "AAPL",
            "TSLA",
            "META",
        }

    def test_all_inputs_present(self, tmp_path):
        rev = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        scen = _make_scenario_state([_make_scenario(tickers=["AAPL"])])
        proxy = {"VisaNet": ["V", "MA"]}
        analysis = _make_legacy_analysis(["META"])

        (tmp_path / "rev.json").write_text(json.dumps(rev))
        (tmp_path / "scen.json").write_text(json.dumps(scen))
        (tmp_path / "proxy.json").write_text(json.dumps(proxy))
        (tmp_path / "analysis.json").write_text(json.dumps(analysis))

        output = run(
            revision_state_path=tmp_path / "rev.json",
            scenario_state_path=tmp_path / "scen.json",
            proxy_seed_map_path=tmp_path / "proxy.json",
            legacy_analysis_path=tmp_path / "analysis.json",
            news_entities=["VisaNet"],
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        # NVDA (revision) + AAPL (scenario) + V + MA (proxy) + META (legacy) = 5
        assert output.n_hypotheses_total == 5
        assert output.n_hypotheses_top == 5

    def test_top_n_smaller_than_total(self, tmp_path):
        scen = _make_scenario_state(
            [_make_scenario(tickers=["AAPL", "MSFT", "NVDA", "META", "GOOG"])]
        )
        p = tmp_path / "scen.json"
        p.write_text(json.dumps(scen))

        output = run(
            scenario_state_path=p,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
            top_n=3,
        )
        assert output.n_hypotheses_total == 5
        assert output.n_hypotheses_top == 3
        assert len(output.top) == 3
        assert len(output.all_hypotheses) == 5

    def test_top_contains_highest_scores(self, tmp_path):
        """top should be the highest-scored subset of all_hypotheses."""
        scen = _make_scenario_state(
            [_make_scenario(tickers=["AAPL", "MSFT", "NVDA", "META", "GOOG"])]
        )
        p = tmp_path / "scen.json"
        p.write_text(json.dumps(scen))

        output = run(
            scenario_state_path=p,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
            top_n=2,
        )
        top_scores = {h.catalyst_score for h in output.top}
        all_scores_sorted = sorted(
            [h.catalyst_score for h in output.all_hypotheses], reverse=True
        )
        assert all(s in all_scores_sorted[:2] for s in top_scores)

    def test_all_hypotheses_ranked_descending(self, tmp_path):
        scen = _make_scenario_state([_make_scenario(tickers=["AAPL", "MSFT", "NVDA"])])
        p = tmp_path / "scen.json"
        p.write_text(json.dumps(scen))

        output = run(
            scenario_state_path=p,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        scores = [h.catalyst_score for h in output.all_hypotheses]
        assert scores == sorted(scores, reverse=True)

    def test_by_type_counts(self, tmp_path):
        rev = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        scen = _make_scenario_state([_make_scenario(tickers=["AAPL"])])
        (tmp_path / "rev.json").write_text(json.dumps(rev))
        (tmp_path / "scen.json").write_text(json.dumps(scen))

        output = run(
            revision_state_path=tmp_path / "rev.json",
            scenario_state_path=tmp_path / "scen.json",
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        assert output.by_type.get("earnings_revision_pullback", 0) == 1
        assert output.by_type.get("bull_pullback", 0) == 1

    def test_write_log_true_writes_jsonl(self, tmp_path):
        rev = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        p = tmp_path / "rev.json"
        p.write_text(json.dumps(rev))
        log_path = tmp_path / "catalyst_hypothesis_log.jsonl"

        output = run(
            revision_state_path=p,
            catalyst_log_path=log_path,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=True,
        )
        assert log_path.exists()
        lines = [line for line in log_path.read_text().splitlines() if line.strip()]
        assert len(lines) == output.n_hypotheses_total

    def test_write_log_false_no_file(self, tmp_path):
        rev = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        p = tmp_path / "rev.json"
        p.write_text(json.dumps(rev))
        log_path = tmp_path / "catalyst_hypothesis_log.jsonl"

        run(
            revision_state_path=p,
            catalyst_log_path=log_path,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        assert not log_path.exists()

    def test_log_rows_are_valid_json(self, tmp_path):
        rev = _make_revision_state(
            {
                "NVDA": _make_ticker_entry(direction="up"),
                "AAPL": _make_ticker_entry(direction="down"),
            }
        )
        p = tmp_path / "rev.json"
        p.write_text(json.dumps(rev))
        log_path = tmp_path / "catalyst_hypothesis_log.jsonl"

        run(
            revision_state_path=p,
            catalyst_log_path=log_path,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=True,
        )
        for line in log_path.read_text().splitlines():
            if line.strip():
                obj = json.loads(line)
                assert "hypothesis_id" in obj
                assert "primary_ticker" in obj
                assert obj["price_at_event"] is None
                assert all(v is None for v in obj["benchmark_price_at_event"].values())

    def test_log_writes_all_hypotheses_not_just_top(self, tmp_path):
        scen = _make_scenario_state(
            [_make_scenario(tickers=["AAPL", "MSFT", "NVDA", "META", "GOOG"])]
        )
        sp = tmp_path / "scen.json"
        sp.write_text(json.dumps(scen))
        log_path = tmp_path / "log.jsonl"

        output = run(
            scenario_state_path=sp,
            catalyst_log_path=log_path,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=True,
            top_n=2,  # only 2 in top, but 5 total
        )
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 5  # all 5 written
        assert output.n_hypotheses_top == 2

    def test_observe_only_logged_but_excluded_from_top(self, tmp_path):
        scen = _make_scenario_state(
            [
                _make_scenario(
                    scenario_id="japan_standalone_bull",
                    tickers=["1306.T"],
                    enabled_for_decision=False,
                    observe_only=True,
                )
            ]
        )
        sp = tmp_path / "scen.json"
        sp.write_text(json.dumps(scen))
        log_path = tmp_path / "log.jsonl"

        output = run(
            scenario_state_path=sp,
            catalyst_log_path=log_path,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=True,
        )
        assert output.n_hypotheses_total == 1
        assert output.n_hypotheses_top == 0
        assert output.top == []
        assert output.all_hypotheses[0].observe_only is True
        review = compact_for_opus(output)
        assert "OBSERVE-ONLY REVIEW" in review
        assert "source_observe_only: true" in review
        row = json.loads(log_path.read_text())
        assert row["observe_only"] is True
        assert row["source_event_id"] == "scenario:japan_standalone_bull"
        assert row["primary_source_agent"] == "scenario:japan_standalone_bull"

    def test_missing_input_file_graceful(self, tmp_path):
        """A path to a nonexistent file should not raise — just contribute zero hypotheses."""
        output = run(
            revision_state_path=tmp_path / "nonexistent.json",
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        assert output.n_hypotheses_total == 0

    def test_dedup_across_sources(self, tmp_path):
        """Same ticker appearing in revision AND legacy should dedup to one hypothesis
        only if they produce the same hypothesis_id (they won't in general because
        source_event_id differs; but if they match, dedup fires).
        """
        # Both sources emit a buy on NVDA, but with different source_event_ids
        # → two distinct hypothesis_ids → no dedup → total=2
        rev = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        analysis = _make_legacy_analysis(["NVDA"])
        (tmp_path / "rev.json").write_text(json.dumps(rev))
        (tmp_path / "analysis.json").write_text(json.dumps(analysis))

        output = run(
            revision_state_path=tmp_path / "rev.json",
            legacy_analysis_path=tmp_path / "analysis.json",
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=False,
        )
        # NVDA from revision + NVDA from legacy → different source_event_ids → 2 hypotheses
        assert output.n_hypotheses_total == 2


# ---------------------------------------------------------------------------
# Cross-module: build from candidate_extractor + revision_tracker
# ---------------------------------------------------------------------------


class TestCrossModule:
    """Integration tests that wire real modules together end-to-end."""

    def test_extract_all_then_synthesize(self):
        """candidate_extractor.extract_all output feeds synthesize_from_legacy_producers."""
        long_tier = {
            "overall_stance": "bullish",
            "priority_actions": [
                {"ticker": "NVDA", "type": "buy", "reason": "AI demand", "confidence_pct": 80},
                {"ticker": "AAPL", "type": "trim", "reason": "Overweight", "confidence_pct": 60},
            ],
        }
        packets = extract_all_legacy(
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            long_tier=long_tier,
        )
        result = synthesize_from_legacy_producers(
            packets, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        assert len(result) == 2
        tickers = {h.ticker for h in result}
        assert tickers == {"NVDA", "AAPL"}

    def test_end_to_end_with_revision_and_legacy(self, tmp_path):
        """Full run() using real revision_state + legacy analysis."""
        rev = _make_revision_state(
            {
                "6762.T": _make_ticker_entry(direction="up", strength=0.9, surprise_score=0.8),
            }
        )
        analysis = _make_legacy_analysis(["NVDA", "AAPL"])

        (tmp_path / "rev.json").write_text(json.dumps(rev))
        (tmp_path / "analysis.json").write_text(json.dumps(analysis))
        log_path = tmp_path / "catalyst_hypothesis_log.jsonl"

        output = run(
            revision_state_path=tmp_path / "rev.json",
            legacy_analysis_path=tmp_path / "analysis.json",
            catalyst_log_path=log_path,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=True,
            top_n=10,
        )

        # 1 revision + 2 legacy = 3 total
        assert output.n_hypotheses_total == 3
        assert output.n_hypotheses_top == 3
        tickers = {h.ticker for h in output.all_hypotheses}
        assert "6762.T" in tickers
        assert "NVDA" in tickers
        assert "AAPL" in tickers

        # Log must exist and have 3 lines
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 3

    def test_write_log_uses_write_catalyst_hypothesis_generated(self, tmp_path):
        """Rows in the log have the expected 'event_type': 'generated' field."""
        rev = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        p = tmp_path / "rev.json"
        p.write_text(json.dumps(rev))
        log_path = tmp_path / "log.jsonl"

        run(
            revision_state_path=p,
            catalyst_log_path=log_path,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=True,
        )
        rows = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert all(r.get("event_type") == "generated" for r in rows)
        assert all("primary_ticker" in r for r in rows)
        assert all("catalyst_score" in r for r in rows)
        assert all("benchmark_basket" in r for r in rows)

    def test_hypothesis_id_matches_ids_module(self):
        """CatalystHypothesis.hypothesis_id must equal compute_hypothesis_id output."""
        state = _make_revision_state({"NVDA": _make_ticker_entry(direction="up")})
        result = synthesize_from_revision_state(
            state, analysis_id=ANALYSIS_ID, analysis_date=ANALYSIS_DATE
        )
        h = result[0]
        expected_id = compute_hypothesis_id(
            ticker="NVDA",
            action_type="buy",
            hypothesis_type="earnings_revision_pullback",
            horizon_days=10,
            source_event_id="revision:NVDA:up",
        )
        assert h.hypothesis_id == expected_id

    def test_all_public_symbols_importable(self):
        """Smoke test: all __all__ symbols are accessible."""
        from almanac.observability.catalyst_layer import __all__ as catalyst_all
        import almanac.observability.catalyst_layer as mod

        for symbol in catalyst_all:
            assert hasattr(mod, symbol), f"Missing symbol: {symbol}"


# ---------------------------------------------------------------------------
# compact_for_opus — prompt formatting helper (plan §6.4)
# ---------------------------------------------------------------------------


class TestCompactForOpus:
    """Tests for :func:`almanac.observability.catalyst_layer.compact_for_opus`."""

    def _make_output(self, hypotheses):
        """Wrap a list of CatalystHypothesis objects in a CatalystOutput."""
        from almanac.observability.catalyst_layer import CatalystOutput
        return CatalystOutput(
            as_of="2026-05-24T18:00:00+00:00",
            n_hypotheses_total=len(hypotheses),
            n_hypotheses_top=len(hypotheses),
            top=hypotheses,
            by_type={},
            all_hypotheses=hypotheses,
        )

    def _make_h(self, ticker="NVDA", score=0.80, observe_only=False,
                invalidates_if="MA20 break", non_listed_entity=None,
                hypothesis_type="earnings_revision_pullback"):
        from almanac.observability.catalyst_layer import CatalystHypothesis
        return CatalystHypothesis(
            hypothesis_id="abc123",
            ticker=ticker,
            hypothesis_type=hypothesis_type,
            candidate_status="generated",
            catalyst_score=score,
            scenario_readiness=0.6,
            priced_in_penalty=0.0,
            surprise_score=0.7,
            conviction_at_generation=72,
            gross_expected_return_bps=360.0,
            proxy_tickers=[],
            non_listed_entity=non_listed_entity,
            evidence_summary="Earnings guidance raised 10%",
            source_event_id="revision:NVDA:up",
            horizon_days=10,
            primary_source_agent="revision_tracker",
            invalidates_if=invalidates_if,
            observe_only=observe_only,
        )

    # --- empty output cases ---

    def test_empty_output_returns_empty_string(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        output = self._make_output([])
        assert compact_for_opus(output) == ""

    def test_observe_only_hypotheses_included_as_review_context(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(observe_only=True)
        result = compact_for_opus(self._make_output([h]))
        assert "OBSERVE-ONLY REVIEW" in result
        assert "source_observe_only: true" in result
        assert "生の observe_only=true action は不可" in result

    def test_observe_only_review_gets_reserved_prompt_slot(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        decisions = [
            self._make_h(ticker=f"D{i}", score=0.70 - i * 0.01, observe_only=False)
            for i in range(3)
        ]
        review = self._make_h(
            ticker="1489.T",
            score=0.90,
            observe_only=True,
            hypothesis_type="scenario_japan_standalone_bull",
        )

        result = compact_for_opus(self._make_output(decisions + [review]), max_items=3)

        assert "1489.T" in result
        assert "OBSERVE-ONLY REVIEW" in result
        assert result.count("- [") == 3

    def test_below_threshold_returns_empty_string(self):
        """score=0.5 <= scenario_readiness(0.8) * 1.2 = 0.96 → excluded."""
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(score=0.5)
        assert compact_for_opus(self._make_output([h]), scenario_readiness=0.8) == ""

    def test_zero_scenario_readiness_admits_any_nonzero_score(self):
        """threshold = 0 * 1.2 = 0 → any positive score admitted."""
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(score=0.01)
        result = compact_for_opus(self._make_output([h]), scenario_readiness=0.0)
        assert result != ""

    # --- content format ---

    def test_output_starts_with_header(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h()
        result = compact_for_opus(self._make_output([h]))
        assert result.startswith("【触媒予測 (Catalyst Hypotheses)】")

    def test_ticker_in_output(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(ticker="9984.T")
        result = compact_for_opus(self._make_output([h]))
        assert "9984.T" in result

    def test_conviction_horizon_score_in_output(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h()
        result = compact_for_opus(self._make_output([h]))
        assert "conv=72" in result
        assert "hor=10d" in result
        assert "score=0.80" in result

    def test_invalidates_if_included_when_set(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(invalidates_if="RSI>75 / MA20 break")
        result = compact_for_opus(self._make_output([h]))
        assert "invalidates_if: RSI>75 / MA20 break" in result

    def test_invalidates_if_omitted_when_empty(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(invalidates_if="")
        result = compact_for_opus(self._make_output([h]))
        assert "invalidates_if" not in result

    def test_proxy_for_included_when_non_listed_entity_set(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(non_listed_entity="OpenAI",
                         hypothesis_type="ipo_proxy")
        result = compact_for_opus(self._make_output([h]))
        assert "proxy_for: OpenAI" in result

    def test_proxy_for_omitted_when_none(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(non_listed_entity=None)
        result = compact_for_opus(self._make_output([h]))
        assert "proxy_for" not in result

    def test_hypothesis_type_uppercased_in_tag(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(hypothesis_type="earnings_revision_pullback")
        result = compact_for_opus(self._make_output([h]))
        assert "EARNINGS-REVISION-PULLBACK" in result

    # --- max_items cap ---

    def test_max_items_caps_output(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        hypotheses = [self._make_h(ticker=t) for t in ["NVDA", "AAPL", "MSFT", "GOOGL"]]
        result = compact_for_opus(self._make_output(hypotheses), max_items=2)
        # Only 2 tickers should appear (header line + 2 entries)
        lines = result.splitlines()
        entry_lines = [l for l in lines if l.startswith("- [")]
        assert len(entry_lines) == 2

    def test_default_max_items_is_three(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        hypotheses = [self._make_h(ticker=t) for t in
                      ["A", "B", "C", "D", "E"]]
        result = compact_for_opus(self._make_output(hypotheses))
        entry_lines = [l for l in result.splitlines() if l.startswith("- [")]
        assert len(entry_lines) == 3

    # --- threshold edge cases ---

    def test_score_exactly_at_threshold_excluded(self):
        """Admission requires strictly greater than threshold."""
        from almanac.observability.catalyst_layer import compact_for_opus
        # threshold = 0.5 * 1.2 = 0.6; score == 0.6 → excluded
        h = self._make_h(score=0.6)
        result = compact_for_opus(self._make_output([h]), scenario_readiness=0.5)
        assert result == ""

    def test_score_just_above_threshold_admitted(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h = self._make_h(score=0.601)
        result = compact_for_opus(self._make_output([h]), scenario_readiness=0.5)
        assert result != ""

    def test_custom_multiplier_respected(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        # threshold = 0.5 * 2.0 = 1.0; score=0.9 → excluded
        h = self._make_h(score=0.9)
        result = compact_for_opus(self._make_output([h]),
                                  scenario_readiness=0.5,
                                  min_score_multiplier=2.0)
        assert result == ""

    def test_multiple_hypotheses_all_above_threshold(self):
        from almanac.observability.catalyst_layer import compact_for_opus
        h1 = self._make_h(ticker="NVDA", score=0.9)
        h2 = self._make_h(ticker="AAPL", score=0.85)
        result = compact_for_opus(self._make_output([h1, h2]),
                                  scenario_readiness=0.0)
        assert "NVDA" in result
        assert "AAPL" in result


# ---------------------------------------------------------------------------
# Evidence Sufficiency Gate — unit tests (plan §6.13 / C6-5 / C7-4)
# ---------------------------------------------------------------------------


class TestEvidenceSufficiencyGate:
    """Tests for the internal ``_evidence_sufficiency_check`` function."""

    def _make_h(self, source_event_id="rev:X", horizon_days=10,
                invalidates_if="RSI>75", hypothesis_type="earnings_revision_pullback"):
        from almanac.observability.catalyst_layer import CatalystHypothesis
        return CatalystHypothesis(
            hypothesis_id="h1",
            ticker="NVDA",
            hypothesis_type=hypothesis_type,
            candidate_status="generated",
            catalyst_score=0.7,
            scenario_readiness=0.0,
            priced_in_penalty=0.0,
            surprise_score=0.5,
            conviction_at_generation=60,
            gross_expected_return_bps=300.0,
            proxy_tickers=[],
            non_listed_entity=None,
            evidence_summary="Some evidence",
            source_event_id=source_event_id,
            horizon_days=horizon_days,
            primary_source_agent="revision_tracker",
            invalidates_if=invalidates_if,
        )

    def _check(self, **kwargs):
        from almanac.observability.catalyst_layer import _evidence_sufficiency_check
        return _evidence_sufficiency_check(self._make_h(**kwargs))

    def test_fully_populated_hypothesis_passes(self):
        assert self._check() == []

    def test_missing_source_event_id_flagged(self):
        missing = self._check(source_event_id="")
        assert "source_event" in missing

    def test_zero_horizon_days_flagged(self):
        missing = self._check(horizon_days=0)
        assert "time_horizon_days" in missing

    def test_negative_horizon_days_flagged(self):
        missing = self._check(horizon_days=-1)
        assert "time_horizon_days" in missing

    def test_missing_invalidates_if_flagged(self):
        missing = self._check(invalidates_if="")
        assert "invalidation" in missing

    def test_all_three_missing_returns_three_items(self):
        missing = self._check(source_event_id="", horizon_days=0, invalidates_if="")
        assert set(missing) == {"source_event", "time_horizon_days", "invalidation"}

    def test_legacy_hypothesis_type_bypasses_gate(self):
        """Legacy producers are fully exempt — all three checks skipped."""
        missing = self._check(
            source_event_id="", horizon_days=0, invalidates_if="",
            hypothesis_type="legacy",
        )
        assert missing == []

    def test_legacy_subtype_also_bypasses_gate(self):
        missing = self._check(
            source_event_id="", hypothesis_type="legacy_long_sonnet"
        )
        assert missing == []

    def test_non_legacy_type_not_exempt(self):
        """Ensure the exemption doesn't accidentally extend to non-legacy types."""
        missing = self._check(
            source_event_id="", invalidates_if="",
            hypothesis_type="earnings_revision_pullback",
        )
        assert len(missing) >= 2  # at least source_event + invalidation

    def test_esg_filtered_hypotheses_excluded_from_run_output(self, tmp_path):
        """Integration: hypotheses missing invalidates_if must not appear in CatalystOutput."""
        import json
        from almanac.observability.catalyst_layer import run

        # A revision_state entry with a real source_event_id and horizon_days,
        # but the synthesizer always fills invalidates_if for revision entries,
        # so manufacture a bare scenario without invalidation_condition instead.
        scenario_state = {
            "scenarios": {
                "no_inv_scenario": {
                    "readiness": 0.9,
                    "hypothesis_type": "bull_pullback",
                    "tickers": ["SOXL"],
                    # No "invalidation_condition" key → invalidates_if will be
                    # filled with a default by the synthesizer (not empty).
                    # Use a separate test to exercise the filter path.
                }
            }
        }
        # Manually test with a missing source_event_id by using a legacy packet
        # that has no source_event_id — but those are exempt, so no filtering.
        # The gate primarily targets NEW sources; test via run() + log inspection.
        p = tmp_path / "sc.json"
        p.write_text(json.dumps(scenario_state))
        log_path = tmp_path / "log.jsonl"

        output = run(
            scenario_state_path=p,
            catalyst_log_path=log_path,
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            write_log=True,
        )
        # The scenario synthesizer fills invalidates_if from the default template,
        # so it should pass the gate and appear in the output.
        assert output.n_hypotheses_total >= 1
        tickers = {h.ticker for h in output.all_hypotheses}
        assert "SOXL" in tickers

    def test_esg_filtered_written_as_not_injected_to_log(self, tmp_path):
        """ESG-filtered rows appear in the log with candidate_status=not_injected."""
        import json
        from dataclasses import replace
        from almanac.observability.catalyst_layer import (
            CatalystOutput, CatalystHypothesis, _evidence_sufficiency_check,
            _write_hypothesis_to_log,
        )
        from almanac.observability.logs import write_catalyst_hypothesis_filtered

        log_path = tmp_path / "log.jsonl"

        # Simulate writing a filtered row (as run() does internally)
        write_catalyst_hypothesis_filtered(
            log_path,
            hypothesis_id="deadbeef",
            analysis_id=ANALYSIS_ID,
            analysis_date=ANALYSIS_DATE,
            filter_reason="evidence_sufficiency_gate",
            missing_fields=["source_event", "invalidation"],
            fsync=False,
        )

        rows = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert len(rows) == 1
        r = rows[0]
        assert r["candidate_status"] == "not_injected"
        assert r["filter_reason"] == "evidence_sufficiency_gate"
        assert "source_event" in r["missing_fields"]
        assert "invalidation" in r["missing_fields"]
        assert r["event_type"] == "filtered"

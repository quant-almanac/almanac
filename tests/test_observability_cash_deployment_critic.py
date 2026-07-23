"""Tests for almanac.observability.cash_deployment_critic.

All three conditions for firing:
  1. scenario_key is a known bull scenario
  2. cash_ratio > CASH_RATIO_THRESHOLD
  3. adopted_candidates == 0

Coverage:
- evaluate() fires / does not fire for every combination of conditions
- format_opus_warning() content and encoding
- write_to_log() creates valid JSONL rows with correct fields
- _make_cash_decision_id() is deterministic and stable
- Custom bull_scenario_keys override
- cash_ratio exactly at threshold (boundary)
- Integration: triggered result → warning appended to prompt string
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.cash_deployment_critic import (  # noqa: E402
    BULL_SCENARIO_KEYS,
    CASH_RATIO_THRESHOLD,
    CashDeploymentResult,
    evaluate,
    format_opus_warning,
    write_to_log,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

ANALYSIS_ID = "aaaaaaaa-0000-0000-0000-000000000001"
ANALYSIS_DATE = "2026-05-24"
BULL_SCENARIO = "bull_pullback"
NON_BULL_SCENARIO = "defensive_cash"


# ---------------------------------------------------------------------------
# evaluate() — trigger logic
# ---------------------------------------------------------------------------


class TestEvaluateTrigger:
    """Test the three-condition trigger logic."""

    def test_all_conditions_met_triggers(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.30,
            adopted_candidates=0,
        )
        assert result.triggered is True

    def test_non_bull_scenario_does_not_trigger(self):
        result = evaluate(
            scenario_key=NON_BULL_SCENARIO,
            cash_ratio=0.50,
            adopted_candidates=0,
        )
        assert result.triggered is False

    def test_cash_ratio_below_threshold_does_not_trigger(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=CASH_RATIO_THRESHOLD - 0.01,
            adopted_candidates=0,
        )
        assert result.triggered is False

    def test_cash_ratio_exactly_at_threshold_does_not_trigger(self):
        """Admission requires strictly greater-than (not >=)."""
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=CASH_RATIO_THRESHOLD,
            adopted_candidates=0,
        )
        assert result.triggered is False

    def test_cash_ratio_just_above_threshold_triggers(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=CASH_RATIO_THRESHOLD + 0.001,
            adopted_candidates=0,
        )
        assert result.triggered is True

    def test_adopted_candidates_nonzero_does_not_trigger(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.50,
            adopted_candidates=1,
        )
        assert result.triggered is False

    def test_zero_cash_ratio_does_not_trigger(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.0,
            adopted_candidates=0,
        )
        assert result.triggered is False

    def test_all_known_bull_scenarios_can_trigger(self):
        for key in BULL_SCENARIO_KEYS:
            result = evaluate(
                scenario_key=key,
                cash_ratio=0.50,
                adopted_candidates=0,
            )
            assert result.triggered is True, f"Expected trigger for scenario {key!r}"

    def test_custom_bull_keys_override(self):
        custom = frozenset({"my_custom_bull"})
        # Known bull scenario should NOT trigger with custom override
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.50,
            adopted_candidates=0,
            bull_scenario_keys=custom,
        )
        assert result.triggered is False

        # Custom key SHOULD trigger
        result2 = evaluate(
            scenario_key="my_custom_bull",
            cash_ratio=0.50,
            adopted_candidates=0,
            bull_scenario_keys=custom,
        )
        assert result2.triggered is True

    def test_custom_threshold_respected(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.15,
            adopted_candidates=0,
            cash_ratio_threshold=0.10,  # lower threshold
        )
        assert result.triggered is True

    def test_generated_candidates_recorded_but_not_gating(self):
        """generated_candidates is context-only; it must not affect trigger."""
        r_zero = evaluate(
            scenario_key=BULL_SCENARIO, cash_ratio=0.30,
            adopted_candidates=0, generated_candidates=0,
        )
        r_many = evaluate(
            scenario_key=BULL_SCENARIO, cash_ratio=0.30,
            adopted_candidates=0, generated_candidates=100,
        )
        assert r_zero.triggered == r_many.triggered


# ---------------------------------------------------------------------------
# evaluate() — result fields
# ---------------------------------------------------------------------------


class TestEvaluateResultFields:
    """Verify the fields of CashDeploymentResult."""

    def test_triggered_result_has_warning_text(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.30,
            adopted_candidates=0,
        )
        assert result.warning_text != ""
        assert BULL_SCENARIO in result.warning_text

    def test_not_triggered_result_has_empty_warning_text(self):
        result = evaluate(
            scenario_key=NON_BULL_SCENARIO,
            cash_ratio=0.50,
            adopted_candidates=0,
        )
        assert result.warning_text == ""

    def test_scenario_key_preserved(self):
        result = evaluate(
            scenario_key="tech_boom",
            cash_ratio=0.30,
            adopted_candidates=0,
        )
        assert result.scenario_key == "tech_boom"

    def test_active_bull_scenarios_populated_when_bull(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.30,
            adopted_candidates=0,
        )
        assert BULL_SCENARIO in result.active_bull_scenarios

    def test_active_bull_scenarios_empty_when_non_bull(self):
        result = evaluate(
            scenario_key=NON_BULL_SCENARIO,
            cash_ratio=0.50,
            adopted_candidates=0,
        )
        assert result.active_bull_scenarios == []

    def test_cash_ratio_preserved(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.42,
            adopted_candidates=0,
        )
        assert result.cash_ratio == pytest.approx(0.42)

    def test_threshold_preserved_in_result(self):
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.30,
            adopted_candidates=0,
            cash_ratio_threshold=0.15,
        )
        assert result.cash_ratio_threshold == pytest.approx(0.15)

    def test_adopted_and_generated_candidates_preserved(self):
        result = evaluate(
            scenario_key=NON_BULL_SCENARIO,
            cash_ratio=0.50,
            adopted_candidates=3,
            generated_candidates=8,
        )
        assert result.adopted_candidates == 3
        assert result.generated_candidates == 8


# ---------------------------------------------------------------------------
# format_opus_warning()
# ---------------------------------------------------------------------------


class TestFormatOpusWarning:
    def test_starts_with_warning_emoji(self):
        text = format_opus_warning("bull_pullback", 0.34)
        assert text.startswith("\n⚠️")

    def test_scenario_key_in_text(self):
        text = format_opus_warning("tech_boom", 0.25)
        assert "tech_boom" in text

    def test_cash_ratio_formatted_as_percent(self):
        text = format_opus_warning("bull_pullback", 0.34)
        assert "34%" in text

    def test_rejection_notes_instruction_present(self):
        text = format_opus_warning("bull_pullback", 0.34)
        assert "rejection_notes" in text

    def test_text_is_reasonably_short(self):
        """Warn text should not bloat Opus prompt (< 200 chars)."""
        text = format_opus_warning("bull_pullback", 0.34)
        assert len(text) < 200


# ---------------------------------------------------------------------------
# write_to_log()
# ---------------------------------------------------------------------------


class TestWriteToLog:
    def _triggered_result(self, cash_ratio=0.30):
        return evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=cash_ratio,
            adopted_candidates=0,
            generated_candidates=5,
        )

    def test_creates_file_when_absent(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        assert log.exists()

    def test_appends_single_row(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        assert len(rows) == 1

    def test_row_event_type_is_critic_triggered(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        row = json.loads(log.read_text().splitlines()[0])
        assert row["event_type"] == "critic_triggered"

    def test_row_contains_cash_ratio(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result(cash_ratio=0.42)
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        row = json.loads(log.read_text().splitlines()[0])
        assert row["cash_ratio"] == pytest.approx(0.42)

    def test_row_contains_active_bull_scenarios(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        row = json.loads(log.read_text().splitlines()[0])
        assert BULL_SCENARIO in row["active_bull_scenarios"]

    def test_row_contains_analysis_date(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        row = json.loads(log.read_text().splitlines()[0])
        assert row["analysis_date"] == ANALYSIS_DATE

    def test_row_contains_opus_no_buy_reason(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE,
                     opus_no_buy_reason="VIX too high",
                     fsync=False)
        row = json.loads(log.read_text().splitlines()[0])
        assert row["opus_no_buy_reason"] == "VIX too high"

    def test_row_has_benchmark_basket(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE,
                     benchmark_basket=["QQQ", "SPY"],
                     benchmark_weights=[0.7, 0.3],
                     fsync=False)
        row = json.loads(log.read_text().splitlines()[0])
        assert row["benchmark_basket"] == ["QQQ", "SPY"]
        assert row["benchmark_weights"] == [0.7, 0.3]

    def test_default_benchmark_vt_agg(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        row = json.loads(log.read_text().splitlines()[0])
        assert "VT" in row["benchmark_basket"]
        assert "AGG" in row["benchmark_basket"]

    def test_returns_stable_cash_decision_id(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result(cash_ratio=0.30)
        id1 = write_to_log(log, result, analysis_id=ANALYSIS_ID,
                           analysis_date=ANALYSIS_DATE, fsync=False)
        log2 = tmp_path / "cash2.jsonl"
        id2 = write_to_log(log2, result, analysis_id=ANALYSIS_ID,
                           analysis_date=ANALYSIS_DATE, fsync=False)
        assert id1 == id2  # deterministic

    def test_cash_decision_id_in_row(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        cid = write_to_log(log, result, analysis_id=ANALYSIS_ID,
                           analysis_date=ANALYSIS_DATE, fsync=False)
        row = json.loads(log.read_text().splitlines()[0])
        assert row["cash_decision_id"] == cid

    def test_multiple_appends_grow_file(self, tmp_path):
        log = tmp_path / "cash.jsonl"
        result = self._triggered_result()
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        rows = [l for l in log.read_text().splitlines() if l.strip()]
        assert len(rows) == 2

    def test_not_triggered_result_still_written(self, tmp_path):
        """Non-triggered results can also be logged for completeness."""
        log = tmp_path / "cash.jsonl"
        result = evaluate(
            scenario_key=NON_BULL_SCENARIO,
            cash_ratio=0.10,
            adopted_candidates=2,
        )
        write_to_log(log, result, analysis_id=ANALYSIS_ID,
                     analysis_date=ANALYSIS_DATE, fsync=False)
        rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        assert len(rows) == 1
        assert rows[0]["event_type"] == "critic_triggered"


# ---------------------------------------------------------------------------
# Integration: warning appended to Opus prompt string
# ---------------------------------------------------------------------------


class TestIntegrationWithPrompt:
    def test_triggered_warning_can_be_appended_to_prompt(self):
        prompt = "以下を分析してください。"
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.30,
            adopted_candidates=0,
        )
        if result.triggered:
            prompt += result.warning_text
        assert "⚠️" in prompt
        assert BULL_SCENARIO in prompt
        assert "rejection_notes" in prompt

    def test_not_triggered_prompt_unchanged(self):
        prompt = "以下を分析してください。"
        result = evaluate(
            scenario_key=NON_BULL_SCENARIO,
            cash_ratio=0.05,
            adopted_candidates=0,
        )
        if result.triggered:
            prompt += result.warning_text
        assert prompt == "以下を分析してください。"

    def test_evaluate_result_is_immutable(self):
        """CashDeploymentResult is frozen — mutation must raise."""
        result = evaluate(
            scenario_key=BULL_SCENARIO,
            cash_ratio=0.30,
            adopted_candidates=0,
        )
        with pytest.raises((AttributeError, TypeError)):
            result.triggered = False  # type: ignore[misc]

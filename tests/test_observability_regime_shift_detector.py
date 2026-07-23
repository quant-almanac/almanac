"""Tests for almanac.observability.regime_shift_detector.

Coverage:
- classify_severity: same-family minor, cross-family major, unknown defaults to major
- detect_shift: identical regimes → None, change → RegimeShift with correct cooldown
- compute_active_multiplier: empty → 1.0, in-cooldown → 0.5, all expired → 1.0
- run end-to-end:
  - First run with no ledger: detects null→current as a shift, appends 1 row
  - Same-day re-run: idempotent (ledger unchanged)
  - Regime changes day 2: appends 2nd row
  - Cooldown expiry after cooldown_days returns multiplier back to 1.0
  - Report file atomic write (no .tmp residue)
- Empty scenario_state → graceful handling (current_regime=None, no shift)
- Unknown regime family → severity "major" (conservative)
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.regime_shift_detector import (  # noqa: E402
    RegimeShift,
    RegimeShiftReport,
    classify_severity,
    compute_active_multiplier,
    detect_shift,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scenario_state(tmp_path: Path, scenarios: dict) -> Path:
    """Write a scenario_state.json fixture and return its path."""
    p = tmp_path / "scenario_state.json"
    p.write_text(
        json.dumps({"scenarios": scenarios, "active_count": 0}),
        encoding="utf-8",
    )
    return p


def _make_shift(
    from_regime: str | None,
    to_regime: str,
    *,
    cooldown_until: str,
    detected_at: str = "2026-05-24T00:00:00+00:00",
    severity: str = "major",
) -> RegimeShift:
    return RegimeShift(
        detected_at=detected_at,
        from_regime=from_regime,
        to_regime=to_regime,
        severity=severity,
        cooldown_until=cooldown_until,
    )


def _read_ledger_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# classify_severity
# ---------------------------------------------------------------------------


class TestClassifySeverity:
    def test_same_family_bull_is_minor(self):
        assert classify_severity("bull_pullback", "tech_boom") == "minor"

    def test_same_family_defensive_is_minor(self):
        assert classify_severity("defensive", "risk_off") == "minor"

    def test_same_family_defensive_both_same_is_minor(self):
        assert classify_severity("risk_off", "geopolitical_crisis") == "minor"

    def test_cross_family_bull_to_defensive_is_major(self):
        assert classify_severity("bull_pullback", "defensive") == "major"

    def test_cross_family_defensive_to_bull_is_major(self):
        assert classify_severity("risk_off", "tech_boom") == "major"

    def test_cross_family_neutral_to_bull_is_major(self):
        assert classify_severity("war_end", "bull_pullback") == "major"

    def test_from_none_is_major(self):
        # No prior history → unknown family → major (conservative)
        assert classify_severity(None, "defensive") == "major"

    def test_unknown_to_regime_is_major(self):
        assert classify_severity("bull_pullback", "some_new_regime") == "major"

    def test_unknown_from_regime_is_major(self):
        assert classify_severity("brand_new_unknown", "tech_boom") == "major"

    def test_both_unknown_is_major(self):
        assert classify_severity("x_regime", "y_regime") == "major"

    def test_same_unknown_regime_is_major(self):
        # Even if both sides are the same unknown string, neither is in the
        # family map → both get "unknown" → major (conservative).
        assert classify_severity("some_future_regime", "some_future_regime") == "major"

    def test_neutral_family_both_war_end_is_minor(self):
        # war_end is in "neutral" family — but only one entry; same family = minor
        assert classify_severity("war_end", "war_end") == "minor"


# ---------------------------------------------------------------------------
# detect_shift
# ---------------------------------------------------------------------------


class TestDetectShift:
    _TODAY = date(2026, 5, 24)

    def test_identical_regimes_returns_none(self):
        result = detect_shift("bull_pullback", "bull_pullback", today=self._TODAY)
        assert result is None

    def test_change_returns_regime_shift(self):
        result = detect_shift("bull_pullback", "defensive", today=self._TODAY)
        assert result is not None
        assert isinstance(result, RegimeShift)

    def test_shift_from_none_to_regime(self):
        result = detect_shift(None, "defensive", today=self._TODAY)
        assert result is not None
        assert result.from_regime is None
        assert result.to_regime == "defensive"

    def test_cooldown_until_is_correct_default(self):
        # Default cooldown_days=30: cooldown_until = today + 29 days (inclusive)
        result = detect_shift(None, "defensive", today=self._TODAY)
        assert result is not None
        expected = (self._TODAY + timedelta(days=29)).isoformat()
        assert result.cooldown_until == expected

    def test_cooldown_until_custom_days(self):
        result = detect_shift(None, "defensive", today=self._TODAY, cooldown_days=10)
        assert result is not None
        expected = (self._TODAY + timedelta(days=9)).isoformat()
        assert result.cooldown_until == expected

    def test_severity_major_for_cross_family(self):
        result = detect_shift("bull_pullback", "defensive", today=self._TODAY)
        assert result is not None
        assert result.severity == "major"

    def test_severity_minor_for_same_family(self):
        result = detect_shift("defensive", "risk_off", today=self._TODAY)
        assert result is not None
        assert result.severity == "minor"

    def test_detected_at_is_iso_string(self):
        result = detect_shift("bull_pullback", "defensive", today=self._TODAY)
        assert result is not None
        # Should parse as valid ISO datetime
        from datetime import datetime
        dt = datetime.fromisoformat(result.detected_at)
        assert dt is not None

    def test_frozen_dataclass(self):
        result = detect_shift(None, "defensive", today=self._TODAY)
        assert result is not None
        with pytest.raises((AttributeError, TypeError)):
            result.to_regime = "bull_pullback"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# compute_active_multiplier
# ---------------------------------------------------------------------------


class TestComputeActiveMultiplier:
    _TODAY = date(2026, 5, 24)

    def test_empty_shifts_returns_one(self):
        assert compute_active_multiplier([], self._TODAY) == 1.0

    def test_in_cooldown_returns_half(self):
        shift = _make_shift(
            "bull_pullback",
            "defensive",
            cooldown_until=self._TODAY.isoformat(),  # inclusive today
        )
        assert compute_active_multiplier([shift], self._TODAY) == 0.5

    def test_cooldown_until_future_returns_half(self):
        future = (self._TODAY + timedelta(days=10)).isoformat()
        shift = _make_shift("bull_pullback", "defensive", cooldown_until=future)
        assert compute_active_multiplier([shift], self._TODAY) == 0.5

    def test_all_expired_returns_one(self):
        past = (self._TODAY - timedelta(days=1)).isoformat()
        shift = _make_shift("bull_pullback", "defensive", cooldown_until=past)
        assert compute_active_multiplier([shift], self._TODAY) == 1.0

    def test_mixed_one_active_one_expired_returns_half(self):
        past = (self._TODAY - timedelta(days=1)).isoformat()
        future = (self._TODAY + timedelta(days=5)).isoformat()
        expired = _make_shift("bull_pullback", "defensive", cooldown_until=past)
        active = _make_shift("defensive", "tech_boom", cooldown_until=future)
        assert compute_active_multiplier([expired, active], self._TODAY) == 0.5

    def test_minor_shift_still_triggers_half(self):
        # MVP: flat 0.5 regardless of severity
        future = (self._TODAY + timedelta(days=5)).isoformat()
        shift = _make_shift(
            "defensive",
            "risk_off",
            cooldown_until=future,
            severity="minor",
        )
        assert compute_active_multiplier([shift], self._TODAY) == 0.5


# ---------------------------------------------------------------------------
# run() end-to-end
# ---------------------------------------------------------------------------


class TestRunEndToEnd:
    _TODAY = date(2026, 5, 24)

    def _paths(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        state = tmp_path / "scenario_state.json"
        ledger = tmp_path / "regime_shift_ledger.jsonl"
        report = tmp_path / "regime_shift_report.json"
        return state, ledger, report

    def _write_state(self, path: Path, regime: str, readiness: float = 0.8) -> None:
        data = {
            "scenarios": {
                regime: {"readiness": readiness, "status": "active"},
                "other_low": {"readiness": 0.1, "status": "watching"},
            }
        }
        path.write_text(json.dumps(data), encoding="utf-8")

    # --- First run ---

    def test_first_run_no_ledger_detects_shift(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert result.current_regime == "defensive"
        assert len(result.historical_shifts) == 1
        shift = result.historical_shifts[0]
        assert shift.from_regime is None
        assert shift.to_regime == "defensive"

    def test_first_run_appends_one_ledger_row(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        rows = _read_ledger_rows(ledger)
        assert len(rows) == 1
        row = rows[0]
        assert row["from_regime"] is None
        assert row["to_regime"] == "defensive"
        assert "row_id" in row
        assert "cooldown_until" in row

    def test_first_run_weight_multiplier_half(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert result.weight_multiplier == 0.5

    # --- Same-day re-run idempotency ---

    def test_same_day_rerun_does_not_append_duplicate(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        # Same state, same day, same regime → no new row
        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        rows = _read_ledger_rows(ledger)
        assert len(rows) == 1

    def test_same_day_rerun_returns_consistent_report(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        r1 = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        r2 = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert r1.current_regime == r2.current_regime
        assert r1.weight_multiplier == r2.weight_multiplier
        assert len(r1.historical_shifts) == len(r2.historical_shifts)

    # --- Regime change on day 2 ---

    def test_regime_change_day2_appends_second_row(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        day1 = self._TODAY
        day2 = self._TODAY + timedelta(days=1)

        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=day1,
        )
        # Change to bull_pullback on day 2
        self._write_state(state, "bull_pullback")
        result2 = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=day2,
        )
        rows = _read_ledger_rows(ledger)
        assert len(rows) == 2
        assert rows[0]["to_regime"] == "defensive"
        assert rows[1]["from_regime"] == "defensive"
        assert rows[1]["to_regime"] == "bull_pullback"
        assert result2.current_regime == "bull_pullback"

    def test_regime_change_day2_multiplier_still_half(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        day1 = self._TODAY
        day2 = self._TODAY + timedelta(days=1)

        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=day1,
        )
        self._write_state(state, "bull_pullback")
        result2 = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=day2,
        )
        # Both shifts are in cooldown → still 0.5
        assert result2.weight_multiplier == 0.5

    # --- Cooldown expiry ---

    def test_cooldown_expiry_returns_multiplier_one(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        cooldown_days = 30

        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
            cooldown_days=cooldown_days,
        )
        # Run on day after cooldown expires
        after_cooldown = self._TODAY + timedelta(days=cooldown_days)
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=after_cooldown,
            cooldown_days=cooldown_days,
        )
        assert result.weight_multiplier == 1.0
        assert result.active_shifts == []

    def test_cooldown_last_day_is_still_half(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        cooldown_days = 30

        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
            cooldown_days=cooldown_days,
        )
        # Last day of cooldown: today + 29 days (inclusive)
        last_day = self._TODAY + timedelta(days=cooldown_days - 1)
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=last_day,
            cooldown_days=cooldown_days,
        )
        assert result.weight_multiplier == 0.5

    # --- Atomic write (no .tmp residue) ---

    def test_report_no_tmp_residue_after_success(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert report.exists()
        tmp_path_check = report.with_suffix(report.suffix + ".tmp")
        assert not tmp_path_check.exists()

    def test_report_is_valid_json(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        assert "as_of" in data
        assert "current_regime" in data
        assert "weight_multiplier" in data
        assert "active_shifts" in data
        assert "historical_shifts" in data

    def test_report_roundtrip_values(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["current_regime"] == result.current_regime
        assert data["weight_multiplier"] == result.weight_multiplier
        assert len(data["historical_shifts"]) == len(result.historical_shifts)

    # --- Empty scenario_state ---

    def test_empty_scenario_state_no_shift(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        state.write_text(json.dumps({"scenarios": {}}), encoding="utf-8")
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert result.current_regime is None
        assert result.historical_shifts == []
        assert result.weight_multiplier == 1.0

    def test_missing_scenario_state_graceful(self, tmp_path: Path):
        state = tmp_path / "nonexistent_scenario_state.json"
        ledger = tmp_path / "ledger.jsonl"
        report = tmp_path / "report.json"
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert result.current_regime is None
        assert result.historical_shifts == []
        assert result.weight_multiplier == 1.0

    def test_scenario_state_no_scenarios_key(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        state.write_text(json.dumps({"evaluated_at": "2026-05-24"}), encoding="utf-8")
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert result.current_regime is None

    # --- Unknown regime family ---

    def test_unknown_regime_family_is_major(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "some_future_unknown_regime")
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert len(result.historical_shifts) == 1
        assert result.historical_shifts[0].severity == "major"

    # --- Highest-readiness regime selection ---

    def test_highest_readiness_is_selected(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        state.write_text(
            json.dumps({
                "scenarios": {
                    "defensive": {"readiness": 0.3, "status": "watching"},
                    "tech_boom": {"readiness": 0.9, "status": "active"},
                    "war_end": {"readiness": 0.1, "status": "dormant"},
                }
            }),
            encoding="utf-8",
        )
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert result.current_regime == "tech_boom"

    def test_all_zero_readiness_picks_first_encountered(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        # All scenarios at 0.0 readiness — should still pick one (or None if
        # best_readiness stays -1.0 which no 0.0 beats)
        state.write_text(
            json.dumps({
                "scenarios": {
                    "defensive": {"readiness": 0.0, "status": "dormant"},
                    "tech_boom": {"readiness": 0.0, "status": "dormant"},
                }
            }),
            encoding="utf-8",
        )
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        # 0.0 > -1.0 so the first one wins; regardless, a regime is returned
        assert result.current_regime is not None

    # --- Active / historical split ---

    def test_active_shifts_subset_of_historical(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        for s in result.active_shifts:
            assert s in result.historical_shifts

    def test_historical_shifts_preserved_across_runs(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        day1 = self._TODAY
        day2 = self._TODAY + timedelta(days=1)

        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=day1,
        )
        self._write_state(state, "tech_boom")
        result2 = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=day2,
        )
        assert len(result2.historical_shifts) == 2

    # --- RegimeShiftReport frozen ---

    def test_report_is_frozen_dataclass(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        result = run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        with pytest.raises((AttributeError, TypeError)):
            result.current_regime = "bull_pullback"  # type: ignore[misc]

    # --- Ledger row shape ---

    def test_ledger_row_has_required_fields(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        rows = _read_ledger_rows(ledger)
        assert len(rows) == 1
        row = rows[0]
        for field in ("row_id", "detected_at", "from_regime", "to_regime",
                      "severity", "cooldown_until"):
            assert field in row, f"Missing field: {field}"

    # --- No mutation of agent_reliability.json ---

    def test_does_not_create_agent_reliability(self, tmp_path: Path):
        state, ledger, report = self._paths(tmp_path)
        self._write_state(state, "defensive")
        run(
            scenario_state_path=state,
            shift_ledger_path=ledger,
            report_path=report,
            today=self._TODAY,
        )
        assert not (tmp_path / "agent_reliability.json").exists()

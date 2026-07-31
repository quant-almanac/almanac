import ast
from pathlib import Path

import pytest

import analyst
from freshness_policy import (
    SOURCE_FRESHNESS_POLICIES,
    refresh_after_hours,
    validate_all_freshness_policies,
)


def test_every_refresh_threshold_precedes_its_stale_policy():
    """A producer may never refresh at the same boundary that rejects input."""
    validate_all_freshness_policies()
    refreshable = {
        source: policy
        for source, policy in SOURCE_FRESHNESS_POLICIES.items()
        if policy.refresh_after_hours is not None
    }
    assert refreshable
    for source, policy in refreshable.items():
        assert policy.refresh_after_hours < policy.stale_after_hours, source


@pytest.mark.parametrize("source", ["technical", "macro", "news", "options"])
def test_override_cannot_recreate_the_refresh_stale_boundary(source):
    policy = SOURCE_FRESHNESS_POLICIES[source]
    with pytest.raises(ValueError, match="must be less than stale_after_hours"):
        refresh_after_hours(source, policy.stale_after_hours)


def test_analysis_fails_closed_on_invalid_refresh_override(monkeypatch):
    monkeypatch.setenv("ALMANAC_MACRO_EVENT_REFRESH_HOURS", "48")
    with pytest.raises(ValueError, match="must be less than stale_after_hours"):
        analyst._validate_decision_refresh_configuration()


def test_analysis_fails_closed_on_non_numeric_refresh_override(monkeypatch):
    monkeypatch.setenv("ALMANAC_TECHNICAL_REFRESH_HOURS", "tomorrow")
    with pytest.raises(ValueError, match="invalid refresh override"):
        analyst._validate_decision_refresh_configuration()


def test_analysis_snapshot_age_policies_come_from_registry():
    path = Path(__file__).parents[1] / "analysis_snapshot.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None)
        if name not in {"_provenance_for_file", "_freshness_status"}:
            continue
        keyword = next(
            (kw.value for kw in node.keywords if kw.arg == "max_age_hours"),
            None,
        )
        if keyword is None:
            continue
        checked += 1
        assert not isinstance(keyword, ast.Constant)
        if isinstance(keyword, ast.Call):
            assert getattr(keyword.func, "id", None) == "stale_after_hours"
    assert checked >= 7

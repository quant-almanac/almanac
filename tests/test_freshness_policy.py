import pytest

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

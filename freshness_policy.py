"""Decision-input freshness contracts shared by refreshers and snapshots.

``refresh_after_hours`` is when the producer must refresh an input.
``stale_after_hours`` is when the frozen decision snapshot must reject it.
Every refreshable source must keep a positive safety margin:

    refresh_after_hours < stale_after_hours

Keeping both values in one registry prevents a daily job from skipping a
refresh at exactly the same boundary at which the downstream snapshot becomes
stale.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FreshnessPolicy:
    refresh_after_hours: Optional[float]
    stale_after_hours: float

    def validate(self, source: str) -> None:
        if self.stale_after_hours <= 0:
            raise ValueError(f"{source}: stale_after_hours must be positive")
        if self.refresh_after_hours is None:
            return
        if self.refresh_after_hours < 0:
            raise ValueError(f"{source}: refresh_after_hours must be non-negative")
        if self.refresh_after_hours >= self.stale_after_hours:
            raise ValueError(
                f"{source}: refresh_after_hours ({self.refresh_after_hours}) "
                f"must be less than stale_after_hours ({self.stale_after_hours})"
            )


SOURCE_FRESHNESS_POLICIES: dict[str, FreshnessPolicy] = {
    "holdings": FreshnessPolicy(refresh_after_hours=None, stale_after_hours=96.0),
    "cash": FreshnessPolicy(refresh_after_hours=None, stale_after_hours=96.0),
    "technical": FreshnessPolicy(refresh_after_hours=4.0, stale_after_hours=8.0),
    "fx": FreshnessPolicy(refresh_after_hours=None, stale_after_hours=24.0),
    "macro": FreshnessPolicy(refresh_after_hours=12.0, stale_after_hours=24.0),
    "news": FreshnessPolicy(refresh_after_hours=6.0, stale_after_hours=12.0),
    "screening": FreshnessPolicy(refresh_after_hours=None, stale_after_hours=72.0),
    "options": FreshnessPolicy(refresh_after_hours=12.0, stale_after_hours=24.0),
}


def get_freshness_policy(source: str) -> FreshnessPolicy:
    try:
        policy = SOURCE_FRESHNESS_POLICIES[source]
    except KeyError as exc:
        raise KeyError(f"unregistered decision-input source: {source}") from exc
    policy.validate(source)
    return policy


def refresh_after_hours(source: str, override: float | None = None) -> float:
    """Return a validated refresh threshold for a refreshable source."""
    policy = get_freshness_policy(source)
    value = policy.refresh_after_hours if override is None else float(override)
    if value is None:
        raise ValueError(f"{source}: source has no periodic refresh contract")
    overridden = FreshnessPolicy(
        refresh_after_hours=float(value),
        stale_after_hours=policy.stale_after_hours,
    )
    overridden.validate(source)
    return float(value)


def stale_after_hours(source: str) -> float:
    return get_freshness_policy(source).stale_after_hours


def validate_all_freshness_policies() -> None:
    for source, policy in SOURCE_FRESHNESS_POLICIES.items():
        policy.validate(source)

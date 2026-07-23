"""Cash Deployment Critic — deterministic rule-based Opus warning injector.

Plan §5 step 11b / Round 6 C6-4 / Round 7 C7-2.

When the portfolio is in an **aggressive / bull-mode** scenario AND:

- ``cash_ratio > CASH_RATIO_THRESHOLD`` (default 0.20), AND
- ``adopted_candidates == 0``

…the critic fires: it returns a Japanese-language WARNING string ready to be
appended to the Opus prompt so that Opus **must** supply ``rejection_notes``
explaining why no position was taken.  The same event is written to
``cash_deployment_log.jsonl`` for retrospective evaluation.

No LLM call is made — the logic is roughly 50 lines of deterministic rules.

Design notes
------------
- ``evaluate()`` is the pure-functional entry point: given inputs, return a
  :class:`CashDeploymentResult`.  No side effects.
- ``write_to_log()`` is the side-effectful layer: appends to the log file and
  returns the ``cash_decision_id`` so the caller can correlate follow-up rows.
- ``format_opus_warning()`` is a tiny helper so tests can check exact wording
  without going through ``evaluate()``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from .logs import write_cash_critic_triggered

__all__ = [
    "CashDeploymentResult",
    "evaluate",
    "format_opus_warning",
    "write_to_log",
    "CASH_RATIO_THRESHOLD",
    "BULL_SCENARIO_KEYS",
]

logger = logging.getLogger(__name__)

#: Cash ratio above which the critic fires when in bull mode.
CASH_RATIO_THRESHOLD: float = 0.20

#: Scenario keys recognised as "aggressive / bull" mode.
BULL_SCENARIO_KEYS: frozenset[str] = frozenset(
    {
        "bull_pullback",
        "tech_boom",
        "risk_on",
        "recovery",
        "bull_confirmed",
        "regime_bull_confirmed",
    }
)

#: Default benchmark basket used when no explicit basket is provided.
_DEFAULT_BENCHMARK_BASKET: list[str] = ["VT", "AGG"]
_DEFAULT_BENCHMARK_WEIGHTS: list[float] = [0.6, 0.4]


# ---------------------------------------------------------------------------
# Public data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CashDeploymentResult:
    """Return value from :func:`evaluate`.

    All fields are immutable so the object can be safely stored and logged.
    """

    triggered: bool
    """``True`` when all three conditions (bull mode, excess cash, no buys) are met."""

    warning_text: str
    """Japanese-language warning ready to append to the Opus prompt.

    Empty string when ``triggered=False``.
    """

    scenario_key: str
    """The scenario key that was evaluated."""

    active_bull_scenarios: list[str]
    """Non-empty when the critic considers the current scenario as bull mode."""

    cash_ratio: float
    """Cash fraction at the time of evaluation."""

    cash_ratio_threshold: float
    """The threshold used for comparison (for audit purposes)."""

    adopted_candidates: int
    """Number of catalyst hypotheses adopted this run."""

    generated_candidates: int
    """Total hypotheses generated this run (logged for context)."""


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def evaluate(
    *,
    scenario_key: str,
    cash_ratio: float,
    adopted_candidates: int,
    generated_candidates: int = 0,
    cash_ratio_threshold: float = CASH_RATIO_THRESHOLD,
    bull_scenario_keys: frozenset[str] | None = None,
) -> CashDeploymentResult:
    """Evaluate the cash deployment condition (pure function — no side effects).

    Parameters
    ----------
    scenario_key:
        Current active scenario key (e.g. ``"bull_pullback"``).
    cash_ratio:
        Cash / total-assets ratio in ``[0, 1]``.
    adopted_candidates:
        Catalyst hypotheses adopted by Opus this run.
    generated_candidates:
        Total hypotheses generated (for log context only).
    cash_ratio_threshold:
        Fires when ``cash_ratio > this`` (default ``CASH_RATIO_THRESHOLD``).
    bull_scenario_keys:
        Override the recognised bull-mode keys. ``None`` → use
        :data:`BULL_SCENARIO_KEYS`.

    Returns
    -------
    CashDeploymentResult
        ``.triggered`` is ``True`` when all conditions are met.
    """
    active_bulls = _active_bull_scenarios(scenario_key, bull_scenario_keys)
    triggered = bool(
        active_bulls
        and cash_ratio > cash_ratio_threshold
        and adopted_candidates == 0
    )
    warning = format_opus_warning(scenario_key, cash_ratio) if triggered else ""
    return CashDeploymentResult(
        triggered=triggered,
        warning_text=warning,
        scenario_key=scenario_key,
        active_bull_scenarios=active_bulls,
        cash_ratio=cash_ratio,
        cash_ratio_threshold=cash_ratio_threshold,
        adopted_candidates=adopted_candidates,
        generated_candidates=generated_candidates,
    )


def format_opus_warning(scenario_key: str, cash_ratio: float) -> str:
    """Return the Japanese warning text appended to the Opus prompt when triggered.

    The text is intentionally terse (< 100 chars) to avoid bloating the prompt.
    """
    return (
        f"\n⚠️ 攻めモード ({scenario_key}) で現金比率 {cash_ratio:.0%}、"
        "買い候補 0 件。rejection_notes で理由を明示せよ。"
    )


def write_to_log(
    log_path: Path | str,
    result: CashDeploymentResult,
    *,
    analysis_id: str,
    analysis_date: str,
    opus_no_buy_reason: str = "",
    portfolio_decision_state: str = "cash_retained",
    benchmark_basket: list[str] | None = None,
    benchmark_weights: list[float] | None = None,
    fsync: bool = True,
) -> str:
    """Append a ``critic_triggered`` row to ``cash_deployment_log.jsonl``.

    Parameters
    ----------
    log_path:
        Destination JSONL file (created if absent).
    result:
        Return value from :func:`evaluate`.
    analysis_id:
        UUID for the current ``analyzer.py`` run.
    analysis_date:
        ISO date string (``YYYY-MM-DD``) for this run.
    opus_no_buy_reason:
        Text extracted from Opus ``rejection_notes`` (empty before Opus replies).
    portfolio_decision_state:
        Current portfolio decision state (default ``"cash_retained"``).
    benchmark_basket:
        Override the default ``["VT", "AGG"]`` benchmark.
    benchmark_weights:
        Override the default ``[0.6, 0.4]`` weights.
    fsync:
        Pass ``False`` in tests / batch jobs to skip the fsync syscall.

    Returns
    -------
    str
        The ``cash_decision_id`` (sha-256 stable across retries for the same
        date/scenario/ratio combination).
    """
    cash_decision_id = _make_cash_decision_id(
        analysis_date=analysis_date,
        scenario_key=result.scenario_key,
        cash_ratio=result.cash_ratio,
    )
    basket = benchmark_basket if benchmark_basket is not None else _DEFAULT_BENCHMARK_BASKET
    weights = benchmark_weights if benchmark_weights is not None else _DEFAULT_BENCHMARK_WEIGHTS

    write_cash_critic_triggered(
        log_path,
        cash_decision_id=cash_decision_id,
        analysis_date=analysis_date,
        analysis_id=analysis_id,
        risk_mode="aggressive" if result.active_bull_scenarios else "neutral",
        cash_ratio=result.cash_ratio,
        cash_ratio_threshold=result.cash_ratio_threshold,
        active_bull_scenarios=result.active_bull_scenarios,
        generated_candidates=result.generated_candidates,
        adopted_candidates=result.adopted_candidates,
        warning_text=result.warning_text,
        opus_no_buy_reason=opus_no_buy_reason,
        portfolio_decision_state=portfolio_decision_state,
        benchmark_basket=basket,
        benchmark_weights=weights,
        fsync=fsync,
    )
    return cash_decision_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _active_bull_scenarios(
    scenario_key: str,
    custom_keys: frozenset[str] | None,
) -> list[str]:
    """Return a single-element list if scenario_key is a known bull scenario."""
    keys = custom_keys if custom_keys is not None else BULL_SCENARIO_KEYS
    if scenario_key in keys:
        return [scenario_key]
    return []


def _make_cash_decision_id(
    *,
    analysis_date: str,
    scenario_key: str,
    cash_ratio: float,
) -> str:
    """Deterministic SHA-256 prefix stable across retries for the same inputs."""
    raw = f"{analysis_date}|{scenario_key}|{cash_ratio:.4f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

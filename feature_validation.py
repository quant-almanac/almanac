"""
feature_validation.py — Phase 1 validation harness for disclosure features.

Decides whether an ``observe_only`` disclosure feature actually predicts forward
excess returns BEFORE it may ever influence decisions. A feature is certified
only if it clears, jointly:

  * rank-IC: positive mean information coefficient that is statistically stable
    (|t-stat| above threshold) and does not flip sign across horizons (decay),
  * after-cost economics: a top-minus-bottom long-short book with a positive
    Sharpe NET of transaction cost,
  * a Deflated Sharpe Ratio that accounts for the TRUE number of trials
    (features x horizons x slices), so multiple testing cannot manufacture a
    "winner".

A buggy validator that greenlights noise is worse than none, so the harness is
calibrated on a NULL feature (must fail) and a synthetic SIGNAL (must pass) —
see ``tests/test_feature_validation.py``.

Pure-stats first: every metric operates on a ``panel`` — a list of observations
``{"date", "ticker", "feature", "fwd_return"}`` (extra keys allowed for slices).
So the whole core is testable offline with synthetic data. Joining the live
feature store to realized forward returns is a thin separate layer that only
runs once forward (post-model-cutoff) data has accumulated.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np
from scipy import stats as _st

from almanac.observability.append_only_log import append_jsonl_safe

__all__ = [
    "DEFAULT_THRESHOLDS",
    "rank_ic_series",
    "ic_summary",
    "long_short_pnl",
    "sharpe",
    "brier_calibration",
    "paraphrase_stability",
    "ensemble_agreement",
    "deflated_sharpe_ratio",
    "hac_effective_sample_size",
    "cluster_robust_dsr",
    "expected_max_sharpe",
    "slice_ic",
    "certify",
    "certification_kill_switch",
    "write_certification",
    "default_certification_path",
    "build_panel_from_logs",
]

_EULER = 0.5772156649015329

# Certification gate. Tunable, but the defaults encode the plan's discipline:
# a stable positive IC, after-cost positive Sharpe, and a Deflated Sharpe that
# survives the true trial count.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "min_abs_t_stat": 2.0,     # IC significance
    "min_ls_sharpe": 0.30,     # after-cost long-short Sharpe (annualized)
    "min_short_ls_sharpe": 0.50,
    "min_dsr": 0.95,           # Deflated Sharpe Ratio
    "min_days": 20,            # minimum distinct cross-sections
}


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> "datetime | None":
    """Parse an ISO timestamp to a UTC-aware datetime; None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _by_date(panel: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for obs in panel:
        groups.setdefault(obs["date"], []).append(obs)
    return groups


def _clean_pairs(obs_list: Sequence[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    """Return aligned (feature, fwd_return) arrays dropping non-finite rows."""
    feats, rets = [], []
    for o in obs_list:
        f, r = o.get("feature"), o.get("fwd_return")
        if f is None or r is None:
            continue
        try:
            f, r = float(f), float(r)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f) and math.isfinite(r):
            feats.append(f)
            rets.append(r)
    return np.asarray(feats, dtype=float), np.asarray(rets, dtype=float)


# ---------------------------------------------------------------------------
# Rank IC
# ---------------------------------------------------------------------------


def rank_ic_series(panel: Iterable[dict[str, Any]], *, min_names: int = 3) -> list[float]:
    """Per-date cross-sectional Spearman rank IC of feature vs forward return.

    A date with fewer than ``min_names`` usable names, or with no variation in
    feature or return (Spearman undefined), is skipped.
    """
    out: list[float] = []
    for _date, obs_list in sorted(_by_date(panel).items()):
        feats, rets = _clean_pairs(obs_list)
        if feats.size < min_names:
            continue
        if np.ptp(feats) == 0 or np.ptp(rets) == 0:
            continue  # constant column → correlation undefined
        rho, _p = _st.spearmanr(feats, rets)
        if rho is not None and math.isfinite(rho):
            out.append(float(rho))
    return out


def ic_summary(panel: Iterable[dict[str, Any]], *, min_names: int = 3) -> dict[str, Any]:
    """Summary stats of the rank-IC series: mean, std, t-stat, hit rate, n days.

    t-stat is ``mean / (std / sqrt(n))`` — the standard test that the average IC
    differs from zero across independent cross-sections.
    """
    ics = rank_ic_series(panel, min_names=min_names)
    n = len(ics)
    if n == 0:
        return {"mean_ic": 0.0, "ic_std": 0.0, "t_stat": 0.0, "hit_rate": 0.0, "n_days": 0}
    arr = np.asarray(ics, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if n > 1 else 0.0
    t_stat = float(mean / (std / math.sqrt(n))) if std > 0 else 0.0
    hit_rate = float((arr > 0).mean())
    return {"mean_ic": mean, "ic_std": std, "t_stat": t_stat,
            "hit_rate": hit_rate, "n_days": n}


# ---------------------------------------------------------------------------
# After-cost long-short economics
# ---------------------------------------------------------------------------


def long_short_pnl(
    panel: Iterable[dict[str, Any]],
    *,
    quantile: float = 0.3,
    cost_bps: float = 10.0,
    min_names: int = 4,
) -> list[float]:
    """Per-date after-cost return of a top-minus-bottom-quantile book.

    Long the top ``quantile`` by feature, short the bottom ``quantile``, equal
    weight, then subtract ``2 * cost_bps`` (entry on both legs) in return units.
    Returns the per-date net return series.
    """
    cost = 2.0 * cost_bps / 1e4
    out: list[float] = []
    for _date, obs_list in sorted(_by_date(panel).items()):
        feats, rets = _clean_pairs(obs_list)
        if feats.size < min_names:
            continue
        k = max(1, int(round(feats.size * quantile)))
        order = np.argsort(feats)
        bottom = rets[order[:k]]
        top = rets[order[-k:]]
        out.append(float(top.mean() - bottom.mean() - cost))
    return out


def sharpe(returns: Sequence[float], *, periods_per_year: int = 52) -> float:
    """Annualized Sharpe of a per-period return series (0.0 if undefined)."""
    arr = np.asarray([r for r in returns if r is not None and math.isfinite(r)], dtype=float)
    if arr.size < 2:
        return 0.0
    sd = arr.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(arr.mean() / sd * math.sqrt(periods_per_year))


def brier_calibration(
    panel: Iterable[dict[str, Any]],
    *,
    score_field: str = "feature",
    return_field: str = "fwd_return",
) -> dict[str, Any]:
    """Brier calibration for signed directional scores in ``[-1, 1]``.

    ``directional_score=-1`` maps to 0% positive-return probability, ``0`` to
    50%, and ``1`` to 100%. The event is ``fwd_return > 0``.
    """
    probabilities: list[float] = []
    outcomes: list[float] = []
    for obs in panel:
        try:
            score = float(obs.get(score_field))
            realized = float(obs.get(return_field))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(score) and math.isfinite(realized)):
            continue
        if not -1.0 <= score <= 1.0:
            continue
        probabilities.append((score + 1.0) / 2.0)
        outcomes.append(1.0 if realized > 0 else 0.0)

    n = len(probabilities)
    if n == 0:
        return {
            "score_field": score_field,
            "return_field": return_field,
            "n": 0,
            "brier_score": None,
            "event_rate": None,
            "mean_probability": None,
            "calibration_bias": None,
            "brier_skill_vs_base_rate": None,
            "error": "no valid directional scores",
        }

    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    brier = float(np.mean((p - y) ** 2))
    event_rate = float(y.mean())
    base_brier = float(np.mean((event_rate - y) ** 2))
    skill = (1.0 - brier / base_brier) if base_brier > 0 else None
    return {
        "score_field": score_field,
        "return_field": return_field,
        "n": n,
        "brier_score": brier,
        "event_rate": event_rate,
        "mean_probability": float(p.mean()),
        "calibration_bias": float((p - y).mean()),
        "brier_skill_vs_base_rate": skill,
        "error": None,
    }


def paraphrase_stability(
    panel: Iterable[dict[str, Any]],
    *,
    feature_field: str = "feature",
    group_field: str = "source_event_id",
) -> dict[str, Any]:
    """Detect sign instability across paraphrased re-extractions."""
    groups: dict[str, list[float]] = {}
    n_obs = 0
    for row in panel:
        raw_key = row.get(group_field) or row.get("hypothesis_id")
        if not raw_key:
            raw_key = f"{row.get('date', '')}|{row.get('ticker', '')}"
        key = str(raw_key)
        value = _safe_float(row.get(feature_field))
        if value is None or value == 0:
            continue
        n_obs += 1
        groups.setdefault(key, []).append(value)

    unstable = []
    for key, values in sorted(groups.items()):
        signs = {1 if value > 0 else -1 for value in values}
        if signs == {-1, 1}:
            unstable.append({
                group_field: key,
                "n": len(values),
                "min_feature": min(values),
                "max_feature": max(values),
            })

    group_count = len(groups)
    stable_count = group_count - len(unstable)
    return {
        "feature_field": feature_field,
        "group_field": group_field,
        "n_obs": n_obs,
        "groups": group_count,
        "stable_group_count": stable_count,
        "unstable_group_count": len(unstable),
        "stable_rate": stable_count / group_count if group_count else None,
        "unstable_groups": unstable[:20],
    }


def ensemble_agreement(
    panel: Iterable[dict[str, Any]],
    *,
    feature_field: str = "feature",
    group_field: str = "source_event_id",
) -> dict[str, Any]:
    """Summarize same-direction agreement across ensemble/self-consistency runs."""
    groups: dict[str, list[float]] = {}
    n_obs = 0
    for row in panel:
        raw_key = row.get(group_field) or row.get("hypothesis_id")
        if not raw_key:
            raw_key = f"{row.get('date', '')}|{row.get('ticker', '')}"
        key = str(raw_key)
        value = _safe_float(row.get(feature_field))
        if value is None or value == 0:
            continue
        n_obs += 1
        groups.setdefault(key, []).append(value)

    disagreed = []
    for key, values in sorted(groups.items()):
        signs = {1 if value > 0 else -1 for value in values}
        if signs == {-1, 1}:
            disagreed.append({
                group_field: key,
                "n": len(values),
                "min_feature": min(values),
                "max_feature": max(values),
            })

    group_count = len(groups)
    agreed_count = group_count - len(disagreed)
    return {
        "feature_field": feature_field,
        "group_field": group_field,
        "n_obs": n_obs,
        "groups": group_count,
        "agreed_group_count": agreed_count,
        "disagreed_group_count": len(disagreed),
        "agreement_rate": agreed_count / group_count if group_count else None,
        "disagreed_groups": disagreed[:20],
    }


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio (Bailey & López de Prado, 2014)
# ---------------------------------------------------------------------------


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    """Expected maximum Sharpe under the null across ``n_trials`` independent
    strategies whose Sharpe estimates have dispersion ``sr_std`` (per-obs units).
    """
    if n_trials <= 1 or sr_std <= 0:
        return 0.0
    z1 = float(_st.norm.ppf(1.0 - 1.0 / n_trials))
    z2 = float(_st.norm.ppf(1.0 - 1.0 / (n_trials * math.e)))
    return sr_std * ((1.0 - _EULER) * z1 + _EULER * z2)


def deflated_sharpe_ratio(
    sr_obs: float,
    *,
    n_trials: int,
    n_obs: int,
    sr_std: float | None = None,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Probability the observed (per-obs) Sharpe is real after deflating for the
    number of trials and the non-normal shape of returns. Range ``(0, 1)``.

    ``sr_obs`` / ``sr_std`` are in PER-OBSERVATION Sharpe units (not annualized).
    When ``sr_std`` is omitted it falls back to the asymptotic SE of the Sharpe
    estimator as a stand-in for cross-trial dispersion.
    """
    if n_obs < 2:
        return 0.0
    shape = 1.0 - skew * sr_obs + ((kurtosis - 1.0) / 4.0) * sr_obs ** 2
    shape = max(shape, 1e-12)
    if sr_std is None:
        sr_std = math.sqrt(shape / (n_obs - 1))
    sr0 = expected_max_sharpe(n_trials, sr_std)
    z = (sr_obs - sr0) * math.sqrt(n_obs - 1) / math.sqrt(shape)
    return float(_st.norm.cdf(z))


def hac_effective_sample_size(
    returns: Sequence[float],
    *,
    max_lag: int,
) -> float:
    """Bartlett-kernel effective N for overlapping forward-return windows."""
    arr = np.asarray([r for r in returns if r is not None and math.isfinite(r)], dtype=float)
    n = int(arr.size)
    if n < 3 or max_lag <= 0:
        return float(n)
    centered = arr - arr.mean()
    variance = float(np.dot(centered, centered) / n)
    if variance <= 0:
        return float(n)
    lag_cap = min(int(max_lag), n - 2)
    dependence = 0.0
    for lag in range(1, lag_cap + 1):
        autocov = float(np.dot(centered[lag:], centered[:-lag]) / n)
        rho = autocov / variance
        weight = 1.0 - lag / (lag_cap + 1.0)
        dependence += weight * rho
    inflation = max(1.0, 1.0 + 2.0 * dependence)
    return max(2.0, min(float(n), n / inflation))


def cluster_robust_dsr(
    returns: Sequence[float],
    *,
    n_trials: int,
    outcome_horizon_days: int,
) -> dict[str, float | int | str]:
    """DSR using HAC effective N for overlapping event-outcome windows."""
    arr = np.asarray([r for r in returns if r is not None and math.isfinite(r)], dtype=float)
    if arr.size < 2:
        return {
            "method": "hac_bartlett_effective_n",
            "max_lag": max(0, int(outcome_horizon_days) - 1),
            "raw_n": int(arr.size),
            "effective_n": float(arr.size),
            "dsr": 0.0,
        }
    sd = float(arr.std(ddof=1))
    sr_obs = float(arr.mean() / sd) if sd > 0 else 0.0
    max_lag = max(0, int(outcome_horizon_days) - 1)
    effective_n = hac_effective_sample_size(arr, max_lag=max_lag)
    dsr = deflated_sharpe_ratio(
        sr_obs,
        n_trials=n_trials,
        n_obs=max(2, int(math.floor(effective_n))),
    )
    return {
        "method": "hac_bartlett_effective_n",
        "max_lag": max_lag,
        "raw_n": int(arr.size),
        "effective_n": effective_n,
        "dsr": dsr,
    }


# ---------------------------------------------------------------------------
# Slice analysis
# ---------------------------------------------------------------------------


def slice_ic(
    panel: Iterable[dict[str, Any]],
    key: str | Callable[[dict[str, Any]], str],
    *,
    min_names: int = 3,
) -> dict[str, dict[str, Any]]:
    """IC summary per slice (e.g. by ``disclosure_type`` / ``market``).

    ``key`` is a field name or a function mapping an observation to a slice label.
    """
    key_fn = key if callable(key) else (lambda o, _k=key: o.get(_k, "unknown"))
    buckets: dict[str, list[dict[str, Any]]] = {}
    for obs in panel:
        buckets.setdefault(str(key_fn(obs)), []).append(obs)
    return {label: ic_summary(rows, min_names=min_names) for label, rows in buckets.items()}


# ---------------------------------------------------------------------------
# Certification
# ---------------------------------------------------------------------------


def default_certification_path() -> Path:
    return Path(__file__).resolve().parent / "feature_certifications.jsonl"


def certify(
    panel: Iterable[dict[str, Any]],
    *,
    feature_name: str,
    n_trials: int,
    min_compute_time: str | None = None,
    allow_mixed_versions: bool = False,
    trial_manifest: Any | None = None,
    prompt_version: str | None = None,
    schema_version: str | None = None,
    horizon_panels: dict[int, Iterable[dict[str, Any]]] | None = None,
    outcome_horizon_days: int | None = None,
    placebo_panel: Iterable[dict[str, Any]] | None = None,
    placebo_feature_name: str = "placebo_hash_score",
    paraphrase_panel: Iterable[dict[str, Any]] | None = None,
    ensemble_panel: Iterable[dict[str, Any]] | None = None,
    slice_keys: Sequence[str] = ("disclosure_type", "market", "direction"),
    quantile: float = 0.3,
    cost_bps: float = 10.0,
    short_cost_bps: float = 30.0,
    min_capacity_jpy: float = 30_000_000.0,
    max_adv_fraction: float = 0.05,
    direction: str | None = None,
    periods_per_year: int = 52,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Run the full gate and return a certification record.

    ``n_trials`` MUST be the true number of trials run this cycle
    (features x horizons x slices) so the Deflated Sharpe is honestly deflated —
    pre-register it; do not pass 1 after fishing.

    ``horizon_panels`` (optional) maps horizon_days → panel for the decay curve.
    A certified verdict requires: |IC t-stat| ≥ threshold AND mean IC > 0 AND
    after-cost long-short Sharpe ≥ threshold AND Deflated Sharpe ≥ threshold AND
    enough cross-sections — and, if multiple horizons are given, the IC sign must
    not flip across them.
    """
    raw_panel = list(panel)
    n_untradeable_excluded = sum(1 for row in raw_panel if row.get("untradeable"))
    panel = [row for row in raw_panel if not row.get("untradeable")]
    capacity_panel: list[dict[str, Any]] = []
    n_capacity_excluded = 0
    n_capacity_unknown = 0
    for row in panel:
        capacity = _row_capacity_jpy(row, max_adv_fraction=max_adv_fraction)
        if capacity is None:
            n_capacity_unknown += 1
            capacity_panel.append(row)
        elif capacity >= min_capacity_jpy:
            capacity_panel.append(row)
        else:
            n_capacity_excluded += 1
    panel = capacity_panel
    if direction is not None:
        panel = [row for row in panel if row.get("direction") == direction]
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    effective_cost_bps = short_cost_bps if direction == "short" else cost_bps

    ic = ic_summary(panel)
    ls = long_short_pnl(panel, quantile=quantile, cost_bps=effective_cost_bps)
    ls_sharpe_ann = sharpe(ls, periods_per_year=periods_per_year)
    cluster_stats = (
        cluster_robust_dsr(
            ls,
            n_trials=n_trials,
            outcome_horizon_days=outcome_horizon_days,
        )
        if outcome_horizon_days is not None and outcome_horizon_days > 0
        else {
            "method": None,
            "max_lag": None,
            "raw_n": len(ls),
            "effective_n": 0.0,
            "dsr": 0.0,
        }
    )
    dsr = float(cluster_stats["dsr"])

    decay: dict[str, Any] = {}
    if horizon_panels:
        for h, p in sorted(horizon_panels.items()):
            decay[str(h)] = ic_summary(list(p))["mean_ic"]
    slices = {k: slice_ic(panel, k) for k in slice_keys}
    calibration = (
        brier_calibration(panel, score_field="feature")
        if feature_name == "directional_score"
        else None
    )
    paraphrase_metrics = (
        paraphrase_stability(paraphrase_panel)
        if paraphrase_panel is not None
        else None
    )
    ensemble_metrics = (
        ensemble_agreement(ensemble_panel)
        if ensemble_panel is not None
        else None
    )

    # No-lookahead enforcement (R-round P1): certification REQUIRES a cutoff and
    # ALL obs computed at/after it (forward-collected, post-model-cutoff).
    # Without this, a pre-cutoff replay — contaminated by the model's memorized
    # future — could certify. Missing/earlier compute_time blocks certification.
    cutoff_dt = _parse_dt(min_compute_time)
    compute_dts = [_parse_dt(o.get("compute_time")) for o in panel]
    n_missing_ct = sum(1 for c in compute_dts if c is None)
    n_pre_cutoff = (
        sum(1 for c in compute_dts if c is not None and c < cutoff_dt)
        if cutoff_dt is not None else 0
    )
    valid_cts = [c for c in compute_dts if c is not None]
    ct_range = ([min(valid_cts).isoformat(), max(valid_cts).isoformat()]
                if valid_cts else [None, None])

    reasons: list[str] = []
    if paraphrase_metrics and paraphrase_metrics["unstable_group_count"] > 0:
        reasons.append(
            f"paraphrase sign instability in {paraphrase_metrics['unstable_group_count']} groups"
        )
    if outcome_horizon_days is None or outcome_horizon_days <= 0:
        reasons.append("cluster-robust DSR requires positive outcome_horizon_days")
    if cutoff_dt is None:
        reasons.append("no valid min_compute_time — cannot attest forward-collected (post-cutoff) data")
    else:
        if n_missing_ct:
            reasons.append(f"{n_missing_ct} obs missing compute_time (cannot verify post-cutoff)")
        if n_pre_cutoff:
            reasons.append(f"{n_pre_cutoff} obs predate min_compute_time {min_compute_time}")

    # Multiple-testing integrity: n_trials must reflect the true trial count, and
    # the panel must come from ONE extractor version (else 'what did we certify?'
    # is ambiguous). R2 P2.
    if n_trials < 1:
        reasons.append(f"n_trials must be >= 1 for honest DSR deflation (got {n_trials})")
    versions = {
        (o.get("model_id"), o.get("prompt_version"), o.get("feature_schema_version"))
        for o in panel
    }
    if len(versions) > 1 and not allow_mixed_versions:
        reasons.append(
            f"panel mixes {len(versions)} extractor versions "
            f"(pass allow_mixed_versions=True only for exploration)"
        )

    if ic["n_days"] < th["min_days"]:
        reasons.append(f"insufficient cross-sections ({ic['n_days']} < {int(th['min_days'])})")
    if ic["mean_ic"] <= 0:
        reasons.append("mean IC not positive")
    if abs(ic["t_stat"]) < th["min_abs_t_stat"]:
        reasons.append(f"IC t-stat {ic['t_stat']:.2f} below {th['min_abs_t_stat']}")
    min_sharpe = th["min_short_ls_sharpe"] if direction == "short" else th["min_ls_sharpe"]
    if ls_sharpe_ann < min_sharpe:
        reasons.append(f"after-cost LS Sharpe {ls_sharpe_ann:.2f} below {min_sharpe}")
    if dsr < th["min_dsr"]:
        reasons.append(f"Deflated Sharpe {dsr:.3f} below {th['min_dsr']}")
    if decay:
        signs = {1 if v > 0 else (-1 if v < 0 else 0) for v in decay.values()}
        if signs - {0} == {1, -1}:
            reasons.append("IC sign flips across horizons")

    placebo_metrics: dict[str, Any] | None = None
    if placebo_panel is None:
        reasons.append("permanent placebo panel is required before certification")
    else:
        placebo_rows = list(placebo_panel)
        placebo_ic = ic_summary(placebo_rows)
        placebo_ls = long_short_pnl(
            placebo_rows,
            quantile=quantile,
            cost_bps=effective_cost_bps,
        )
        placebo_sharpe = sharpe(placebo_ls, periods_per_year=periods_per_year)
        placebo_cluster = (
            cluster_robust_dsr(
                placebo_ls,
                n_trials=n_trials,
                outcome_horizon_days=outcome_horizon_days,
            )
            if outcome_horizon_days is not None and outcome_horizon_days > 0
            else {"dsr": 0.0, "effective_n": 0.0}
        )
        placebo_passed = bool(
            placebo_ic["n_days"] >= th["min_days"]
            and placebo_ic["mean_ic"] > 0
            and abs(placebo_ic["t_stat"]) >= th["min_abs_t_stat"]
            and placebo_sharpe >= th["min_ls_sharpe"]
            and float(placebo_cluster["dsr"]) >= th["min_dsr"]
        )
        placebo_metrics = {
            "feature_name": placebo_feature_name,
            "n_obs": len(placebo_rows),
            "n_days": placebo_ic["n_days"],
            "ic_mean": placebo_ic["mean_ic"],
            "ic_t_stat": placebo_ic["t_stat"],
            "ls_sharpe_annualized": placebo_sharpe,
            "dsr": float(placebo_cluster["dsr"]),
            "effective_n": float(placebo_cluster["effective_n"]),
            "passed_gate": placebo_passed,
        }
        if placebo_passed:
            reasons.append(
                f"placebo feature {placebo_feature_name} passed certification gate; "
                "validation harness is not trustworthy"
            )

    verdict = "certified" if not reasons else "observe_only"
    return {
        "feature_name": feature_name,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "n_obs": len(panel),
        "direction": direction,
        "effective_cost_bps": effective_cost_bps,
        "n_untradeable_excluded": n_untradeable_excluded,
        "n_capacity_excluded": n_capacity_excluded,
        "n_capacity_unknown": n_capacity_unknown,
        "capacity_turnover_gate": {
            "min_capacity_jpy": min_capacity_jpy,
            "max_adv_fraction": max_adv_fraction,
        },
        "n_days": ic["n_days"],
        "n_trials": n_trials,
        "min_compute_time": min_compute_time,
        "compute_time_range": ct_range,
        "n_pre_cutoff": n_pre_cutoff,
        "n_missing_compute_time": n_missing_ct,
        "extractor_versions": sorted("|".join(str(x) for x in v) for v in versions),
        "trial_manifest": trial_manifest,
        "ic_mean": ic["mean_ic"],
        "ic_t_stat": ic["t_stat"],
        "ic_hit_rate": ic["hit_rate"],
        "ls_sharpe_annualized": ls_sharpe_ann,
        "dsr": dsr,
        "cluster_robust": cluster_stats,
        "placebo": placebo_metrics,
        "calibration": calibration,
        "paraphrase_stability": paraphrase_metrics,
        "ensemble_agreement": ensemble_metrics,
        "decay": decay,
        "slices": slices,
        "verdict": verdict,
        "reasons": reasons,
        "valid_until": None,
    }


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _row_capacity_jpy(row: dict[str, Any], *, max_adv_fraction: float) -> float | None:
    explicit = _safe_float(row.get("capacity_jpy"))
    if explicit is not None:
        return max(0.0, explicit)
    for key in ("avg_turnover_jpy", "adv_jpy", "average_daily_turnover_jpy"):
        turnover = _safe_float(row.get(key))
        if turnover is not None:
            return max(0.0, turnover * max_adv_fraction)
    return None


def certification_kill_switch(
    certification: dict[str, Any],
    monitoring: dict[str, Any],
    *,
    as_of: str | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return a certification status record after drift/decay checks.

    The function is intentionally pure. Callers can append the returned record via
    :func:`write_certification` when ``kill_switch.triggered`` is true.
    """
    th = {
        "max_population_stability_index": 0.25,
        "min_rolling_ic_mean": 0.0,
        **(thresholds or {}),
    }
    previous = str(certification.get("verdict") or "unknown")
    reasons: list[str] = []

    if previous == "certified":
        psi = _safe_float(
            monitoring.get("population_stability_index", monitoring.get("psi"))
        )
        if psi is not None and psi > th["max_population_stability_index"]:
            reasons.append(
                f"PSI drift {psi:.3f} exceeds {th['max_population_stability_index']:.3f}"
            )

        rolling_ic = monitoring.get("rolling_ic")
        rolling_ic_mean = None
        if isinstance(rolling_ic, dict):
            rolling_ic_mean = _safe_float(rolling_ic.get("mean_ic"))
        if rolling_ic_mean is None:
            rolling_ic_mean = _safe_float(monitoring.get("rolling_ic_mean"))
        if rolling_ic_mean is not None and rolling_ic_mean <= th["min_rolling_ic_mean"]:
            reasons.append(
                f"rolling IC mean {rolling_ic_mean:.4f} <= {th['min_rolling_ic_mean']:.4f}"
            )

        placebo = monitoring.get("placebo")
        placebo_passed = bool(monitoring.get("placebo_passed_gate"))
        if isinstance(placebo, dict):
            placebo_passed = placebo_passed or bool(placebo.get("passed_gate"))
        if placebo_passed:
            reasons.append("placebo feature passed certification gate")

    triggered = bool(reasons)
    return {
        "feature_name": certification.get("feature_name"),
        "as_of": as_of or datetime.now(timezone.utc).isoformat(),
        "verdict": "observe_only" if triggered else previous,
        "previous_verdict": previous,
        "kill_switch": {
            "triggered": triggered,
            "thresholds": th,
            "monitoring": monitoring,
        },
        "reasons": reasons,
        "source_certification_as_of": certification.get("as_of"),
        "source_valid_until": certification.get("valid_until"),
    }


def write_certification(record: dict[str, Any], *, path: Path | str | None = None,
                        fsync: bool = True) -> None:
    """Append a certification record to ``feature_certifications.jsonl``."""
    p = Path(path) if path is not None else default_certification_path()
    append_jsonl_safe(p, record, fsync=fsync)


# ---------------------------------------------------------------------------
# Panel assembly — join the feature store to realized forward outcomes
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue   # one corrupt line must not break assembly
    return rows


def build_panel_from_logs(
    *,
    feature_name: str,
    horizon_days: int,
    return_field: str = "excess_return_bps",
    model_id: str | None = None,
    prompt_version: str | None = None,
    feature_schema_version: str | None = None,
    features_path: Path | str | None = None,
    outcome_log_path: Path | str | None = None,
    date_field: str = "compute_time",
) -> list[dict[str, Any]]:
    """Assemble a validation panel by joining stored features to realized outcomes.

    For each disclosure feature row, the same :func:`disclosure_hypothesis_id`
    used at generation recovers the ``hypothesis_id``; the realized forward
    return is looked up in ``catalyst_outcome_log.jsonl`` by
    ``(hypothesis_id, horizon_days)`` — where ``horizon_days`` is the *measurement*
    horizon (the hypothesis_id itself uses the fixed generation horizon).

    Returns ``{date, ticker, feature, fwd_return, disclosure_type, market}`` obs
    ready for :func:`certify`. Rows with no actionable direction, no matching
    outcome, or a missing feature/return are dropped. ``date`` is the event date
    (the no-look-ahead origin), so cross-sections group same-day disclosures.
    """
    from almanac.observability.catalyst_layer import (
        disclosure_directional_value,
        disclosure_hypothesis_id,
    )
    from almanac.observability.disclosure_features import read_features

    feats = read_features(features_path)

    # Extractor-version filter + per-event dedup (R-round P1): multiple
    # prompt/model/schema versions of the same disclosure must not each become an
    # obs (it inflates n_obs and weakens the DSR deflation). Pin a version when
    # given, then keep ONE row per source_event_id (latest compute_time).
    def _version_ok(r: dict) -> bool:
        return (
            (model_id is None or r.get("model_id") == model_id)
            and (prompt_version is None or r.get("prompt_version") == prompt_version)
            and (feature_schema_version is None
                 or r.get("feature_schema_version") == feature_schema_version)
        )

    by_event: dict[str, dict[str, Any]] = {}
    for r in feats:
        if not _version_ok(r):
            continue
        sid = r.get("source_event_id")
        if not sid:
            continue
        prev = by_event.get(sid)
        if prev is None or str(r.get("compute_time") or "") > str(prev.get("compute_time") or ""):
            by_event[sid] = r
    feats = list(by_event.values())

    out_path = (Path(outcome_log_path) if outcome_log_path is not None
                else Path(__file__).resolve().parent / "catalyst_outcome_log.jsonl")
    outcomes = _read_jsonl(out_path)

    idx: dict[tuple[str, int], dict[str, Any]] = {}
    for o in outcomes:
        hid, hz = o.get("hypothesis_id"), o.get("horizon_days")
        if hid is None or hz is None:
            continue
        try:
            idx[(hid, int(hz))] = o
        except (TypeError, ValueError):
            continue

    panel: list[dict[str, Any]] = []
    for row in feats:
        fv = row.get(feature_name)
        if fv is None:
            continue
        hid = disclosure_hypothesis_id(
            row.get("ticker"),
            disclosure_directional_value(row),
            row.get("source_event_id"),
            model_id=row.get("model_id"),
            prompt_version=row.get("prompt_version"),
            feature_schema_version=row.get("feature_schema_version"),
            action_type="short_sell" if row.get("dilution_flag") is True else None,
        )
        if not hid:
            continue
        o = idx.get((hid, int(horizon_days)))
        if not o:
            continue
        ret = o.get(return_field)
        if ret is None:
            continue
        try:
            ret_val = float(ret)
        except (TypeError, ValueError):
            continue  # non-numeric outcome (corrupt/migrated row) → skip, don't crash
        # Normalize bps → decimal return so it matches long_short_pnl's cost units
        # (cost is decimal). Mixing bps magnitudes with a decimal cost made the
        # after-cost gate a no-op (R-round P1).
        if return_field.endswith("_bps"):
            ret_val /= 1e4
        date = str(row.get(date_field) or row.get("compute_time") or row.get("publish_time") or "")[:10]
        directional_value = disclosure_directional_value(row)
        panel.append({
            "date": date,
            "ticker": row.get("ticker"),
            "feature": fv,
            "fwd_return": ret_val,
            "compute_time": row.get("compute_time"),
            "model_id": row.get("model_id"),
            "prompt_version": row.get("prompt_version"),
            "feature_schema_version": row.get("feature_schema_version"),
            "disclosure_type": row.get("disclosure_type"),
            "market": row.get("market"),
            "direction": "short" if directional_value is not None and directional_value < 0 else "long",
            "untradeable": bool(row.get("untradeable")),
            "capacity_jpy": row.get("capacity_jpy"),
            "avg_turnover_jpy": row.get("avg_turnover_jpy"),
            "adv_jpy": row.get("adv_jpy"),
        })
    return panel

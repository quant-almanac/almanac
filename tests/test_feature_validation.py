"""Tests for feature_validation.py — the Phase 1 harness.

The harness must be trustworthy before it gates anything, so the headline tests
are calibration: a NULL feature must fail certification and a SIGNAL feature must
pass, and inflating the trial count must deflate a borderline signal away.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from feature_validation import (  # noqa: E402
    brier_calibration,
    cluster_robust_dsr,
    certify,
    certification_kill_switch,
    deflated_sharpe_ratio,
    ensemble_agreement,
    expected_max_sharpe,
    ic_summary,
    long_short_pnl,
    paraphrase_stability,
    rank_ic_series,
    sharpe,
    slice_ic,
    write_certification,
)


_CUTOFF = "2026-01-01T00:00:00+00:00"  # model knowledge cutoff for no-lookahead tests


def _panel(*, beta: float, noise: float = 1.0, n_dates: int = 60,
           n_names: int = 25, seed: int = 0,
           compute_time: str = "2026-06-04T00:00:00+00:00") -> list[dict]:
    """fwd_return = beta*feature + noise — beta=0 is a null feature."""
    rng = np.random.default_rng(seed)
    panel: list[dict] = []
    for d in range(n_dates):
        f = rng.standard_normal(n_names)
        r = beta * f + noise * rng.standard_normal(n_names)
        for i in range(n_names):
            panel.append({
                "date": f"2026-06-{d + 1:03d}", "ticker": f"T{i}",
                "feature": float(f[i]), "fwd_return": float(r[i]),
                "compute_time": compute_time,
                "disclosure_type": "earnings" if i % 2 else "guidance",
                "market": "US" if i % 3 else "JP",
            })
    return panel


def _certify(panel, **kwargs):
    kwargs.setdefault("outcome_horizon_days", 5)
    kwargs.setdefault("placebo_panel", _panel(beta=0.0, seed=991))
    return certify(panel, **kwargs)


# ---------------------------------------------------------------------------
# Rank IC
# ---------------------------------------------------------------------------


def test_perfect_rank_ic_is_one() -> None:
    panel = [{"date": "d1", "ticker": f"T{i}", "feature": i, "fwd_return": i}
             for i in range(10)]
    assert rank_ic_series(panel)[0] > 0.999  # perfect rank corr (FP ~1.0)


def test_null_feature_has_near_zero_ic() -> None:
    s = ic_summary(_panel(beta=0.0, seed=1))
    assert abs(s["mean_ic"]) < 0.1
    assert abs(s["t_stat"]) < 2.0


def test_signal_feature_has_strong_positive_ic() -> None:
    s = ic_summary(_panel(beta=0.6, seed=2))
    assert s["mean_ic"] > 0.2
    assert s["t_stat"] > 3.0


# ---------------------------------------------------------------------------
# Long-short + Sharpe + DSR
# ---------------------------------------------------------------------------


def test_long_short_sign_follows_signal() -> None:
    pos = np.mean(long_short_pnl(_panel(beta=0.6, seed=3), cost_bps=0.0))
    neg = np.mean(long_short_pnl(_panel(beta=-0.6, seed=3), cost_bps=0.0))
    assert pos > 0 > neg


def test_long_short_cost_is_in_return_units() -> None:
    """Cost must reduce each date's LS return by exactly 2*cost_bps/1e4 (decimal).

    This is the R-round P1: returns and cost must share units, else the
    after-cost gate is a no-op.
    """
    panel = _panel(beta=0.6, seed=11)
    free = long_short_pnl(panel, cost_bps=0.0)
    costed = long_short_pnl(panel, cost_bps=100.0)
    assert all(abs((f - c) - 0.02) < 1e-9 for f, c in zip(free, costed))


def test_dsr_in_unit_interval_and_monotonic() -> None:
    base = dict(n_obs=60)
    # More trials → lower DSR (harder to clear).
    assert (deflated_sharpe_ratio(0.3, n_trials=1, **base)
            > deflated_sharpe_ratio(0.3, n_trials=10_000, **base))
    # Higher observed Sharpe → higher DSR.
    assert (deflated_sharpe_ratio(0.5, n_trials=10, **base)
            > deflated_sharpe_ratio(0.1, n_trials=10, **base))
    for v in (deflated_sharpe_ratio(0.3, n_trials=50, **base),):
        assert 0.0 < v < 1.0


def test_expected_max_sharpe_grows_with_trials() -> None:
    assert expected_max_sharpe(1000, 0.1) > expected_max_sharpe(10, 0.1) > 0


def test_sharpe_zero_for_constant_series() -> None:
    assert sharpe([0.01, 0.01, 0.01]) == 0.0


def test_cluster_robust_dsr_reduces_effective_n_for_overlapping_returns() -> None:
    rng = np.random.default_rng(44)
    innovations = rng.normal(0.001, 0.01, 200)
    overlapping = np.convolve(innovations, np.ones(5), mode="valid")

    robust = cluster_robust_dsr(
        overlapping,
        n_trials=10,
        outcome_horizon_days=5,
    )

    assert robust["method"] == "hac_bartlett_effective_n"
    assert robust["effective_n"] < robust["raw_n"]


def test_brier_calibration_rewards_confident_correct_directional_scores() -> None:
    good = [
        {"feature": 0.9, "fwd_return": 0.02},
        {"feature": -0.9, "fwd_return": -0.01},
        {"feature": 0.8, "fwd_return": 0.01},
        {"feature": -0.8, "fwd_return": -0.02},
    ]
    bad = [
        {"feature": -0.9, "fwd_return": 0.02},
        {"feature": 0.9, "fwd_return": -0.01},
        {"feature": -0.8, "fwd_return": 0.01},
        {"feature": 0.8, "fwd_return": -0.02},
    ]

    good_cal = brier_calibration(good)
    bad_cal = brier_calibration(bad)

    assert good_cal["n"] == 4
    assert good_cal["event_rate"] == 0.5
    assert good_cal["brier_score"] < bad_cal["brier_score"]


# ---------------------------------------------------------------------------
# Calibration — the gate must reject noise and accept signal
# ---------------------------------------------------------------------------


def test_null_feature_fails_certification() -> None:
    rec = _certify(_panel(beta=0.0, seed=4), feature_name="null", n_trials=10,
                   min_compute_time=_CUTOFF)
    assert rec["verdict"] == "observe_only"
    assert rec["reasons"]  # at least one gate failed


def test_signal_feature_passes_certification() -> None:
    rec = _certify(_panel(beta=0.7, seed=5), feature_name="signal", n_trials=5,
                   min_compute_time=_CUTOFF)
    assert rec["verdict"] == "certified", rec["reasons"]
    assert rec["ic_mean"] > 0 and rec["dsr"] >= 0.95
    assert rec["cluster_robust"]["effective_n"] <= rec["cluster_robust"]["raw_n"]
    assert rec["placebo"]["passed_gate"] is False


def test_certification_requires_cluster_horizon_and_placebo() -> None:
    panel = _panel(beta=0.7, seed=77)
    rec = certify(
        panel,
        feature_name="signal",
        n_trials=5,
        min_compute_time=_CUTOFF,
    )
    assert rec["verdict"] == "observe_only"
    assert any("cluster-robust" in reason for reason in rec["reasons"])
    assert any("placebo" in reason for reason in rec["reasons"])


def test_placebo_passing_gate_blocks_real_feature() -> None:
    rec = certify(
        _panel(beta=0.7, seed=78),
        feature_name="signal",
        n_trials=5,
        min_compute_time=_CUTOFF,
        outcome_horizon_days=5,
        placebo_panel=_panel(beta=0.7, seed=79),
    )
    assert rec["verdict"] == "observe_only"
    assert rec["placebo"]["passed_gate"] is True
    assert any("harness is not trustworthy" in reason for reason in rec["reasons"])


def test_certify_reports_directional_score_brier_calibration() -> None:
    panel = []
    for d in range(20):
        for i, score in enumerate((0.8, 0.6, -0.6, -0.8)):
            panel.append({
                "date": f"2026-07-{d + 1:02d}",
                "ticker": f"T{i}",
                "feature": score,
                "fwd_return": 0.01 if score > 0 else -0.01,
                "compute_time": "2026-07-01T00:00:00+00:00",
                "disclosure_type": "earnings",
                "market": "JP",
            })

    rec = certify(
        panel,
        feature_name="directional_score",
        n_trials=1,
        min_compute_time=_CUTOFF,
        outcome_horizon_days=5,
        placebo_panel=[],
    )

    assert rec["calibration"]["score_field"] == "feature"
    assert rec["calibration"]["n"] == len(panel)
    assert rec["calibration"]["brier_score"] < 0.05


def test_multiple_testing_deflates_a_signal() -> None:
    """Same signal, astronomically more trials → DSR collapses, verdict flips."""
    panel = _panel(beta=0.25, noise=1.0, seed=6)
    few = _certify(panel, feature_name="x", n_trials=1, min_compute_time=_CUTOFF)
    many = _certify(panel, feature_name="x", n_trials=10**9, min_compute_time=_CUTOFF)
    assert many["dsr"] < few["dsr"]
    assert many["verdict"] == "observe_only"


def test_certify_requires_a_cutoff() -> None:
    """No min_compute_time → cannot attest forward-collected → refuse."""
    rec = _certify(_panel(beta=0.7, seed=9), feature_name="signal", n_trials=5)
    assert rec["verdict"] == "observe_only"
    assert any("min_compute_time" in r for r in rec["reasons"])


def test_certify_rejects_pre_cutoff_data() -> None:
    """A strong signal computed BEFORE the cutoff is rejected (memorization risk),
    yet the same data with an earlier cutoff certifies — proving the gate is the
    only difference."""
    pre = _panel(beta=0.7, seed=9, compute_time="2025-06-01T00:00:00+00:00")
    blocked = _certify(pre, feature_name="signal", n_trials=5, min_compute_time=_CUTOFF)
    assert blocked["verdict"] == "observe_only"
    assert blocked["n_pre_cutoff"] > 0
    assert any("predate" in r for r in blocked["reasons"])

    ok = _certify(pre, feature_name="signal", n_trials=5,
                  min_compute_time="2025-01-01T00:00:00+00:00")
    assert ok["verdict"] == "certified", ok["reasons"]


def test_certify_requires_positive_n_trials() -> None:
    """n_trials=0 disables DSR deflation → must be rejected (R2 P2)."""
    rec = _certify(_panel(beta=0.7, seed=12), feature_name="signal", n_trials=0,
                   min_compute_time=_CUTOFF)
    assert rec["verdict"] == "observe_only"
    assert any("n_trials" in r for r in rec["reasons"])


def test_certify_rejects_mixed_extractor_versions() -> None:
    """A panel mixing extractor versions can't certify unless explicitly allowed."""
    panel = _panel(beta=0.7, seed=13)
    for i, o in enumerate(panel):
        o["model_id"] = "m"
        o["prompt_version"] = "p1" if i % 2 else "p2"
        o["feature_schema_version"] = "0.1.0"
    mixed = _certify(panel, feature_name="signal", n_trials=5, min_compute_time=_CUTOFF)
    assert mixed["verdict"] == "observe_only"
    assert any("extractor versions" in r for r in mixed["reasons"])

    ok = _certify(panel, feature_name="signal", n_trials=5, min_compute_time=_CUTOFF,
                  allow_mixed_versions=True)
    assert ok["verdict"] == "certified", ok["reasons"]


# ---------------------------------------------------------------------------
# Slices + persistence
# ---------------------------------------------------------------------------


def test_slice_ic_breaks_down_by_key() -> None:
    sl = slice_ic(_panel(beta=0.5, seed=7), "market")
    assert set(sl.keys()) <= {"US", "JP"}
    assert all("mean_ic" in v for v in sl.values())


def test_write_certification_appends(tmp_path: Path) -> None:
    rec = _certify(_panel(beta=0.0, seed=8), feature_name="null", n_trials=3,
                   min_compute_time=_CUTOFF)
    path = tmp_path / "feature_certifications.jsonl"
    write_certification(rec, path=path, fsync=False)
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["feature_name"] == "null" and row["verdict"] == "observe_only"


def test_certification_kill_switch_triggers_on_psi_drift() -> None:
    certified = {"feature_name": "directional_score", "verdict": "certified"}

    record = certification_kill_switch(
        certified,
        {"population_stability_index": 0.41, "rolling_ic_mean": 0.03},
        as_of="2026-07-01T00:00:00+00:00",
    )

    assert record["verdict"] == "observe_only"
    assert record["previous_verdict"] == "certified"
    assert record["kill_switch"]["triggered"] is True
    assert any("PSI" in reason for reason in record["reasons"])


def test_certification_kill_switch_triggers_on_rolling_ic_decay_and_placebo() -> None:
    certified = {"feature_name": "directional_score", "verdict": "certified"}

    record = certification_kill_switch(
        certified,
        {
            "rolling_ic_mean": -0.01,
            "placebo": {"passed_gate": True, "feature_name": "placebo_hash_score"},
        },
        as_of="2026-07-01T00:00:00+00:00",
    )

    assert record["verdict"] == "observe_only"
    assert any("rolling IC" in reason for reason in record["reasons"])
    assert any("placebo" in reason for reason in record["reasons"])


def test_certification_kill_switch_keeps_healthy_certified_record() -> None:
    certified = {"feature_name": "directional_score", "verdict": "certified"}

    record = certification_kill_switch(
        certified,
        {"population_stability_index": 0.04, "rolling_ic_mean": 0.08, "placebo_passed_gate": False},
        as_of="2026-07-01T00:00:00+00:00",
    )

    assert record["verdict"] == "certified"
    assert record["kill_switch"]["triggered"] is False
    assert record["reasons"] == []


def test_certify_excludes_rows_below_capacity_floor() -> None:
    panel = _panel(beta=0.7, seed=31, n_dates=20, n_names=5)
    for i, row in enumerate(panel):
        row["capacity_jpy"] = 10_000_000 if i % 5 == 0 else 100_000_000

    rec = _certify(
        panel,
        feature_name="signal",
        n_trials=5,
        min_compute_time=_CUTOFF,
    )

    assert rec["capacity_turnover_gate"]["min_capacity_jpy"] == 30_000_000
    assert rec["n_capacity_excluded"] == 20


def test_certify_excludes_rows_when_turnover_implied_capacity_is_too_small() -> None:
    panel = _panel(beta=0.7, seed=32, n_dates=20, n_names=5)
    for i, row in enumerate(panel):
        # 5% of ADV is the assumed executable capacity.
        row["avg_turnover_jpy"] = 100_000_000 if i % 5 == 0 else 2_000_000_000

    rec = _certify(
        panel,
        feature_name="signal",
        n_trials=5,
        min_compute_time=_CUTOFF,
    )

    assert rec["capacity_turnover_gate"]["max_adv_fraction"] == 0.05
    assert rec["n_capacity_excluded"] == 20


def test_paraphrase_stability_detects_sign_flips() -> None:
    result = paraphrase_stability([
        {"source_event_id": "a", "feature": 0.7, "paraphrase_run_id": "r1"},
        {"source_event_id": "a", "feature": -0.4, "paraphrase_run_id": "r2"},
        {"source_event_id": "b", "feature": 0.2, "paraphrase_run_id": "r1"},
        {"source_event_id": "b", "feature": 0.5, "paraphrase_run_id": "r2"},
    ])

    assert result["groups"] == 2
    assert result["unstable_group_count"] == 1
    assert result["stable_rate"] == 0.5
    assert result["unstable_groups"][0]["source_event_id"] == "a"


def test_certify_rejects_paraphrase_sign_flip_when_panel_is_provided() -> None:
    rec = _certify(
        _panel(beta=0.7, seed=41),
        feature_name="signal",
        n_trials=5,
        min_compute_time=_CUTOFF,
        paraphrase_panel=[
            {"source_event_id": "a", "feature": 0.7, "paraphrase_run_id": "r1"},
            {"source_event_id": "a", "feature": -0.6, "paraphrase_run_id": "r2"},
        ],
    )

    assert rec["verdict"] == "observe_only"
    assert rec["paraphrase_stability"]["unstable_group_count"] == 1
    assert any("paraphrase" in reason for reason in rec["reasons"])


def test_ensemble_agreement_reports_direction_consensus_rate() -> None:
    result = ensemble_agreement([
        {"source_event_id": "a", "feature": 0.7, "ensemble_run_id": "r1"},
        {"source_event_id": "a", "feature": 0.4, "ensemble_run_id": "r2"},
        {"source_event_id": "b", "feature": -0.2, "ensemble_run_id": "r1"},
        {"source_event_id": "b", "feature": 0.3, "ensemble_run_id": "r2"},
    ])

    assert result["groups"] == 2
    assert result["agreed_group_count"] == 1
    assert result["agreement_rate"] == 0.5
    assert result["disagreed_groups"][0]["source_event_id"] == "b"


def test_certify_reports_ensemble_agreement_when_panel_is_provided() -> None:
    rec = _certify(
        _panel(beta=0.7, seed=42),
        feature_name="signal",
        n_trials=5,
        min_compute_time=_CUTOFF,
        ensemble_panel=[
            {"source_event_id": "a", "feature": 0.7, "ensemble_run_id": "r1"},
            {"source_event_id": "a", "feature": 0.6, "ensemble_run_id": "r2"},
        ],
    )

    assert rec["ensemble_agreement"]["groups"] == 1
    assert rec["ensemble_agreement"]["agreement_rate"] == 1.0

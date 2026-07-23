"""Tests for build_panel_from_logs — joining stored features to realized outcomes.

Offline: synthetic feature rows + synthetic outcome rows written to temp JSONL.
Verifies the join key, the drop rules, horizon matching, and that an assembled
panel feeds certify() end to end.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from feature_validation import build_panel_from_logs, certify  # noqa: E402
from almanac.observability.catalyst_layer import disclosure_hypothesis_id  # noqa: E402


def _wj(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# disclosure_hypothesis_id (shared join key)
# ---------------------------------------------------------------------------


def test_hid_none_for_non_actionable_or_anchorless() -> None:
    assert disclosure_hypothesis_id("AAPL", None, "edgar:a") is None
    assert disclosure_hypothesis_id("AAPL", 0, "edgar:a") is None
    assert disclosure_hypothesis_id("AAPL", 0.5, "") is None
    assert disclosure_hypothesis_id(None, 0.5, "edgar:a") is None


def test_hid_stable_and_sign_sensitive() -> None:
    a = disclosure_hypothesis_id("AAPL", 0.5, "edgar:a")
    assert a == disclosure_hypothesis_id("AAPL", 0.9, "edgar:a")   # both buy
    assert a != disclosure_hypothesis_id("AAPL", -0.5, "edgar:a")  # trim differs


def test_hid_is_extractor_version_sensitive() -> None:
    common = {
        "ticker": "AAPL",
        "directional_score": 0.5,
        "source_event_id": "edgar:a",
        "model_id": "deepseek-v4-flash",
        "feature_schema_version": "0.1.0",
    }
    p1 = disclosure_hypothesis_id(**common, prompt_version="p1")
    p2 = disclosure_hypothesis_id(**common, prompt_version="p2")
    assert p1 != p2


# ---------------------------------------------------------------------------
# build_panel_from_logs
# ---------------------------------------------------------------------------


def test_join_matches_and_drops(tmp_path: Path) -> None:
    feats, outs = [], []
    for i in range(3):
        ds, sid = 0.5, f"edgar:acc-{i}"
        feats.append({"ticker": f"T{i}", "directional_score": ds, "source_event_id": sid,
                      "event_at": "2026-06-01T00:00:00+00:00",
                      "compute_time": "2026-06-01T00:00:00+00:00",
                      "disclosure_type": "earnings", "market": "US"})
        hid = disclosure_hypothesis_id(f"T{i}", ds, sid)
        outs.append({"hypothesis_id": hid, "horizon_days": 20, "excess_return_bps": float(i)})
    # no matching outcome → dropped
    feats.append({"ticker": "NOPE", "directional_score": 0.5,
                  "source_event_id": "edgar:none", "event_at": "2026-06-01T00:00:00+00:00"})
    # no actionable direction → no hid → dropped
    feats.append({"ticker": "ZERO", "directional_score": 0.0,
                  "source_event_id": "edgar:z", "event_at": "2026-06-01T00:00:00+00:00"})

    f, o = tmp_path / "f.jsonl", tmp_path / "o.jsonl"
    _wj(f, feats); _wj(o, outs)
    panel = build_panel_from_logs(feature_name="directional_score", horizon_days=20,
                                  features_path=f, outcome_log_path=o)
    assert len(panel) == 3
    assert {p["fwd_return"] for p in panel} == {0.0, 0.0001, 0.0002}  # bps→decimal
    assert all(p["feature"] == 0.5 for p in panel)
    assert all(p["date"] == "2026-06-01" for p in panel)  # event-date origin


def test_excess_return_bps_normalized_to_decimal(tmp_path: Path) -> None:
    ds, sid = 0.5, "edgar:a"
    feats = [{"ticker": "T", "directional_score": ds, "source_event_id": sid,
              "event_at": "2026-06-01T00:00:00+00:00"}]
    hid = disclosure_hypothesis_id("T", ds, sid)
    outs = [{"hypothesis_id": hid, "horizon_days": 20, "excess_return_bps": 100.0}]
    f, o = tmp_path / "f.jsonl", tmp_path / "o.jsonl"
    _wj(f, feats); _wj(o, outs)
    panel = build_panel_from_logs(feature_name="directional_score", horizon_days=20,
                                  features_path=f, outcome_log_path=o)
    assert panel[0]["fwd_return"] == 0.01   # 100 bps → 0.01 decimal


def test_horizon_must_match(tmp_path: Path) -> None:
    ds, sid = 0.5, "edgar:a"
    feats = [{"ticker": "T", "directional_score": ds, "source_event_id": sid,
              "event_at": "2026-06-01T00:00:00+00:00"}]
    hid = disclosure_hypothesis_id("T", ds, sid)
    outs = [{"hypothesis_id": hid, "horizon_days": 5, "excess_return_bps": 3.0}]
    f, o = tmp_path / "f.jsonl", tmp_path / "o.jsonl"
    _wj(f, feats); _wj(o, outs)
    assert build_panel_from_logs(feature_name="directional_score", horizon_days=20,
                                 features_path=f, outcome_log_path=o) == []
    assert len(build_panel_from_logs(feature_name="directional_score", horizon_days=5,
                                     features_path=f, outcome_log_path=o)) == 1


def test_assembled_panel_feeds_certify(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    feats, outs, n = [], [], 0
    base = datetime(2026, 1, 1)
    for d in range(60):
        date = (base + timedelta(days=d)).strftime("%Y-%m-%dT00:00:00+00:00")
        for k in range(20):
            ds = float(rng.standard_normal()) or 0.01
            sid = f"edgar:{d}-{k}"
            feats.append({"ticker": f"T{k}", "directional_score": ds, "source_event_id": sid,
                          "event_at": date, "compute_time": date,
                          "disclosure_type": "earnings", "market": "US"})
            hid = disclosure_hypothesis_id(f"T{k}", ds, sid)
            # bps-scale excess return (build_panel normalizes →decimal); strong signal
            ret = float(80.0 * ds + 120.0 * rng.standard_normal())
            outs.append({"hypothesis_id": hid, "horizon_days": 20, "excess_return_bps": ret})
            n += 1
    f, o = tmp_path / "f.jsonl", tmp_path / "o.jsonl"
    _wj(f, feats); _wj(o, outs)
    panel = build_panel_from_logs(feature_name="directional_score", horizon_days=20,
                                  features_path=f, outcome_log_path=o)
    assert len(panel) == n
    placebo = [
        {**row, "feature": float(rng.standard_normal())}
        for row in panel
    ]
    rec = certify(panel, feature_name="directional_score", n_trials=5,
                  min_compute_time="2025-12-01T00:00:00+00:00",
                  outcome_horizon_days=20, placebo_panel=placebo)
    assert rec["ic_mean"] > 0 and rec["verdict"] == "certified", rec["reasons"]


def test_non_numeric_outcome_return_skipped(tmp_path: Path) -> None:
    """A non-numeric outcome return must skip that obs, not crash assembly."""
    ds, sid = 0.5, "edgar:a"
    feats = [{"ticker": "T", "directional_score": ds, "source_event_id": sid,
              "compute_time": "2026-06-01T00:00:00+00:00"}]
    hid = disclosure_hypothesis_id("T", ds, sid)
    outs = [{"hypothesis_id": hid, "horizon_days": 20, "excess_return_bps": "N/A"}]
    f, o = tmp_path / "f.jsonl", tmp_path / "o.jsonl"
    _wj(f, feats); _wj(o, outs)
    assert build_panel_from_logs(feature_name="directional_score", horizon_days=20,
                                 features_path=f, outcome_log_path=o) == []


def test_build_panel_dedups_versions_by_event(tmp_path: Path) -> None:
    """Two extractor versions of the same disclosure must collapse to ONE obs
    (R-round P1: else n_obs inflates and DSR deflation is too weak)."""
    ds, sid = 0.5, "edgar:dup"
    feats = [
        {"ticker": "T", "directional_score": ds, "source_event_id": sid,
         "compute_time": "2026-06-01T00:00:00+00:00", "prompt_version": "p1",
         "model_id": "m", "feature_schema_version": "0.1.0"},
        {"ticker": "T", "directional_score": ds, "source_event_id": sid,
         "compute_time": "2026-06-02T00:00:00+00:00", "prompt_version": "p2",
         "model_id": "m", "feature_schema_version": "0.1.0"},
    ]
    hid_p1 = disclosure_hypothesis_id(
        "T", ds, sid, model_id="m", prompt_version="p1",
        feature_schema_version="0.1.0")
    hid_p2 = disclosure_hypothesis_id(
        "T", ds, sid, model_id="m", prompt_version="p2",
        feature_schema_version="0.1.0")
    outs = [
        {"hypothesis_id": hid_p1, "horizon_days": 20, "excess_return_bps": 10.0},
        {"hypothesis_id": hid_p2, "horizon_days": 20, "excess_return_bps": 50.0},
    ]
    f, o = tmp_path / "f.jsonl", tmp_path / "o.jsonl"
    _wj(f, feats); _wj(o, outs)

    panel = build_panel_from_logs(feature_name="directional_score", horizon_days=20,
                                  features_path=f, outcome_log_path=o)
    assert len(panel) == 1                                   # deduped by event
    assert panel[0]["compute_time"] == "2026-06-02T00:00:00+00:00"  # latest kept
    assert panel[0]["fwd_return"] == 0.005  # p2 outcome, never p1

    p1 = build_panel_from_logs(feature_name="directional_score", horizon_days=20,
                               prompt_version="p1", features_path=f, outcome_log_path=o)
    assert len(p1) == 1 and p1[0]["compute_time"] == "2026-06-01T00:00:00+00:00"
    assert p1[0]["fwd_return"] == 0.001

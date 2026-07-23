"""disclosure_feature_promotion: 開示特徴量タイプ別昇格判定の回帰テスト"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import disclosure_feature_promotion as dfp  # noqa: E402
from almanac.observability.catalyst_layer import (  # noqa: E402
    _disclosure_action_type,
    disclosure_directional_value,
    disclosure_hypothesis_id,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _feature_row(ticker, directional_score, source_event_id, disclosure_type="stake"):
    return {
        "ticker": ticker,
        "directional_score": directional_score,
        "source_event_id": source_event_id,
        "disclosure_type": disclosure_type,
        "model_id": None,
        "prompt_version": None,
        "feature_schema_version": None,
    }


def _outcome_row_for(feature_row, *, horizon_days=20, excess_return_bps=None):
    ds = disclosure_directional_value(feature_row)
    action_type = _disclosure_action_type(ds, feature_row)
    hid = disclosure_hypothesis_id(
        feature_row["ticker"], ds, feature_row["source_event_id"],
        model_id=feature_row.get("model_id"),
        prompt_version=feature_row.get("prompt_version"),
        feature_schema_version=feature_row.get("feature_schema_version"),
        action_type=action_type,
    )
    return {"hypothesis_id": hid, "horizon_days": horizon_days, "excess_return_bps": excess_return_bps}


def test_join_recovers_hypothesis_id_and_aggregates(tmp_path):
    features_path = tmp_path / "features.jsonl"
    outcome_path = tmp_path / "outcomes.jsonl"

    features = [_feature_row("AAA", 0.5, f"evt{i}", "stake") for i in range(3)]
    _write_jsonl(features_path, features)
    outcomes = [_outcome_row_for(f, excess_return_bps=100.0) for f in features]
    _write_jsonl(outcome_path, outcomes)

    agg = dfp.aggregate_by_disclosure_type(features_path=features_path, outcome_log_path=outcome_path)
    assert agg["stake"]["n"] == 3
    assert agg["stake"]["hit_rate"] == 1.0
    assert agg["stake"]["mean_excess_return_bps"] == 100.0


def test_zero_directional_score_is_skipped(tmp_path):
    features_path = tmp_path / "features.jsonl"
    outcome_path = tmp_path / "outcomes.jsonl"
    _write_jsonl(features_path, [_feature_row("AAA", 0.0, "evt1", "earnings")])
    _write_jsonl(outcome_path, [])

    agg = dfp.aggregate_by_disclosure_type(features_path=features_path, outcome_log_path=outcome_path)
    assert agg == {}


def test_missing_source_event_id_is_skipped(tmp_path):
    features_path = tmp_path / "features.jsonl"
    outcome_path = tmp_path / "outcomes.jsonl"
    row = _feature_row("AAA", 0.5, None, "earnings")
    _write_jsonl(features_path, [row])
    _write_jsonl(outcome_path, [])

    agg = dfp.aggregate_by_disclosure_type(features_path=features_path, outcome_log_path=outcome_path)
    assert agg == {}


def test_horizon_mismatch_does_not_join(tmp_path):
    features_path = tmp_path / "features.jsonl"
    outcome_path = tmp_path / "outcomes.jsonl"
    features = [_feature_row("AAA", 0.5, "evt1", "stake")]
    _write_jsonl(features_path, features)
    # horizon_days=5 の outcome しかない -> horizon_days=20 (デフォルト) では結合されない
    _write_jsonl(outcome_path, [_outcome_row_for(features[0], horizon_days=5, excess_return_bps=100.0)])

    agg = dfp.aggregate_by_disclosure_type(features_path=features_path, outcome_log_path=outcome_path)
    assert agg == {}


def test_promotion_verdicts_thresholds():
    agg = {
        "stake": {"n": 40, "hit_rate": 0.6, "mean_excess_return_bps": 50.0},
        "earnings": {"n": 60, "hit_rate": 0.3, "mean_excess_return_bps": -80.0},
        "mna": {"n": 5, "hit_rate": 0.9, "mean_excess_return_bps": 500.0},
    }
    verdicts = dfp.promotion_verdicts(agg)
    assert verdicts["stake"]["verdict"] == "promote"
    assert verdicts["earnings"]["verdict"] == "retire"
    assert verdicts["mna"]["verdict"] == "insufficient_data"


def test_falls_back_to_return_pct_when_excess_missing(tmp_path):
    features_path = tmp_path / "features.jsonl"
    outcome_path = tmp_path / "outcomes.jsonl"
    features = [_feature_row("AAA", 0.5, "evt1", "stake")]
    _write_jsonl(features_path, features)
    ds = disclosure_directional_value(features[0])
    action_type = _disclosure_action_type(ds, features[0])
    hid = disclosure_hypothesis_id(features[0]["ticker"], ds, features[0]["source_event_id"], action_type=action_type)
    _write_jsonl(outcome_path, [{"hypothesis_id": hid, "horizon_days": 20, "return_pct": 0.02}])

    agg = dfp.aggregate_by_disclosure_type(features_path=features_path, outcome_log_path=outcome_path)
    assert agg["stake"]["mean_excess_return_bps"] == 200.0  # 0.02 * 10000

"""disclosure_feature_promotion.py — 開示特徴量タイプ別の昇格/維持/廃止判定。

攻めバックログ 2026-07 項目3。data/disclosure_features.jsonl の各行から
catalyst_layer.disclosure_hypothesis_id() で hypothesis_id を再計算し
(catalyst_layer.py のdocstring: "Phase-1 panel-assembly join" と同じ設計。
catalyst_hypothesis_log.jsonl 自体には source_event_id が書き込まれていない
ため、この再計算がJOINの唯一の経路)、catalyst_outcome_log.jsonl と
disclosure_type 単位で結合して hit率・平均超過リターンを集計する。

新しいscreenerを手作りする代わりに、既存のobserve_only特徴量パイプラインへの
昇格として実現する（攻めバックログの設計方針どおり）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
DISCLOSURE_FEATURES_PATH = BASE_DIR / "data" / "disclosure_features.jsonl"
CATALYST_OUTCOME_LOG_PATH = BASE_DIR / "catalyst_outcome_log.jsonl"
HORIZON_DAYS = 20  # catalyst_layer._HORIZON_DISCLOSURE と一致させる

MIN_MEASURED_N_PROMOTE = 30
MIN_MEASURED_N_RETIRE = 50
MIN_HIT_RATE_PROMOTE = 0.55
MAX_HIT_RATE_RETIRE = 0.45


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _hypothesis_id_for_feature_row(row: dict) -> Optional[str]:
    """disclosure_features の1行から、synthesize_from_disclosure_features と
    同一の hypothesis_id を再計算する。"""
    from almanac.observability.catalyst_layer import (
        _disclosure_action_type,
        disclosure_directional_value,
        disclosure_hypothesis_id,
    )

    ds = disclosure_directional_value(row)
    if ds is None or ds == 0:
        return None
    ticker = row.get("ticker")
    source_event_id = row.get("source_event_id")
    if not ticker or not source_event_id:
        return None
    action_type = _disclosure_action_type(ds, row)
    return disclosure_hypothesis_id(
        ticker,
        ds,
        source_event_id,
        model_id=row.get("model_id"),
        prompt_version=row.get("prompt_version"),
        feature_schema_version=row.get("feature_schema_version"),
        action_type=action_type,
    )


def aggregate_by_disclosure_type(
    *,
    horizon_days: int = HORIZON_DAYS,
    features_path: Optional[Path] = None,
    outcome_log_path: Optional[Path] = None,
) -> dict[str, dict]:
    """disclosure_type別に n/hit_rate/mean_excess_return_bps を集計する。"""
    f_path = Path(features_path) if features_path is not None else DISCLOSURE_FEATURES_PATH
    o_path = Path(outcome_log_path) if outcome_log_path is not None else CATALYST_OUTCOME_LOG_PATH

    outcomes_by_hid: dict[str, list[dict]] = {}
    for row in _read_jsonl(o_path):
        if row.get("horizon_days") != horizon_days:
            continue
        hid = row.get("hypothesis_id")
        if not hid:
            continue
        outcomes_by_hid.setdefault(hid, []).append(row)

    type_values: dict[str, list[float]] = {}
    for row in _read_jsonl(f_path):
        dtype = row.get("disclosure_type") or "other"
        hid = _hypothesis_id_for_feature_row(row)
        if not hid:
            continue
        for outcome in outcomes_by_hid.get(hid, []):
            excess = outcome.get("excess_return_bps")
            if excess is None:
                ret = outcome.get("return_pct")
                if ret is None:
                    continue
                excess = ret * 10_000
            if not isinstance(excess, (int, float)):
                continue
            type_values.setdefault(dtype, []).append(float(excess))

    result: dict[str, dict] = {}
    for dtype, values in type_values.items():
        n = len(values)
        hits = sum(1 for v in values if v > 0)
        hit_rate = (hits / n) if n else None
        mean_excess = (sum(values) / n) if n else None
        result[dtype] = {
            "n": n,
            "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "mean_excess_return_bps": round(mean_excess, 2) if mean_excess is not None else None,
        }
    return result


def promotion_verdicts(agg: dict[str, dict]) -> dict[str, dict]:
    """各 disclosure_type に promote/maintain/retire/insufficient_data を付与する。"""
    out: dict[str, dict] = {}
    for dtype, stats in agg.items():
        n = stats["n"]
        hit_rate = stats["hit_rate"]
        mean_excess = stats["mean_excess_return_bps"] or 0
        if n < MIN_MEASURED_N_PROMOTE:
            verdict, reason = "insufficient_data", f"n={n} < {MIN_MEASURED_N_PROMOTE}"
        elif hit_rate is not None and hit_rate >= MIN_HIT_RATE_PROMOTE and mean_excess > 0:
            verdict, reason = "promote", f"hit_rate={hit_rate:.2f} >= {MIN_HIT_RATE_PROMOTE}, mean_excess={mean_excess:.1f}bps"
        elif n >= MIN_MEASURED_N_RETIRE and hit_rate is not None and hit_rate < MAX_HIT_RATE_RETIRE:
            verdict, reason = "retire", f"hit_rate={hit_rate:.2f} < {MAX_HIT_RATE_RETIRE} (n={n})"
        else:
            verdict, reason = "maintain", (
                f"hit_rate={hit_rate:.2f} (昇格・廃止いずれの基準も未達)" if hit_rate is not None else "計測不足"
            )
        out[dtype] = {**stats, "verdict": verdict, "reason": reason}
    return out


if __name__ == "__main__":
    agg = aggregate_by_disclosure_type()
    verdicts = promotion_verdicts(agg)
    print(json.dumps(verdicts, ensure_ascii=False, indent=2))

"""Diagnostics of published analysis bytes, never investment authority.

Stages are independently reported lists, not a conserved funnel: rejection
lists can overlap and deterministic candidates can appear after synthesis.
No position, price, fee or borrowing evidence is certified by this observer.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


SCHEMA_VERSION = "analysis-pipeline-observation-v1"
RELATIVE_DIRECTORY = Path("logs/analysis_pipeline_observations")
MAX_CACHE_BYTES = 32 * 1024 * 1024
ACTION_TYPES = frozenset({
    "buy", "add", "dca", "margin_buy", "sell", "trim", "reduce",
    "stop_loss", "take_profit", "short", "cover", "rebalance", "hold",
})
READINESS = frozenset({"ready", "review", "blocked", "unknown"})


def _reject_constant(_value):
    raise ValueError("nonfinite_json")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _decode(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_CACHE_BYTES:
        raise ValueError("analysis_bytes_invalid")
    value = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise ValueError("analysis_object_required")
    return value


def _reference(value):
    return value if isinstance(value, str) and value.strip() and len(value) <= 160 else None


def _same_report(stored, expected):
    # Python considers False == 0 and True == 1; diagnostic flags must keep
    # their JSON types rather than accepting a corrupted numeric substitute.
    return json.dumps(stored, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)


def _label(value, choices):
    if not isinstance(value, str):
        return "unknown"
    value = value.strip().lower()
    return value if value in choices else "unknown"


def _rows(parent, key, *, wrapped=False):
    if not isinstance(parent, dict) or key not in parent:
        return {"status": "missing", "row_count": None}
    rows = parent[key]
    if not isinstance(rows, list):
        return {"status": "malformed", "row_count": None}
    candidates = [row.get("action") if isinstance(row, dict) else None for row in rows] if wrapped else rows
    valid = [row for row in candidates if isinstance(row, dict)]
    types = Counter(_label(row.get("type"), ACTION_TYPES) for row in valid)
    readiness = Counter(_label(row.get("execution_readiness"), READINESS) for row in valid)
    reasons = Counter()
    if wrapped:
        for row in rows:
            rule = row.get("rule") if isinstance(row, dict) else None
            if isinstance(rule, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", rule):
                reasons[rule] += 1
            else:
                reasons["unstructured_policy_rule"] += 1
    for row in valid:
        codes = set()
        blocks = row.get("execution_block_reasons")
        if isinstance(blocks, list):
            for block in blocks:
                code = block.get("code") if isinstance(block, dict) else None
                if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                    codes.add(code)
                else:
                    codes.add("unstructured_reason")
        if row.get("filtered_reason") or row.get("non_executable_reason"):
            # Free text may include instruments, amounts or accounts. Do not
            # turn its first word into a supposedly authoritative reason code.
            codes.add("reported_filter_reason_present")
        reasons.update(codes)
    return {
        "status": "reported" if len(valid) == len(rows) else "malformed_rows",
        "row_count": len(rows), "object_row_count": len(valid),
        "malformed_row_count": len(rows) - len(valid),
        "reported_action_types": dict(sorted(types.items())),
        "reported_execution_readiness": dict(sorted(readiness.items())),
        "reported_reason_codes": dict(sorted(reasons.items())),
    }


def summarize_analysis_bytes(raw: bytes) -> dict:
    """Pure, source-bound report. Absent lists are unknown, not empty."""
    data = _decode(raw)
    synthesis = data.get("synthesis")
    synthesis = synthesis if isinstance(synthesis, dict) else {}
    stages = {key: _rows(synthesis, key) for key in (
        "raw_priority_actions", "post_policy_priority_actions", "priority_actions",
        "_filtered_actions", "order_intent_deferred_actions",
    )}
    # The legacy combined field includes MODIFIED as well as rejected rows.
    # Inspect the explicit decision lists independently; never call their sum
    # a count of candidates removed from the pipeline.
    stages["policy_filtered_actions"] = _rows(synthesis, "policy_filtered_actions", wrapped=True)
    for key in ("rejected", "modified"):
        stages[f"policy_{key}"] = _rows(synthesis.get("policy_decision"), key, wrapped=True)
    tiers = {}
    for name, keys in {
        "long_analysis": ("priority_actions",),
        "medium_analysis": ("priority_actions",),
        "short_positions_analysis": ("priority_actions",),
        "margin_long_analysis": ("margin_long_picks", "priority_actions"),
        "short_selling_analysis": ("short_opportunities", "priority_actions"),
    }.items():
        # Preserve both source fields: empty primary does not mean fallback,
        # and a candidate without type does not become an authorized short.
        tiers[name] = {key: _rows(data.get(name), key) for key in keys}
    identifier = _reference(synthesis.get("analysis_id"))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "reported" if identifier else "unbound",
        "analysis_id": identifier,
        "reported_as_of": _reference(data.get("as_of")),
        "reported_decision_snapshot_id": _reference(synthesis.get("decision_snapshot_id")),
        "source_cache_sha256": hashlib.sha256(raw).hexdigest(),
        "stages": stages, "saved_tier_outputs": tiers,
        "stage_counts_are_unique_candidates": False,
        "policy_filtered_includes_modifications": True,
        "cross_stage_attrition_verified": False,
        "run_completion_verified": False,
        "growth_candidate_quality": "not_evaluated",
        "growth_lifecycle_cost": "not_evaluated",
        "growth_post_trade_risk": "not_evaluated",
        "rollout_evidence_qualified": False,
        "execution_authorized": False,
        "policy_changed": False,
    }


def _path(cache_path, digest):
    return cache_path.parent / RELATIVE_DIRECTORY / f"{SCHEMA_VERSION}-{digest}.json"


def publish_saved_analysis(cache_path: Path) -> dict:
    """Call after authoritative cache commit under its existing writer lock.

One deterministic artifact per byte generation. No clock, heartbeat, latest
pointer, history mutation, source rewrite, schema migration or network call.
Concurrent exact publication produces identical content; conflicting existing
artifacts are refused rather than repaired. This is private diagnostic storage.
"""
    cache_path = Path(cache_path)
    raw = cache_path.read_bytes()
    report = summarize_analysis_bytes(raw)
    target = _path(cache_path, report["source_cache_sha256"])
    if cache_path.read_bytes() != raw:
        raise ValueError("analysis_changed_during_observation")
    if target.exists():
        if not _same_report(_decode(target.read_bytes()), report):
            raise ValueError("observation_conflict")
        return {"recorded": True, "duplicate": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    from utils import atomic_write_json
    atomic_write_json(target, report)
    return {"recorded": True, "duplicate": False}


def read_current_observation(cache_path: Path) -> dict:
    """No creation. An older generation can never stand in for today's cache."""
    try:
        cache_path = Path(cache_path)
        raw = cache_path.read_bytes()
        expected = summarize_analysis_bytes(raw)
        stored = _decode(_path(cache_path, expected["source_cache_sha256"]).read_bytes())
        if not _same_report(stored, expected) or cache_path.read_bytes() != raw:
            raise ValueError("observation_not_bound_to_current_analysis")
        return stored
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return {"schema_version": SCHEMA_VERSION, "status": "unavailable",
                "execution_authorized": False, "rollout_evidence_qualified": False}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Read private analysis diagnostics; no writes")
    parser.add_argument("--cache", required=True, type=Path)
    result = read_current_observation(parser.parse_args().cache)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result["status"] == "reported" else 2)

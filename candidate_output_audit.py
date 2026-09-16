"""Saved-output integrity only; not candidate lineage or issuer authentication.

The host seals final candidates at the cache write boundary, after all legitimate
rewrites. A manifest and payload changed together cannot be authenticated here.
"""
from __future__ import annotations

import copy
import hashlib
import json

KEY = "candidate_output_manifest"
VERSION = 1
GROUPS = ("priority_actions", "_filtered_actions", "order_intent_deferred_actions",
          "policy_filtered_actions")


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_manifest(artifact: dict) -> dict:
    synthesis = artifact.get("synthesis")
    if not isinstance(synthesis, dict):
        raise ValueError("synthesis must be an object")
    groups = {}
    for name in GROUPS:
        rows = synthesis.get(name, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("candidate groups must be lists of objects")
        # Bucket and ordinal are output locators, NOT cross-stage candidate IDs.
        groups[name] = [_digest(row) for row in rows]
    return {
        "schema_version": VERSION,
        "status": "sealed",
        "run_digest": _digest({"analysis_id": artifact.get("analysis_id"),
                               "as_of": artifact.get("as_of")}),
        "groups": groups,
        "summary_digest": _digest(synthesis.get("decision_summary")),
    }


def seal_for_save(artifact: dict) -> dict:
    """Return a separate snapshot; never trust a model-provided manifest.

    Audit failure is diagnostic, not a new trading gate or a reason to discard
    the analysis. The existing serializer still owns payload serializability.
    """
    result = copy.deepcopy(artifact)
    try:
        manifest = build_manifest(result)
    except (ValueError, TypeError, OverflowError):
        manifest = {"schema_version": VERSION, "status": "unavailable"}
    result[KEY] = manifest
    return result


def verify_manifest(artifact: dict) -> str:
    """Return verified, legacy_unverifiable, or invalid (no authenticity claim)."""
    if KEY not in artifact:
        return "legacy_unverifiable"
    supplied = artifact[KEY]
    if not isinstance(supplied, dict) or type(supplied.get("schema_version")) is not int:
        return "invalid"
    try:
        return "verified" if supplied == build_manifest(artifact) else "invalid"
    except (ValueError, TypeError, OverflowError):
        return "invalid"

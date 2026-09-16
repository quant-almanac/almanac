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
#: Only priority_actions' row order is semantically meaningful (it encodes
#: execution/ranking priority). The other three groups are unordered
#: collections in practice; storing their digests as a sorted list lets a
#: harmless reordering (e.g. a future refactor that changes how a bucket is
#: assembled, without changing its content) not be reported as tampering,
#: while a dropped/added/substituted/duplicated row still changes the sorted
#: digest list (2026-09 review).
ORDER_SENSITIVE_GROUPS = frozenset({"priority_actions"})


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
        digests = [_digest(row) for row in rows]
        groups[name] = digests if name in ORDER_SENSITIVE_GROUPS else sorted(digests)
    # The real run identity lives at synthesis["analysis_id"]; the artifact's
    # own top level never sets one (analyst/__init__.py's result dict has no
    # top-level analysis_id key). Falling back to the top level only when the
    # synthesis-level id is absent matches order_strategy._validate_formal_analysis's
    # own precedence, so run_digest actually varies with the real run identity
    # instead of always hashing None in production (2026-09 review).
    analysis_id = synthesis.get("analysis_id")
    if analysis_id is None:
        analysis_id = artifact.get("analysis_id")
    return {
        "schema_version": VERSION,
        "status": "sealed",
        "run_digest": _digest({"analysis_id": analysis_id,
                               "as_of": artifact.get("as_of")}),
        "groups": groups,
        "summary_digest": _digest(synthesis.get("decision_summary")),
    }


def seal_for_save(artifact: dict) -> dict:
    """Return a separate snapshot; never trust a model-provided manifest.

    Audit failure is diagnostic, not a new trading gate or a reason to discard
    the analysis. The existing serializer still owns payload serializability.
    """
    try:
        result = copy.deepcopy(artifact)
    except Exception:
        # A full deep copy itself failed (e.g. RecursionError on a pathological
        # structure, or a copy.Error/custom exception from an accidentally
        # embedded non-JSON-native object) -- an exception type build_manifest's
        # own except clause below does not claim to catch. Degrade to a shallow
        # top-level copy (always safe, never recurses) so the caller's dict is
        # still never mutated in place, and mark the manifest unavailable
        # rather than letting an unrelated audit failure crash the save path
        # this function's own docstring says it must never do (2026-09 review).
        result = dict(artifact)
        result[KEY] = {"schema_version": VERSION, "status": "unavailable"}
        return result
    try:
        manifest = build_manifest(result)
    except (ValueError, TypeError, OverflowError):
        manifest = {"schema_version": VERSION, "status": "unavailable"}
    result[KEY] = manifest
    return result


def verify_manifest(artifact: dict) -> str:
    """Return verified, legacy_unverifiable, unavailable, or invalid.

    No authenticity claim in any case: this only checks self-consistency
    between a saved payload and its own embedded manifest.
    """
    if not isinstance(artifact, dict):
        return "invalid"
    if KEY not in artifact:
        return "legacy_unverifiable"
    supplied = artifact[KEY]
    if not isinstance(supplied, dict) or type(supplied.get("schema_version")) is not int:
        return "invalid"
    if supplied.get("status") == "unavailable":
        # seal_for_save deliberately could not build a manifest for this
        # content (e.g. NaN/Infinity in a row, or a deepcopy failure) and
        # marked that as a benign degrade, not tampering. Confirm the same
        # content is still unrepresentable; if it now builds cleanly, the
        # stored "unavailable" claim no longer matches this artifact, which
        # is itself a real discrepancy (2026-09 review).
        try:
            build_manifest(artifact)
        except (ValueError, TypeError, OverflowError):
            return "unavailable"
        return "invalid"
    try:
        return "verified" if supplied == build_manifest(artifact) else "invalid"
    except (ValueError, TypeError, OverflowError):
        return "invalid"

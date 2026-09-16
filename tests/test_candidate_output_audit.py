import copy
import json
from contextlib import nullcontext

import pytest

from candidate_output_audit import GROUPS, KEY, seal_for_save, verify_manifest


def artifact():
    return {"analysis_id": "synthetic-run", "as_of": "2026-09-16T00:00:00+00:00",
            "synthesis": {**{group: [{"ticker": "SYNTH", "type": "buy", "quantity": 1}]
                             for group in GROUPS},
                          "decision_summary": {"candidate_count": 1}}}


def test_roundtrip_and_no_input_mutation():
    original = artifact()
    before = copy.deepcopy(original)
    saved = seal_for_save(original)
    assert original == before
    assert verify_manifest(json.loads(json.dumps(saved))) == "verified"
    original["synthesis"]["priority_actions"][0]["ticker"] = "CHANGED"
    assert verify_manifest(saved) == "verified"


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("change", ["ticker", "quantity", "drop", "duplicate"])
def test_changed_content_is_detected_even_when_counts_match(group, change):
    saved = seal_for_save(artifact())
    rows = saved["synthesis"][group]
    if change == "drop":
        rows.clear()
    elif change == "duplicate":
        rows.append(dict(rows[0]))
    else:
        rows[0][change] = "REPLACED" if change == "ticker" else 2
    assert verify_manifest(saved) == "invalid"


@pytest.mark.parametrize("field", ["analysis_id", "as_of", "summary"])
def test_run_and_summary_binding(field):
    saved = seal_for_save(artifact())
    if field == "summary":
        saved["synthesis"]["decision_summary"]["candidate_count"] = 999
    else:
        saved[field] = "changed"
    assert verify_manifest(saved) == "invalid"


@pytest.mark.parametrize("invalid", [None, [], {}, {"schema_version": True},
                                    {"schema_version": 999}])
def test_invalid_manifest_is_not_legacy(invalid):
    saved = artifact()
    saved[KEY] = invalid
    assert verify_manifest(saved) == "invalid"


def test_absent_manifest_is_explicitly_unverified():
    assert verify_manifest(artifact()) == "legacy_unverifiable"


@pytest.mark.parametrize("invalid", [None, {}, [None], [{"quantity": float("nan")}]])
def test_invalid_payload_is_not_sealed(invalid):
    raw = artifact()
    raw["synthesis"]["priority_actions"] = invalid
    saved = seal_for_save(raw)
    assert saved[KEY]["status"] == "unavailable"
    # seal_for_save's "unavailable" is a deliberate, documented benign
    # degrade (2026-09 review): the content that could not be digested at
    # seal time still cannot be digested at verify time, so this is
    # self-consistent, not tampering. Re-labeling it as "invalid" would make
    # every save of a genuinely non-serializable-but-legitimate row report
    # as corrupted.
    assert verify_manifest(saved) == "unavailable"


@pytest.mark.parametrize("invalid", [None, {}, [None]])
def test_unavailable_manifest_that_no_longer_matches_content_is_invalid(invalid):
    """If the content that made sealing "unavailable" later becomes
    representable again (e.g. an in-place mutator repaired it) without a
    fresh reseal, the stored "unavailable" claim no longer describes this
    artifact -- that mismatch must surface as "invalid", not be silently
    accepted as still self-consistent."""
    raw = artifact()
    raw["synthesis"]["priority_actions"] = invalid
    saved = seal_for_save(raw)
    assert saved[KEY]["status"] == "unavailable"
    saved["synthesis"]["priority_actions"] = [{"ticker": "SYNTH", "type": "buy", "quantity": 1}]
    assert verify_manifest(saved) == "invalid"


def test_actual_save_boundary_and_verifier(tmp_path, monkeypatch):
    import analyst.cache as cache
    import analysis_pipeline_observation
    from post_run_verify import check_candidate_output_manifest

    target = tmp_path / "ai_portfolio_analysis.json"
    monkeypatch.setattr(cache, "CACHE_PATH", target)
    monkeypatch.setattr(cache, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(cache, "process_lock", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(analysis_pipeline_observation, "publish_saved_analysis", lambda *a: None)
    raw = artifact()
    raw[KEY] = {"status": "model-provided"}
    # Legitimate upstream changes are included at the final write boundary.
    raw["synthesis"]["priority_actions"][0]["quantity"] = 3
    before = copy.deepcopy(raw)
    returned = cache.save_cache(raw)
    assert raw == before
    # save_cache's return value must carry the manifest that was actually
    # written to disk. run_analysis() rebinds its own result to this return
    # value; before this, the dict returned to every caller of
    # run_analysis() never contained candidate_output_manifest even though
    # the file on disk did (2026-09 review).
    assert returned[KEY] == json.loads(target.read_text())[KEY]
    assert verify_manifest(returned) == "verified"
    assert check_candidate_output_manifest(tmp_path) == []
    saved = json.loads(target.read_text())
    saved["synthesis"]["_filtered_actions"][0]["ticker"] = "REPLACED"
    target.write_text(json.dumps(saved))
    issues = check_candidate_output_manifest(tmp_path)
    assert [(i["code"], i["severity"]) for i in issues] == [
        ("candidate_output_manifest_invalid", "error")]


def test_manifest_is_not_an_authentication_mechanism():
    saved = seal_for_save(artifact())
    saved["synthesis"]["priority_actions"][0]["ticker"] = "REPLACED"
    # Re-sealing is a trusted-host operation. No claim of adversarial integrity.
    assert verify_manifest(seal_for_save(saved)) == "verified"


def test_seal_for_save_degrades_on_a_deepcopy_failure_instead_of_crashing(monkeypatch):
    """copy.deepcopy can raise exception types build_manifest's own except
    clause does not claim to catch (RecursionError, copy.Error, a custom
    __deepcopy__'s own exception). seal_for_save must still return a snapshot
    -- never mutating the caller's dict -- with an unavailable manifest,
    rather than letting an unrelated audit failure crash the entire save
    path this module's own docstring says it must never do (2026-09 review)."""
    import candidate_output_audit as coa

    def _boom(_value):
        raise RecursionError("synthetic deepcopy failure")

    original = artifact()
    before = json.loads(json.dumps(original))  # snapshot taken before patching copy.deepcopy
    monkeypatch.setattr(coa.copy, "deepcopy", _boom)

    saved = seal_for_save(original)

    assert original == before  # caller's dict is still never mutated in place
    assert saved is not original
    assert saved[KEY]["status"] == "unavailable"
    assert saved["synthesis"]["priority_actions"] == original["synthesis"]["priority_actions"]


def test_verify_manifest_rejects_non_dict_input_instead_of_raising():
    """Both current call sites happen to guard dict-ness before calling this,
    but verify_manifest itself must classify malformed input cleanly rather
    than raise, per its own contract of never letting a malformed artifact
    crash the audit (2026-09 review)."""
    assert verify_manifest(None) == "invalid"
    assert verify_manifest([1, 2, 3]) == "invalid"
    assert verify_manifest("not a dict") == "invalid"


def test_run_digest_uses_the_real_synthesis_level_analysis_id():
    """build_manifest's run_digest must bind to synthesis["analysis_id"] (the
    real run identity every production caller actually sets) rather than
    only the artifact's top level, which analyst.__init__'s result dict
    never populates -- otherwise run_digest only ever varies with as_of,
    silently defeating its documented purpose in production (2026-09 review)."""
    import candidate_output_audit as coa

    base = artifact()
    base["synthesis"]["analysis_id"] = "run-A"
    base.pop("analysis_id", None)  # no top-level id, as in production
    manifest_a = coa.build_manifest(base)

    other = artifact()
    other["synthesis"]["analysis_id"] = "run-B"
    other.pop("analysis_id", None)
    manifest_b = coa.build_manifest(other)

    assert manifest_a["run_digest"] != manifest_b["run_digest"]

    # A top-level analysis_id is still used as a fallback when the
    # synthesis-level one is absent (e.g. a legacy or hand-built artifact).
    fallback = artifact()
    fallback["synthesis"].pop("analysis_id", None)
    fallback["analysis_id"] = "top-level-run"
    manifest_fallback = coa.build_manifest(fallback)
    assert manifest_fallback["run_digest"] == coa._digest(
        {"analysis_id": "top-level-run", "as_of": fallback["as_of"]})

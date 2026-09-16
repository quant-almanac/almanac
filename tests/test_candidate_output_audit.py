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
    cache.save_cache(raw)
    assert raw == before
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

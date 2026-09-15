from copy import deepcopy
import hashlib
import json

import pytest

import analysis_pipeline_observation as obs
from analyst import cache
import utils


def payload():
    return {
        "as_of": "2026-09-14 06:30",
        "synthesis": {
            "analysis_id": "synthetic-run", "decision_snapshot_id": "synthetic-snapshot",
            "raw_priority_actions": [{"type": "short"}, {"type": "cover"}],
            "priority_actions": [{"type": "cover", "execution_readiness": "review"}],
            "policy_filtered_actions": [{"action": {"type": "short"}, "rule": "sample_rule"}],
            "_filtered_actions": [{"type": "short"}],
        },
        "short_selling_analysis": {"short_opportunities": [{"ticker": "SYNTH_A"}]},
    }


def encoded(data=None):
    return json.dumps(payload() if data is None else data).encode()


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "analysis.json")
    monkeypatch.setattr(cache, "HISTORY_PATH", tmp_path / "history.json")
    return cache.CACHE_PATH


def artifacts(path):
    return sorted((path.parent / obs.RELATIVE_DIRECTORY).glob("*.json"))


def test_report_never_turns_legacy_readiness_into_growth_permission():
    data = payload()
    data["synthesis"]["priority_actions"][0]["execution_readiness"] = "ready"
    report = obs.summarize_analysis_bytes(encoded(data))
    assert report["stages"]["priority_actions"]["reported_execution_readiness"] == {"ready": 1}
    assert report["growth_candidate_quality"] == "not_evaluated"
    assert report["growth_lifecycle_cost"] == "not_evaluated"
    assert report["growth_post_trade_risk"] == "not_evaluated"
    for key in ("execution_authorized", "policy_changed", "rollout_evidence_qualified",
                "run_completion_verified", "cross_stage_attrition_verified",
                "stage_counts_are_unique_candidates"):
        assert report[key] is False


def test_short_cover_and_saved_tier_candidates_are_distinct():
    report = obs.summarize_analysis_bytes(encoded())
    assert report["stages"]["raw_priority_actions"]["reported_action_types"] == {"cover": 1, "short": 1}
    assert report["stages"]["priority_actions"]["reported_action_types"] == {"cover": 1}
    assert report["saved_tier_outputs"]["short_selling_analysis"]["short_opportunities"]["row_count"] == 1
    assert report["saved_tier_outputs"]["short_selling_analysis"]["short_opportunities"]["reported_action_types"] == {"unknown": 1}


@pytest.mark.parametrize("value,status,count", [(None, "malformed", None),
    ({}, "malformed", None), ("bad", "malformed", None), ([], "reported", 0),
    ([None, {"type": ["short"]}], "malformed_rows", 2)])
def test_absent_malformed_and_zero_are_not_conflated(value, status, count):
    data = payload()
    data["synthesis"]["priority_actions"] = value
    summary = obs.summarize_analysis_bytes(encoded(data))["stages"]["priority_actions"]
    assert (summary["status"], summary["row_count"]) == (status, count)
    del data["synthesis"]["priority_actions"]
    assert obs.summarize_analysis_bytes(encoded(data))["stages"]["priority_actions"] == {"status": "missing", "row_count": None}


def test_empty_primary_tier_field_does_not_fall_back_or_double_count():
    data = payload()
    data["short_selling_analysis"] = {"short_opportunities": [], "priority_actions": [{"type": "short"}]}
    tier = obs.summarize_analysis_bytes(encoded(data))["saved_tier_outputs"]["short_selling_analysis"]
    assert tier["short_opportunities"]["row_count"] == 0
    assert tier["priority_actions"]["row_count"] == 1


def test_policy_wrappers_and_modifications_are_not_all_rejections():
    data = payload()
    reject = {"action": {"type": "short"}, "rule": "borrow_unavailable"}
    modified = {"action": {"type": "buy"}, "rule": "size_reduced"}
    data["synthesis"]["policy_filtered_actions"] = [reject, modified]
    data["synthesis"]["policy_decision"] = {"rejected": [reject], "modified": [modified]}
    data["synthesis"]["post_policy_priority_actions"] = [{"type": "buy"}]
    result = obs.summarize_analysis_bytes(encoded(data))
    assert result["policy_filtered_includes_modifications"] is True
    assert result["stages"]["policy_filtered_actions"]["reported_action_types"] == {"buy": 1, "short": 1}
    assert result["stages"]["policy_rejected"]["reported_action_types"] == {"short": 1}
    assert result["stages"]["policy_modified"]["reported_action_types"] == {"buy": 1}
    assert result["stages"]["policy_rejected"]["reported_reason_codes"] == {"borrow_unavailable": 1}
    assert result["stages"]["post_policy_priority_actions"]["row_count"] == 1


def test_no_values_accounts_tickers_or_free_text_are_copied():
    data = payload()
    data["synthesis"]["priority_actions"] = [{
        "ticker": "SYNTH_SECRET_TICKER", "account": "SYNTH_SECRET_ACCOUNT",
        "estimated_notional_jpy": 987654321, "type": "buy",
        "filtered_reason": "SYNTH_FREE_TEXT", "execution_block_reasons": [
            {"code": "cash_balance_insufficient"}, {"code": "cash_balance_insufficient"},
            {"code": "SYNTH_PRIVATE_TEXT"},
        ],
    }]
    result = obs.summarize_analysis_bytes(encoded(data))
    text = json.dumps(result)
    for forbidden in ("SYNTH_SECRET", "987654321", "SYNTH_FREE_TEXT", "SYNTH_PRIVATE_TEXT"):
        assert forbidden not in text
    assert result["stages"]["priority_actions"]["reported_reason_codes"]["cash_balance_insufficient"] == 1


@pytest.mark.parametrize("raw", [b"[]", b"null", b'{"x": NaN}', b'{"x": 1, "x": 2}', b"broken"])
def test_invalid_source_cannot_look_like_an_empty_analysis(raw):
    with pytest.raises(ValueError):
        obs.summarize_analysis_bytes(raw)


def test_real_cache_save_publishes_bound_sidecar_without_changing_analysis(paths):
    data = payload()
    before = deepcopy(data)
    cache.save_cache(data)
    assert data == before
    assert json.loads(paths.read_bytes()) == before
    report = obs.read_current_observation(paths)
    assert report["status"] == "reported"
    assert report["analysis_id"] == "synthetic-run"
    assert report["source_cache_sha256"] == hashlib.sha256(paths.read_bytes()).hexdigest()
    assert len(artifacts(paths)) == 1
    assert "analysis_observation" not in cache.load_history_context()
    assert "pipeline" not in cache.HISTORY_PATH.read_text()


def test_exact_retry_preserves_artifact_and_changed_bytes_get_new_generation(paths):
    cache.save_cache(payload())
    first = artifacts(paths)[0]
    original = first.read_bytes(), first.stat().st_mtime_ns
    assert obs.publish_saved_analysis(paths) == {"recorded": True, "duplicate": True}
    assert (first.read_bytes(), first.stat().st_mtime_ns) == original
    data = payload()
    data["synthesis"]["priority_actions"] = []
    cache.save_cache(data)
    assert len(artifacts(paths)) == 2
    assert first.read_bytes() == original[0]
    assert obs.read_current_observation(paths)["stages"]["priority_actions"]["row_count"] == 0


def test_observer_failure_does_not_stop_cache_or_history(paths, monkeypatch, capsys):
    def fail(_path):
        raise OSError("SYNTH_PRIVATE_PATH")
    monkeypatch.setattr(obs, "publish_saved_analysis", fail)
    cache.save_cache(payload())
    assert json.loads(paths.read_bytes()) == payload()
    assert len(json.loads(cache.HISTORY_PATH.read_bytes())["history"]) == 1
    assert "SYNTH_PRIVATE_PATH" not in capsys.readouterr().out
    assert obs.read_current_observation(paths)["status"] == "unavailable"


def test_actual_artifact_write_failure_preserves_formal_result(paths, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("disk failure")
    monkeypatch.setattr(utils, "atomic_write_json", fail)
    # cache has the original imported writer; only the observer uses this one.
    cache.save_cache(payload())
    assert json.loads(paths.read_bytes()) == payload()
    assert cache.HISTORY_PATH.exists()
    assert obs.read_current_observation(paths)["status"] == "unavailable"


def test_cache_write_failure_cannot_publish_observation(paths, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("cache failure")
    monkeypatch.setattr(cache, "atomic_write_json", fail)
    with pytest.raises(OSError):
        cache.save_cache(payload())
    assert not artifacts(paths)
    assert not cache.HISTORY_PATH.exists()


def test_history_failure_does_not_hide_a_committed_cache_observation(paths, monkeypatch):
    original = cache.atomic_write_json
    def fail_history(path, data):
        if path == cache.HISTORY_PATH:
            raise OSError("history failure")
        original(path, data)
    monkeypatch.setattr(cache, "atomic_write_json", fail_history)
    with pytest.raises(OSError):
        cache.save_cache(payload())
    assert obs.read_current_observation(paths)["status"] == "reported"
    assert obs.read_current_observation(paths)["run_completion_verified"] is False


def test_stale_or_corrupt_artifact_is_never_presented_as_current(paths):
    cache.save_cache(payload())
    file = artifacts(paths)[0]
    file.write_text('{}')
    assert obs.read_current_observation(paths)["status"] == "unavailable"
    with pytest.raises(ValueError, match="observation_conflict"):
        obs.publish_saved_analysis(paths)
    assert file.read_text() == '{}'
    paths.write_bytes(encoded({"synthesis": {"analysis_id": "other"}}))
    assert obs.read_current_observation(paths)["status"] == "unavailable"


def test_reader_does_not_create_missing_source_or_log_directory(tmp_path):
    path = tmp_path / "absent.json"
    assert obs.read_current_observation(path)["status"] == "unavailable"
    assert list(tmp_path.iterdir()) == []


def test_numeric_false_does_not_count_as_an_intact_permission_flag(paths):
    cache.save_cache(payload())
    file = artifacts(paths)[0]
    data = json.loads(file.read_bytes())
    data["execution_authorized"] = 0
    file.write_text(json.dumps(data))
    assert obs.read_current_observation(paths)["status"] == "unavailable"
    with pytest.raises(ValueError, match="observation_conflict"):
        obs.publish_saved_analysis(paths)


def test_changing_source_is_not_published(paths, monkeypatch):
    from pathlib import Path
    paths.write_bytes(encoded())
    original = Path.read_bytes
    calls = []
    def read(path):
        raw = original(path)
        if path == paths:
            calls.append(1)
            if len(calls) > 1:
                return raw + b" "
        return raw
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(ValueError, match="analysis_changed"):
        obs.publish_saved_analysis(paths)
    assert not artifacts(paths)


def test_backup_configuration_keeps_observer_artifacts_separate_from_financial_state():
    import backup_manager
    assert str(obs.RELATIVE_DIRECTORY) in backup_manager.EVIDENCE_DIRECTORIES
    assert str(obs.RELATIVE_DIRECTORY) not in backup_manager.TARGETS


def test_actual_cli_only_reads_and_refuses_a_missing_generation(paths):
    import os
    import subprocess
    import sys
    cache.save_cache(payload())
    before = paths.read_bytes(), artifacts(paths)[0].read_bytes()
    command = [sys.executable, obs.__file__, "--cache", str(paths)]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "reported"
    assert (paths.read_bytes(), artifacts(paths)[0].read_bytes()) == before
    missing = paths.parent / "missing.json"
    result = subprocess.run(command[:-1] + [str(missing)], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "unavailable"
    assert not missing.exists()

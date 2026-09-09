"""S3 uses synthetic inputs only, no provider/LLM calls or live state."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import json
from types import SimpleNamespace

import pytest

import earnings_blackout_observation as shadow


def state(**changes):
    args = dict(
        holdings=[{"ticker": "SYNTH_A", "asset_type": "stock"}],
        snapshot={"suggestions": [], "skipped": [{"ticker": "SYNTH_A", "earnings": "2026-09-17"}]},
        registry={}, non_earnings=set(), today=date(2026, 9, 9), evidence=["synthetic"],
    )
    args.update(changes)
    return shadow.build_state(**args)


def test_windows_share_frozen_dates_and_are_immutable():
    value = state()
    assert value.window(5)["cleared"] == frozenset({"SYNTH_A"})
    assert value.window(7)["blackout"] == frozenset({"SYNTH_A"})
    assert value.window(1)["cleared"] == frozenset({"SYNTH_A"})
    with pytest.raises(TypeError):
        value.events["SYNTH_A"] = date(2026, 9, 9)
    with pytest.raises(AttributeError):
        value.window(7)["blackout"].add("SYNTH_B")


def test_classification_precedence_and_coverage():
    holdings = [
        {"ticker": "SYNTH_A", "asset_type": "stock"},
        {"ticker": "SYNTH_B", "asset_type": None},
        {"ticker": "SYNTH_C", "asset_type": "stock"},
        {"ticker": "SYNTH_D", "asset_type": "stock"},
        {"ticker": "SYNTH_D", "asset_type": "etf"},
        {"ticker": "SYNTH_E", "asset_type": "investment_trust"},
        {"ticker": "SYNTH_F", "asset_type": None},
    ]
    value = state(holdings=holdings, registry={"SYNTH_F": {"asset_class": "etf"}})
    assert value.classifications == {
        "SYNTH_A": "dated", "SYNTH_B": "unknown", "SYNTH_C": "not_covered",
        "SYNTH_D": "unknown", "SYNTH_E": "non_earnings", "SYNTH_F": "non_earnings",
    }
    assert value.unknown_reasons == {"SYNTH_B": "classification_missing", "SYNTH_D": "classification_conflict"}


def test_missing_date_is_unknown_not_cleared():
    value = state(snapshot={"suggestions": [], "skipped": [{"ticker": "SYNTH_A", "earnings": "bad"}]})
    assert value.resolved
    assert value.window(7)["unknown"] == {"SYNTH_A"}
    assert not value.window(7)["cleared"]


def test_registry_fund_without_snapshot_coverage():
    from instrument_metadata import BROAD_EXECUTION_ALLOWLIST
    value = state(holdings=[{"ticker": "VT", "asset_type": None}],
                  snapshot={"suggestions": [], "skipped": []}, registry=BROAD_EXECUTION_ALLOWLIST)
    assert value.window(7)["non_earnings"] == {"VT"}


def test_hash_covers_full_classification_and_event_inputs():
    original = state().input_hash
    assert state(evidence=["changed override"]).input_hash != original
    assert state(holdings=[{"ticker": "SYNTH_A", "asset_type": "etf"}]).input_hash != original
    assert state(snapshot={"suggestions": [], "skipped": [{"ticker": "SYNTH_A", "earnings": "2026-09-10"}]}).input_hash != original


def manager(tmp_path):
    paths = [tmp_path / name for name in ("holdings.json", "snapshot.json", "overrides.json")]
    data = [{"positions": [{"ticker": "SYNTH_A", "asset_type": "stock"}]},
            {"suggestions": [], "skipped": [{"ticker": "SYNTH_A", "earnings": "2026-09-17"}]}, {}]
    for path, value in zip(paths, data):
        path.write_text(json.dumps(value))
    return SimpleNamespace(HOLDINGS=paths[0], OUTPUT=paths[1], EARNINGS_OVERRIDES=paths[2],
                           _read_and_validate_snapshot=lambda **kw: json.loads(paths[1].read_text()))


def test_freeze_once_then_replacement_cannot_change_queries(tmp_path):
    source = manager(tmp_path)
    calls = []
    read = source._read_and_validate_snapshot
    source._read_and_validate_snapshot = lambda **kw: (calls.append(1), read(**kw))[1]
    value = shadow.freeze_inputs(source, now=datetime(2026, 9, 9, tzinfo=shadow.JST))
    source.OUTPUT.write_text("broken")
    assert value.window(7)["blackout"] == {"SYNTH_A"}
    assert not value.window(5)["blackout"]
    assert calls == [1]


def test_freeze_rejects_change_during_validation(tmp_path):
    source = manager(tmp_path)
    read = source._read_and_validate_snapshot
    def racing(**kw):
        result = read(**kw)
        source.EARNINGS_OVERRIDES.write_text('{"changed": true}')
        return result
    source._read_and_validate_snapshot = racing
    with pytest.raises(ValueError, match="inputs_changed"):
        shadow.freeze_inputs(source)


def record(identifier="run-1", day="2026-09-09", resolved=True):
    observer = shadow.Observer()
    observer.state = state() if resolved else state(snapshot=None)
    return observer.record(identifier, now=datetime.fromisoformat(day).replace(tzinfo=shadow.JST))


def test_append_dedup_and_weekdays(tmp_path, monkeypatch):
    import utils
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    path = tmp_path / shadow.FILENAME
    assert shadow.append_observation(path, record())["distinct_valid_weekdays"] == 1
    assert shadow.append_observation(path, record())["duplicate"]
    assert shadow.append_observation(path, record("run-2"))["distinct_valid_weekdays"] == 1
    assert shadow.append_observation(path, record("run-3", "2026-09-12"))["distinct_valid_weekdays"] == 1
    result = shadow.append_observation(path, record("run-4", "2026-09-14", False))
    assert result["distinct_valid_weekdays"] == 1
    assert result["failed_input_runs"] == 1
    assert not result["automatic_promotion"]


def test_concurrent_same_id_is_one_row(tmp_path, monkeypatch):
    import utils
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    path = tmp_path / shadow.FILENAME
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: shadow.append_observation(path, record()), range(3)))
    assert len(path.read_text().splitlines()) == 1
    assert sum(not row["duplicate"] for row in results) == 1


def test_corrupt_history_is_reported_not_replaced(tmp_path):
    (tmp_path / shadow.FILENAME).write_text("broken\n")
    with shadow.observation_scope():
        shadow._CURRENT.get().state = state()
        result = shadow.finish_current(tmp_path, "run-1")
    assert result["recorded"] is False
    assert "JSONDecodeError" in result["error"]
    assert (tmp_path / shadow.FILENAME).read_text() == "broken\n"


def test_worker_context_and_actual_legacy_result_are_recorded(tmp_path):
    import analyst
    from llm_run_context import submit_with_current_context
    with shadow.observation_scope():
        observer = shadow._CURRENT.get()
        observer.state = state()
        with ThreadPoolExecutor(max_workers=1) as pool:
            submit_with_current_context(pool, shadow.capture, "prompt", 5, {"SYNTH_B"}).result()
        row = observer.record("run-1", ["SYNTH_Z"])
    assert row["consumer_reads"][0]["legacy_only"] == ["SYNTH_B"]
    assert row["out_of_universe_candidates"] == ["SYNTH_Z"]
    assert analyst._earnings_shadow is shadow
    assert shadow._CURRENT.get() is None


def test_prompt_is_identical_and_legacy_value_not_replaced(monkeypatch):
    import analyst
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda **kw: {"SYNTH_B"})
    before = analyst._format_earnings_blackout_for_prompt()
    with shadow.observation_scope():
        observer = shadow._CURRENT.get()
        observer.state = state(snapshot=None)
        after = analyst._format_earnings_blackout_for_prompt()
        row = observer.record("run-1")
    assert before == after
    assert row["consumer_reads"][0]["legacy"] == ["SYNTH_B"]
    assert row["resolved"] is False


def test_disk_failure_flows_to_final_heartbeat_with_telegram_failure(tmp_path, monkeypatch):
    import portfolio_analyst as cli
    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(shadow, "append_observation", fail)
    with shadow.observation_scope():
        shadow._CURRENT.get().state = state()
        diagnostic = shadow.finish_current(tmp_path, "run-1")
    calls = []
    monkeypatch.setattr(cli, "run_analysis", lambda **kw: {
        "synthesis": {"priority_actions": []}, "earnings_blackout_observation": diagnostic})
    monkeypatch.setattr(cli, "send_to_telegram", lambda result: False)
    monkeypatch.setattr(cli, "heartbeat", lambda *a, **kw: calls.append(a))
    assert cli.main(["--telegram"]) == 2
    assert len(calls) == 1
    assert calls[0][0:2] == ("portfolio_analyst", "warn")
    assert "Telegram" in calls[0][2] and "synthetic disk failure" in calls[0][2]


def test_backup_optional_and_no_automatic_promotion():
    import backup_manager
    assert shadow.FILENAME in backup_manager.TARGETS
    assert shadow.FILENAME not in backup_manager.REQUIRED_TARGETS
    rows = [record(str(day), f"2026-09-{day:02d}") for day in range(1, 25)]
    result = shadow.summarize(rows)
    assert result["review_ready"]
    assert result["automatic_promotion"] is False


def test_real_heartbeat_consumer_sees_observation_failure(tmp_path, monkeypatch):
    import portfolio_analyst as cli
    import utils
    import watchdog
    path = tmp_path / "heartbeats.json"
    monkeypatch.setattr(utils, "HEARTBEAT_PATH", path)
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(cli, "heartbeat", utils.heartbeat)
    monkeypatch.setattr(cli, "run_analysis", lambda **kw: {
        "synthesis": {}, "earnings_blackout_observation": {
            "recorded": False, "resolved": True, "error": "disk full"}})
    assert cli.main([]) == 0  # Successful analysis remains successful.
    entries = json.loads(path.read_text())
    assert set(entries) == {"portfolio_analyst"}  # Never masks the scheduled producer.
    monkeypatch.setattr(watchdog, "EXPECTED_INTERVALS", {
        "portfolio_analyst": watchdog.EXPECTED_INTERVALS["portfolio_analyst"]})
    monkeypatch.setattr(watchdog, "_is_weekend", lambda: False)
    monkeypatch.setattr(watchdog, "_is_monday_morning_grace", lambda: False)
    report = watchdog.evaluate_heartbeats(entries)
    assert report["errors"][0]["script"] == "portfolio_analyst"
    monkeypatch.setattr(watchdog, "evaluate_health", lambda: {
        **report, "fx_stale": False, "fx_age_hours": 0})
    monkeypatch.setattr(watchdog, "WATCHDOG_STATE", tmp_path / "watchdog.json")
    assert watchdog.run_check(notify=False) == 1


def test_cached_analysis_does_not_freeze_or_append(monkeypatch):
    import analyst
    sentinel = {"synthesis": {}, "as_of": "synthetic"}
    monkeypatch.setattr(analyst, "_validate_decision_refresh_configuration", lambda: None)
    monkeypatch.setattr(analyst, "is_cache_valid", lambda: True)
    monkeypatch.setattr(analyst, "get_cached", lambda: sentinel)
    monkeypatch.setattr(analyst, "write_progress", lambda *a: None)
    def forbidden(*a, **kw):
        pytest.fail("cache reuse must not create observations")
    monkeypatch.setattr(shadow, "freeze_current", forbidden)
    monkeypatch.setattr(shadow, "finish_current", forbidden)
    assert analyst.run_analysis() is sentinel


def test_input_errors_are_observed_without_changing_legacy_values(tmp_path):
    source = manager(tmp_path)
    source.HOLDINGS.write_text("bad")
    with shadow.observation_scope():
        shadow.freeze_current(source)
        shadow.capture("policy", 5, {"SYNTH_A"})
        result = shadow.finish_current(tmp_path, "bad-input")
    assert result["recorded"] is True
    assert result["resolved"] is False
    row = json.loads((tmp_path / shadow.FILENAME).read_text())
    assert row["consumer_reads"][0]["legacy"] == ["SYNTH_A"]


def test_pre_policy_candidate_survives_rejection_and_input_is_not_mutated(tmp_path):
    import copy
    actions = [{"ticker": "SYNTH_Z", "type": "buy", "amount_jpy": 12345}]
    original = copy.deepcopy(actions)
    with shadow.observation_scope():
        shadow._CURRENT.get().state = state()
        shadow.capture_candidates("pre_policy", actions)
        result = shadow.finish_synthesis(tmp_path, "run-1", {"priority_actions": []})
    assert actions == original
    assert result["recorded"] is True
    saved = json.loads((tmp_path / shadow.FILENAME).read_text())
    assert saved["out_of_universe_candidates"] == ["SYNTH_Z"]
    assert saved["candidate_stages"]["pre_policy"] == ["SYNTH_Z"]
    assert "12345" not in (tmp_path / shadow.FILENAME).read_text()


def test_malformed_candidate_output_cannot_abort_analysis(tmp_path):
    with shadow.observation_scope():
        shadow._CURRENT.get().state = state()
        result = shadow.finish_synthesis(tmp_path, "run-1", {"_filtered_actions": 42})
    assert result["recorded"] is True
    assert result["resolved"] is False
    assert "candidate_capture_failed" in result["error"]
    assert result["distinct_valid_weekdays"] == 0


def test_funnel_capture_is_wired_and_output_unchanged():
    import analyst
    inputs = {"screening": {"long_term": {"passed": [{"ticker": "SYNTH_Z"}]}}}
    now = datetime(2026, 9, 9, 7, tzinfo=shadow.JST)
    baseline = analyst._build_candidate_funnel({}, inputs, now=now, earnings_blackout_tickers=set())
    with shadow.observation_scope():
        observer = shadow._CURRENT.get()
        observer.state = state()
        result = analyst._build_candidate_funnel({}, inputs, now=now, earnings_blackout_tickers=set())
        row = observer.record("run-1")
    assert result == baseline
    assert row["candidate_stages"]["candidate_funnel"] == ["SYNTH_Z"]


def test_late_worker_cannot_modify_closed_observation():
    observer = shadow.Observer()
    observer.state = state()
    observer.capture_candidates("pre_policy", [{"ticker": "SYNTH_Z"}])
    first = observer.record("run-1")
    observer.capture_candidates("pre_policy", [{"ticker": "SYNTH_LATE"}])
    observer.capture("prompt", 5, {"SYNTH_LATE"})
    second = observer.record("run-1")
    assert first == second

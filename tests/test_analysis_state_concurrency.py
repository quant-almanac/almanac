import json
import threading
import time

import action_state_tracker as action_state
import analyst
from analyst import cache
import utils


def test_formal_analysis_decorator_is_single_flight(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    @analyst._with_analysis_singleflight
    def _formal_run() -> None:
        entered.set()
        release.wait(timeout=2)

    first = threading.Thread(target=_formal_run)
    first.start()
    assert entered.wait(timeout=1)
    try:
        _formal_run()
    except utils.LockBusy:
        outcomes.append("busy")
    finally:
        release.set()
        first.join(timeout=2)

    assert outcomes == ["busy"]


def test_run_analysis_applies_single_flight_before_any_work(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(
        analyst,
        "_validate_decision_refresh_configuration",
        lambda: (_ for _ in ()).throw(RuntimeError("analysis body was entered")),
    )

    with utils.process_lock(analyst.ANALYSIS_LOCK_NAME):
        try:
            analyst.run_analysis(force=True)
        except utils.LockBusy:
            pass
        else:
            raise AssertionError("run_analysis entered without acquiring its single-flight lock")


def test_cache_history_read_modify_write_is_serialized(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "analysis.json")
    monkeypatch.setattr(cache, "HISTORY_PATH", tmp_path / "history.json")
    active = 0
    max_active = 0
    guard = threading.Lock()
    original_load = cache.load_json

    def _slow_load(*args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        try:
            return original_load(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(cache, "load_json", _slow_load)
    rows = [
        {"as_of": "2026-08-31 06:00", "synthesis": {"overall_stance": "neutral"}},
        {"as_of": "2026-08-31 06:01", "synthesis": {"overall_stance": "defensive"}},
    ]
    threads = [threading.Thread(target=cache.save_cache, args=(row,)) for row in rows]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    history = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert max_active == 1
    assert {row["as_of"] for row in history["history"]} == {
        "2026-08-31 06:00", "2026-08-31 06:01",
    }


def test_cache_failure_never_publishes_a_new_history_generation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "analysis.json")
    monkeypatch.setattr(cache, "HISTORY_PATH", tmp_path / "history.json")
    original_write = cache.atomic_write_json

    def fail_cache(path, data):
        if path == cache.CACHE_PATH:
            raise OSError("disk full")
        return original_write(path, data)

    monkeypatch.setattr(cache, "atomic_write_json", fail_cache)

    try:
        cache.save_cache({
            "as_of": "2026-08-31 06:00",
            "synthesis": {"analysis_id": "new", "overall_stance": "neutral"},
        })
    except OSError:
        pass
    else:
        raise AssertionError("cache write failure was swallowed")

    assert not cache.HISTORY_PATH.exists()


def test_action_state_mutations_share_one_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    state_path = tmp_path / "action_state.json"
    monkeypatch.setattr(action_state, "STATE_FILE", state_path)
    state_path.write_text(json.dumps({
        "actions": {
            "a1": {
                "id": "a1", "ticker": "AAPL", "action_type": "buy",
                "status": "placed", "recommended_at": "2026-01-01T00:00:00",
                "placed_at": "2026-01-01T00:00:00",
            }
        },
        "last_updated": "",
    }), encoding="utf-8")
    active = 0
    max_active = 0
    guard = threading.Lock()
    original_load = action_state._load

    def _slow_load():
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        try:
            return original_load()
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(action_state, "_load", _slow_load)
    threads = [
        threading.Thread(target=action_state.update_status, args=("a1", "filled")),
        threading.Thread(target=action_state.expire_stale_placed_actions, kwargs={"max_days": 1}),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    saved = json.loads(state_path.read_text(encoding="utf-8"))["actions"]["a1"]
    assert max_active == 1
    assert saved["status"] == "filled"

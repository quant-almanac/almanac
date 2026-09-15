import sys
from types import SimpleNamespace

import watchdog as wd


def report(issue=True):
    return {"stale": [], "errors": [], "fx_stale": False, "ok": [],
            "nav_recording_issues": [{"check": "nav_publication", "reason": "nav_table_empty"}] if issue else []}


def test_notification_filter_counter_fingerprint_and_message():
    view = wd._notification_report(report())
    assert wd._notification_problem_count(view) == 1
    assert wd._notification_fingerprint(view) != wd._notification_fingerprint(wd._notification_report(report(False)))
    assert "nav_table_empty" in wd._build_watchdog_message(view)


def test_run_check_counts_debounces_and_cools_down(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "WATCHDOG_STATE", tmp_path / "state.json")
    monkeypatch.setattr(wd, "evaluate_health", report)
    sent = []
    monkeypatch.setitem(sys.modules, "alert", SimpleNamespace(send_telegram=lambda msg: sent.append(msg) or True))
    for _ in range(2):
        assert wd.run_check(notify=True) == 1
    assert sent == []
    assert wd.run_check(notify=True) == 1
    assert len(sent) == 1
    wd.run_check(notify=True)
    assert len(sent) == 1
    monkeypatch.setattr(wd, "evaluate_health", lambda: report(False))
    assert wd.run_check(notify=True) == 0


def test_observer_exception_is_isolated(monkeypatch):
    monkeypatch.setattr(wd, "resolve_db_path", lambda *_: (_ for _ in ()).throw(OSError("private path")))
    got = wd._nav_recording_observation(now=1)
    assert got["reason"] == "nav_observer_failed"
    assert "private path" not in str(got)


def test_evaluate_health_wires_both_axes(monkeypatch):
    for name in dir(wd):
        if name.startswith("_check_"):
            monkeypatch.setattr(wd, name, lambda *a, **kw: [])
    monkeypatch.setattr(wd, "load_json", lambda *a, **kw: {})
    monkeypatch.setattr(wd, "evaluate_heartbeats", lambda *_: {"stale": [], "errors": [], "ok": []})
    monkeypatch.setattr(wd, "_fx_staleness", lambda *a, **kw: (False, None))
    sentinel = {"publication_status": "reported", "dd_evidence_status": "unknown", "reason": "nav_publication_reported"}
    monkeypatch.setattr(wd, "_nav_recording_observation", lambda **_: sentinel)
    got = wd.evaluate_health()
    assert got["nav_recording"] == sentinel
    assert got["nav_recording_issues"] == []
    sentinel["publication_status"] = "unknown"
    assert len(wd.evaluate_health()["nav_recording_issues"]) == 1


def test_print_status_shows_unknown_dd_separately(monkeypatch, capsys):
    monkeypatch.setattr(wd, "evaluate_health", lambda: {**report(False), "fx_age_hours": None,
                         "nav_recording": {"publication_status": "reported", "dd_evidence_status": "unknown"}})
    wd.print_status()
    assert "NAV publication: reported / DD evidence: unknown" in capsys.readouterr().out

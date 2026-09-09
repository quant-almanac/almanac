"""watchdog が heartbeat() のロック競合による書込抑止を検知する (2026-09 レビュー S0-d)。

utils.heartbeat() はロックが取れないとき共有 heartbeats.json を書かず、代わりに
heartbeat_lock_failures.jsonl へ追記する。これは「更新消失を再導入しない」ための
選択であり、その代償として watchdog 側がこのファイルを能動的に拾わない限り、
抑止された heartbeat は誰にも気づかれない新しい静かな失敗経路になる。
"""
from __future__ import annotations

import json
import time

import pytest

import watchdog as wd


@pytest.fixture
def isolated_base(tmp_path, monkeypatch):
    """watchdog.py 側は BASE_DIR 相対でファイルを解決する既存流儀に合わせる
    （utils.HEARTBEAT_LOCK_FAILURES_PATH は import 時に一度だけ束縛されるため、
    watchdog.py からの参照は BASE_DIR 経由にする ―― ACCOUNT_JSON と同じ理由で
    utils 側だけ monkeypatch しても watchdog 側には効かない）。"""
    monkeypatch.setattr(wd, "BASE_DIR", tmp_path)
    return tmp_path


def _write_row(path, *, script, ts, reason="heartbeat_lock_busy", status="ok"):
    row = {
        "script": script,
        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)),
        "attempted_ts": ts,
        "status": status,
        "reason": reason,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_absent_fallback_file_reports_no_issues(isolated_base):
    assert wd._check_heartbeat_lock_failures(now=time.time()) == []


def test_recent_lock_failure_is_reported(isolated_base):
    path = isolated_base / "heartbeat_lock_failures.jsonl"
    now = time.time()
    _write_row(path, script="earnings_proximity", ts=now - 60)

    issues = wd._check_heartbeat_lock_failures(now=now)
    assert len(issues) == 1
    assert issues[0]["script"] == "earnings_proximity"


def test_old_lock_failure_outside_window_is_excluded(isolated_base):
    path = isolated_base / "heartbeat_lock_failures.jsonl"
    now = time.time()
    _write_row(path, script="earnings_proximity", ts=now - 40 * 3600)

    issues = wd._check_heartbeat_lock_failures(now=now, window_sec=26 * 3600)
    assert issues == []


def test_malformed_lines_are_skipped_without_crashing(isolated_base):
    path = isolated_base / "heartbeat_lock_failures.jsonl"
    now = time.time()
    with open(path, "a", encoding="utf-8") as f:
        f.write("not json at all\n")
        f.write("\n")
    _write_row(path, script="behavioral_guard_snapshot", ts=now - 10)

    issues = wd._check_heartbeat_lock_failures(now=now)
    assert len(issues) == 1
    assert issues[0]["script"] == "behavioral_guard_snapshot"


def _empty_report(**overrides) -> dict:
    """evaluate_health() が返す report の最小形。他テストと同じ手組み流儀
    (test_watchdog_extended.py) に合わせ、evaluate_health() 自体は呼ばない
    ―― その関数は ACCOUNT_JSON 等インポート時に一度だけ束縛された定数へ
    触るチェックを含み、monkeypatch.setattr(wd, "BASE_DIR", ...) だけでは
    隔離できない。"""
    base = {
        "stale": [], "errors": [], "fx_stale": False, "fx_age_hours": None,
        "schema_issues": [], "llm_issues": [], "integrity_issues": [],
        "measurement_stale": [], "outcome_log_issues": [], "disclosure_freshness": [],
        "shadow_book_issues": [], "disk_space_issues": [], "backup_issues": [],
        "heartbeat_lock_issues": [],
    }
    base.update(overrides)
    return base


_OTHER_CHECKS_TO_STUB = [
    "_check_critical_json", "_check_old_parquet", "_check_price_sanity",
    "_check_screener_outputs", "_check_short_universe_staleness", "_check_llm_output",
    "_check_portfolio_integrity", "_check_measurement_tables", "_check_outcome_logs",
    "_check_disclosure_freshness", "_check_shadow_book", "_check_disk_space",
    "_check_lane_registry",
]


def test_evaluate_health_includes_heartbeat_lock_issues_in_its_report(isolated_base, monkeypatch):
    """evaluate_health() 自身がこのチェックを呼び出し、結果を返す report へ含める。

    evaluate_health() の他チェックは import 時に一度だけ束縛された定数
    (ACCOUNT_JSON 等) に触れるため BASE_DIR の monkeypatch だけでは隔離できない
    ―― ここでは heartbeat_lock_issues の配線だけを実挙動で検証するため、
    他の全チェックを no-op へ差し替える。
    """
    for name in _OTHER_CHECKS_TO_STUB:
        monkeypatch.setattr(wd, name, lambda *a, **k: [])
    monkeypatch.setattr(wd, "_check_backup_offsite", lambda *a, **k: [])
    monkeypatch.setattr(wd, "_fx_staleness", lambda *a, **k: (False, 1.0))
    monkeypatch.setattr(wd, "load_json", lambda *a, **k: {})
    monkeypatch.setattr(wd, "evaluate_heartbeats", lambda *a, **k: {"stale": [], "errors": [], "ok": []})

    sentinel = [{"script": "earnings_proximity", "attempted_at": None, "reason": "heartbeat_lock_busy"}]
    monkeypatch.setattr(wd, "_check_heartbeat_lock_failures", lambda *a, **k: sentinel)

    report = wd.evaluate_health()
    assert report.get("heartbeat_lock_issues") == sentinel


def test_lock_failures_are_unconditionally_notify_worthy(isolated_base):
    """suppressed heartbeat は NOTIFY_STALE_SCRIPTS のような別途の許可リストを要求しない。

    stale と違い、これはスクリプト単位の周期判断ではなく監視の書込機構そのものの
    欠損なので、他の 'errors' と同じ扱い（無条件通知対象）にする。
    """
    issues = [{"script": "a_script_nobody_added_to_notify_stale_scripts",
               "attempted_at": None, "reason": "heartbeat_lock_busy"}]
    notify = wd._notification_report(_empty_report(heartbeat_lock_issues=issues))
    assert len(notify["heartbeat_lock_issues"]) == 1


def test_lock_failures_count_toward_notification_problem_count(isolated_base):
    issues = [{"script": "earnings_proximity", "attempted_at": None, "reason": "heartbeat_lock_busy"}]
    notify = wd._notification_report(_empty_report(heartbeat_lock_issues=issues))
    assert wd._notification_problem_count(notify) >= 1


def test_lock_failures_appear_in_the_built_message(isolated_base):
    issues = [{"script": "earnings_proximity", "attempted_at": None, "reason": "heartbeat_lock_busy"}]
    notify = wd._notification_report(_empty_report(heartbeat_lock_issues=issues))
    message = wd._build_watchdog_message(notify)
    assert "earnings_proximity" in message


def test_lock_failures_change_the_notification_fingerprint(isolated_base):
    """同一問題が続く限り 24h クールダウンで 1 通、内容が変われば別 fingerprint。"""
    empty_fp = wd._notification_fingerprint(_empty_report())
    with_issue_fp = wd._notification_fingerprint(_empty_report(heartbeat_lock_issues=[
        {"script": "earnings_proximity", "attempted_at": None, "reason": "heartbeat_lock_busy"},
    ]))
    assert empty_fp != with_issue_fp

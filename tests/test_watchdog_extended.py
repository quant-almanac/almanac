"""
tests/test_watchdog_extended.py — P2-25: watchdog 拡張部分のみテスト

新規 _check_* 関数を BASE_DIR を一時ディレクトリに差し替えて評価する。
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import watchdog as wd


@pytest.fixture
def isolated_base(tmp_path, monkeypatch):
    """watchdog の BASE_DIR を tmp_path に差し替える。"""
    monkeypatch.setattr(wd, "BASE_DIR", tmp_path)
    # ohlcv ディレクトリ作成
    (tmp_path / "data" / "ohlcv").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ────────────────────────────────────────────────────────
# _fx_staleness
# ────────────────────────────────────────────────────────

def test_fx_staleness_absent_as_of_is_treated_as_stale():
    """fx_rate_usdjpy_as_of が一度も記録されていない account は stale
    (fresh ではない) 扱いにする。「確認できていない」のを「新鮮」と誤認
    する fail-open を防ぐ (レビューで指摘): account_stale 経由の USD 入出金
    は意図的にこのフィールドを更新しないため、一度も live/cache 取得の
    ない口座だと永久に欠落したままになり得る。
    """
    stale, age_hours = wd._fx_staleness({}, now=time.time())
    assert stale is True
    assert age_hours is None


def test_fx_staleness_recent_as_of_is_fresh():
    now = time.time()
    stale, age_hours = wd._fx_staleness({"fx_rate_usdjpy_as_of": now - 3600}, now=now)
    assert stale is False
    assert age_hours == 1.0


def test_fx_staleness_old_as_of_is_stale():
    now = time.time()
    four_days_ago = now - 4 * 24 * 3600
    stale, age_hours = wd._fx_staleness({"fx_rate_usdjpy_as_of": four_days_ago}, now=now)
    assert stale is True
    assert age_hours == pytest.approx(96.0, abs=0.1)


def test_fx_staleness_future_as_of_beyond_tolerance_is_stale():
    """時計ずれ・書き込みバグで未来の as_of が書かれると、経過時間が負に
    なり `fx_age_sec > FX_STALE_SEC` が常に False で fresh 扱いになって
    いた (レビューで指摘・実測: 7日未来の as_of が (False, -168.0) を
    返していた)。許容幅 (FX_FUTURE_TOLERANCE_HOURS) を超える未来値は
    stale 側へ倒す。
    """
    now = time.time()
    seven_days_future = now + 7 * 24 * 3600
    stale, age_hours = wd._fx_staleness({"fx_rate_usdjpy_as_of": seven_days_future}, now=now)
    assert stale is True
    assert age_hours == -168.0


def test_fx_staleness_future_as_of_within_tolerance_is_still_fresh():
    """通常の時計ずれ (許容幅以内) までは fresh のまま — 誤検知しない。"""
    now = time.time()
    thirty_min_future = now + 1800
    stale, age_hours = wd._fx_staleness({"fx_rate_usdjpy_as_of": thirty_min_future}, now=now)
    assert stale is False
    assert age_hours == -0.5


def test_fx_staleness_non_numeric_as_of_does_not_crash():
    """float() できない値で evaluate_health() ごとクラッシュしていた
    (レビューで指摘・実測: ValueError が伝播し、heartbeat/schema/parquet
    など他の全チェックまで巻き込んで止まっていた)。stale=True, age=None
    へ fail-closed し、例外は投げない。
    """
    stale, age_hours = wd._fx_staleness({"fx_rate_usdjpy_as_of": "not_a_number"}, now=time.time())
    assert stale is True
    assert age_hours is None


def test_fx_staleness_nan_as_of_is_treated_as_stale():
    """NaN との比較は常に False になるため、素朴な比較では fresh 扱いに
    なっていた (レビューで指摘・実測: (False, nan) を返していた)。
    """
    stale, age_hours = wd._fx_staleness({"fx_rate_usdjpy_as_of": float("nan")}, now=time.time())
    assert stale is True
    assert age_hours is None


def test_fx_staleness_infinite_as_of_is_treated_as_stale():
    stale, age_hours = wd._fx_staleness({"fx_rate_usdjpy_as_of": float("inf")}, now=time.time())
    assert stale is True
    assert age_hours is None


# ────────────────────────────────────────────────────────
# _check_critical_json
# ────────────────────────────────────────────────────────

def test_critical_json_missing_required_field(isolated_base):
    """account.json に required 'balance' が欠落 → issue として返る。"""
    (isolated_base / "account.json").write_text('{"usd_balance": 100}')
    (isolated_base / "holdings.json").write_text('{"AAPL": {"shares": 10}}')
    issues = wd._check_critical_json()
    assert any(
        i["file"] == "account.json" and "balance" in i["issue"]
        for i in issues
    )


def test_critical_json_corrupted(isolated_base):
    """holdings.json が破損 → json_parse_error。"""
    (isolated_base / "holdings.json").write_text('{not valid json')
    (isolated_base / "account.json").write_text('{"balance": 0, "usd_balance": 0}')
    issues = wd._check_critical_json()
    assert any(
        i["file"] == "holdings.json" and "json_parse_error" in i["issue"]
        for i in issues
    )


def test_critical_json_dict_of_records_too_few(isolated_base):
    """holdings.json が空 dict → 0 records で issue。"""
    (isolated_base / "holdings.json").write_text('{}')
    (isolated_base / "account.json").write_text('{"balance": 0, "usd_balance": 0}')
    issues = wd._check_critical_json()
    assert any(
        i["file"] == "holdings.json" and "too few records" in i["issue"]
        for i in issues
    )


def test_critical_json_missing_required_file(isolated_base):
    """holdings.json も account.json もなければ両方 missing。"""
    issues = wd._check_critical_json()
    files = {i["file"] for i in issues}
    assert "holdings.json" in files
    assert "account.json" in files


def test_critical_json_optional_file_missing_is_ok(isolated_base):
    """nisa_portfolio.json は optional なので missing でも issue 化しない。"""
    (isolated_base / "holdings.json").write_text('{"X": {}}')
    (isolated_base / "account.json").write_text('{"balance": 0, "usd_balance": 0}')
    issues = wd._check_critical_json()
    assert not any(i["file"] == "nisa_portfolio.json" for i in issues)


def test_critical_json_wrapped_list_valid(isolated_base):
    """cash_transactions.json が正しい構造ならパス。"""
    (isolated_base / "holdings.json").write_text('{"X": {}}')
    (isolated_base / "account.json").write_text('{"balance": 0, "usd_balance": 0}')
    (isolated_base / "cash_transactions.json").write_text('{"transactions": []}')
    issues = wd._check_critical_json()
    assert not any(i["file"] == "cash_transactions.json" for i in issues)


# ────────────────────────────────────────────────────────
# _check_old_parquet
# ────────────────────────────────────────────────────────

def test_old_parquet_detected(isolated_base, monkeypatch):
    """parquet の mtime を 10 日前に偽装 → stale として検出。"""
    p = isolated_base / "data" / "ohlcv" / "AAPL.parquet"
    p.write_text("dummy")
    old = time.time() - 10 * 24 * 3600
    import os
    os.utime(p, (old, old))
    stale = wd._check_old_parquet()
    assert len(stale) == 1
    assert "AAPL.parquet" in stale[0]["file"]


def test_recent_parquet_not_stale(isolated_base):
    """最新 parquet は stale 判定されない。"""
    p = isolated_base / "data" / "ohlcv" / "FRESH.parquet"
    p.write_text("dummy")
    stale = wd._check_old_parquet()
    assert stale == []


# ────────────────────────────────────────────────────────
# _check_screener_outputs
# ────────────────────────────────────────────────────────

def _write_all_screener_outputs(base: Path, payload: dict | None = None) -> None:
    payload = payload or {"ticker": "X"}
    for fname, cfg in wd.SCREENER_FILES.items():
        key = cfg["key"]
        (base / fname).write_text(json.dumps({key: [payload]}))


def test_screener_missing_file(isolated_base):
    issues = wd._check_screener_outputs()
    files = {i["file"] for i in issues}
    assert "long_term_screen_results.json" in files
    assert "margin_long_candidates_morning.json" in files
    assert "screen_results_jp.json" in files


def test_screener_empty_candidates(isolated_base):
    for fname, cfg in wd.SCREENER_FILES.items():
        (isolated_base / fname).write_text(json.dumps({cfg["key"]: []}))
    issues = wd._check_screener_outputs()
    assert {i["file"] for i in issues} == set(wd.SCREENER_FILES)
    assert all("empty" in i["issue"] for i in issues)


def test_screener_with_results_ok(isolated_base):
    _write_all_screener_outputs(isolated_base)
    issues = wd._check_screener_outputs()
    assert issues == []


def test_screener_monitor_includes_ai_consumed_morning_and_jp_files():
    assert "screen_results_morning.json" in wd.SCREENER_FILES
    assert "screen_results_jp.json" in wd.SCREENER_FILES
    assert "margin_long_candidates_morning.json" in wd.SCREENER_FILES
    assert "short_candidates_morning.json" in wd.SCREENER_FILES


# ────────────────────────────────────────────────────────
# _check_short_universe_staleness
# ────────────────────────────────────────────────────────

def test_short_universe_stale_entry_detected(isolated_base):
    """short_universe.json 内の古い評価行をticker単位で検知する。"""
    old = (datetime.now() - timedelta(days=8)).isoformat()
    fresh = datetime.now().isoformat()
    path = isolated_base / "data" / "short_universe.json"
    path.write_text(json.dumps({
        "as_of": fresh,
        "tickers": {
            "OLD": {"ticker": "OLD", "shortable": True, "last_evaluated_at": old},
            "FRESH": {"ticker": "FRESH", "shortable": True, "last_evaluated_at": fresh},
        },
    }))

    issues = wd._check_short_universe_staleness()

    assert len(issues) == 1
    assert issues[0]["file"] == "data/short_universe.json"
    assert issues[0]["ticker"] == "OLD"
    assert "stale_entry" in issues[0]["issue"]


def test_short_universe_fresh_entries_ok(isolated_base):
    """全tickerのlast_evaluated_atが新しければ問題なし。"""
    fresh = datetime.now().isoformat()
    path = isolated_base / "data" / "short_universe.json"
    path.write_text(json.dumps({
        "as_of": fresh,
        "tickers": {
            "AAPL": {"ticker": "AAPL", "shortable": True, "last_evaluated_at": fresh},
        },
    }))

    assert wd._check_short_universe_staleness() == []


def test_short_universe_missing_timestamp_is_flagged(isolated_base):
    """古いmerge由来などでtimestampが無い行は鮮度不明として警告する。"""
    path = isolated_base / "data" / "short_universe.json"
    path.write_text(json.dumps({
        "as_of": datetime.now().isoformat(),
        "tickers": {
            "NO_TS": {"ticker": "NO_TS", "shortable": True},
        },
    }))

    issues = wd._check_short_universe_staleness()

    assert len(issues) == 1
    assert issues[0]["ticker"] == "NO_TS"
    assert issues[0]["issue"] == "missing_last_evaluated_at"


# ────────────────────────────────────────────────────────
# _check_llm_output
# ────────────────────────────────────────────────────────

def test_llm_output_valid_with_empty_actions(isolated_base):
    """priority_actions=[] (no-trade) は valid。"""
    (isolated_base / "ai_portfolio_analysis.json").write_text(json.dumps({
        "synthesis": {"priority_actions": [], "headline": "no-trade day"}
    }))
    issues = wd._check_llm_output()
    # error は無い (no-trade は valid)
    assert not any("priority_actions" in i["issue"] for i in issues)


def test_llm_output_synthesis_missing(isolated_base):
    (isolated_base / "ai_portfolio_analysis.json").write_text('{}')
    issues = wd._check_llm_output()
    assert any("synthesis" in i["issue"] for i in issues)


def test_llm_output_error_field(isolated_base):
    (isolated_base / "ai_portfolio_analysis.json").write_text(
        '{"error": "Sonnet API timeout"}'
    )
    issues = wd._check_llm_output()
    assert any("error" in i["issue"] for i in issues)


def test_llm_output_priority_actions_wrong_type(isolated_base):
    (isolated_base / "ai_portfolio_analysis.json").write_text(json.dumps({
        "synthesis": {"priority_actions": "not a list"}
    }))
    issues = wd._check_llm_output()
    assert any("wrong type" in i["issue"] for i in issues)


# ────────────────────────────────────────────────────────
# Telegram notification filtering
# ────────────────────────────────────────────────────────

def test_notification_report_suppresses_advisory_noise():
    old_saved_at = (datetime.now() - timedelta(days=20)).isoformat()
    report = {
        "stale": [{"script": "daily_briefing", "age_hours": 400, "reason": "older_than_26h"}],
        "errors": [],
        "fx_stale": False,
        "fx_age_hours": 1,
        "schema_issues": [],
        "parquet_stale": [{"file": "data/ohlcv/AAPL.parquet"}],
        "screener_issues": [{"file": "short_candidates.json", "issue": "missing"}],
        "llm_issues": [],
        "backup_issues": [],
        "integrity_issues": [
            {
                "severity": "critical",
                "check": "execution_portfolio_not_applied",
                "message": "old execution",
                "saved_at": old_saved_at,
            }
        ],
    }

    notify_report = wd._notification_report(report)

    assert wd._notification_problem_count(notify_report) == 0


def test_backup_offsite_skipped_is_watchdog_issue(monkeypatch):
    hb = {
        "backup_manager": {
            "last_run_iso": "2026-06-27T01:00:02+0900",
            "status": "ok",
            "extra": {
                "offsite_status": "skipped",
                "offsite_reason": "rclone_not_installed",
            },
        }
    }

    issues = wd._check_backup_offsite(hb)

    assert issues == [{
        "severity": "warning",
        "check": "backup_offsite",
        "status": "skipped",
        "reason": "rclone_not_installed",
        "last_run_iso": "2026-06-27T01:00:02+0900",
        "message": "offsite backup did not complete",
    }]


def test_backup_offsite_copied_is_ok():
    hb = {
        "backup_manager": {
            "status": "ok",
            "extra": {"offsite_status": "copied"},
        }
    }

    assert wd._check_backup_offsite(hb) == []


def test_backup_offsite_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("ALMANAC_REQUIRE_OFFSITE_BACKUP", "0")
    hb = {
        "backup_manager": {
            "status": "ok",
            "extra": {"offsite_status": "skipped", "offsite_reason": "rclone_not_installed"},
        }
    }

    assert wd._check_backup_offsite(hb) == []


def test_notification_report_keeps_current_critical_issues():
    recent_saved_at = (datetime.now() - timedelta(hours=2)).isoformat()
    report = {
        "stale": [{"script": "data_fetcher", "age_hours": 30, "reason": "older_than_26h"}],
        "errors": [],
        "fx_stale": False,
        "fx_age_hours": 1,
        "schema_issues": [],
        "llm_issues": [],
        "backup_issues": [{"severity": "warning", "status": "skipped", "reason": "rclone_not_installed"}],
        "integrity_issues": [
            {"severity": "critical", "check": "cash_mirror", "message": "cash mismatch"},
            {
                "severity": "critical",
                "check": "execution_portfolio_not_applied",
                "message": "new execution",
                "saved_at": recent_saved_at,
            },
        ],
    }

    notify_report = wd._notification_report(report)

    assert wd._notification_problem_count(notify_report) == 3
    assert len(notify_report["integrity_issues"]) == 2
    assert notify_report["backup_issues"] == []


def test_notification_report_keeps_critical_backup_issues():
    report = {
        "stale": [],
        "errors": [],
        "fx_stale": False,
        "fx_age_hours": 1,
        "schema_issues": [],
        "llm_issues": [],
        "backup_issues": [
            {"severity": "warning", "status": "skipped", "reason": "rclone_not_installed"},
            {"severity": "critical", "status": "error", "reason": "copy_failed"},
        ],
        "integrity_issues": [],
    }

    notify_report = wd._notification_report(report)

    assert notify_report["backup_issues"] == [
        {"severity": "critical", "status": "error", "reason": "copy_failed"}
    ]
    assert wd._notification_problem_count(notify_report) == 1


def test_run_check_returns_zero_for_backup_warning_only(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "WATCHDOG_STATE", tmp_path / "watchdog_state.json")
    monkeypatch.setattr(
        wd,
        "evaluate_health",
        lambda: {
            "ok": [],
            "stale": [],
            "errors": [],
            "fx_stale": False,
            "fx_age_hours": 1,
            "schema_issues": [],
            "parquet_stale": [],
            "price_sanity_issues": [],
            "screener_issues": [],
            "short_universe_issues": [],
            "llm_issues": [],
            "integrity_issues": [],
            "measurement_stale": [],
            "outcome_log_issues": [],
            "disclosure_freshness": [],
            "shadow_book_issues": [],
            "disk_space_issues": [],
            "backup_issues": [
                {"severity": "warning", "status": "skipped", "reason": "rclone_not_installed"}
            ],
            "lane_registry_issues": [],
        },
    )

    assert wd.run_check(notify=True) == 0


def _notification_test_report() -> dict:
    return {
        "ok": [],
        "stale": [],
        "errors": [{"script": "ai_analysis", "error": "failed"}],
        "fx_stale": False,
        "fx_age_hours": 1,
        "schema_issues": [],
        "parquet_stale": [],
        "price_sanity_issues": [],
        "screener_issues": [],
        "short_universe_issues": [],
        "llm_issues": [],
        "integrity_issues": [],
        "measurement_stale": [],
        "outcome_log_issues": [],
        "disclosure_freshness": [],
        "shadow_book_issues": [],
        "disk_space_issues": [],
        "backup_issues": [],
        "lane_registry_issues": [],
    }


@pytest.mark.parametrize(
    ("send_result", "expected_status", "has_failure_reason"),
    [(True, "sent", False), (False, "failed", True)],
)
def test_run_check_records_telegram_delivery_outcome(
    monkeypatch, tmp_path, send_result, expected_status, has_failure_reason,
):
    import alert

    state_path = tmp_path / "watchdog_state.json"
    state_path.write_text(json.dumps({
        "consecutive_failures": 2,
        "consecutive_notify_failures": 2,
        "last_notified": 0,
    }))
    monkeypatch.setattr(wd, "WATCHDOG_STATE", state_path)
    monkeypatch.setattr(wd, "evaluate_health", _notification_test_report)
    monkeypatch.setattr(alert, "send_telegram", lambda _message: send_result)

    assert wd.run_check(notify=True) == 1
    state = json.loads(state_path.read_text())
    assert state["last_notification_status"] == expected_status
    assert bool(state.get("last_notification_failure_reason")) is has_failure_reason
    if send_result:
        assert state.get("last_notified", 0) > 0
        assert state.get("last_notification_fingerprint")
    else:
        assert state.get("last_notified", 0) == 0
        assert not state.get("last_notification_fingerprint")


def test_run_check_records_notification_skip_reason(monkeypatch, tmp_path):
    state_path = tmp_path / "watchdog_state.json"
    monkeypatch.setattr(wd, "WATCHDOG_STATE", state_path)
    monkeypatch.setattr(wd, "evaluate_health", _notification_test_report)

    assert wd.run_check(notify=False) == 1
    state = json.loads(state_path.read_text())
    assert state["last_notification_status"] == "skipped"
    assert state["last_notification_failure_reason"] == "notify_disabled"


def test_notification_report_keeps_disk_warning_and_critical():
    """warning も通知対象。critical だけだと 15GB 警告が誰にも届かない。"""
    report = _notification_test_report()
    report["errors"] = []
    report["disk_space_issues"] = [
        {"severity": "warning", "free_gb": 12.7, "issue": "disk_low"},
        {"severity": "critical", "free_gb": 7.9, "issue": "disk_critical"},
    ]

    notify_report = wd._notification_report(report)

    assert notify_report["disk_space_issues"] == [
        {"severity": "warning", "free_gb": 12.7, "issue": "disk_low"},
        {"severity": "critical", "free_gb": 7.9, "issue": "disk_critical"},
    ]


def test_disk_fingerprint_ignores_fluctuating_free_gb():
    """残量の小数変動で fingerprint が変わると 24h cooldown が無効化される。

    watchdog は 30 分毎に走るため、free_gb を含めていた時期は逼迫中ずっと
    最大 48 通/日の通知が飛ぶ状態だった。
    """
    def _with_disk(free_gb, severity="warning"):
        report = _notification_test_report()
        report["errors"] = []
        report["disk_space_issues"] = [
            {"path": "/srv/almanac", "severity": severity,
             "free_gb": free_gb, "issue": "free space below 15GB"}
        ]
        return wd._notification_fingerprint(wd._notification_report(report))

    # 残量だけが動いた場合は同一 condition とみなす
    assert _with_disk(12.16) == _with_disk(11.83)

    # 深刻度が上がったら別 condition として即通知させる
    assert _with_disk(12.16) != _with_disk(7.4, severity="critical")


def test_disk_warning_reaches_the_telegram_message_body():
    """本文に残量が出ること（fingerprint から外しても情報は失われない）。"""
    report = _notification_test_report()
    report["disk_space_issues"] = [
        {"path": "/x", "severity": "warning", "free_gb": 12.16,
         "issue": "free space below 15GB"}
    ]
    message = wd._build_watchdog_message(wd._notification_report(report))
    assert "12.16GB free" in message
    assert "warning" in message

#!/usr/bin/env python3
"""
watchdog.py (P2-9): ヘルスチェック & Silent Failure 検知

各スクリプトが utils.heartbeat() で heartbeats.json に書く生存シグナルを
定期的に評価し、以下を Telegram 通知する:
  1. 想定周期を超えて未実行（stale）
  2. 最新 status='error'
  3. FX レートが古すぎる（account.json の fx_rate_usdjpy_as_of）

Telegram 対象の重要問題が連続 3 回続いた場合のみ送信して通知ノイズを抑える。
LaunchAgent から 30 分毎に起動する想定。

使い方:
  python watchdog.py check      # 1 回チェックして終了
  python watchdog.py status     # 現在のヘルス状態を表示
"""
from __future__ import annotations

import argparse
from datetime import date
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

import json as _json

from almanac.runtime_config import get_env, resolve_db_path
from utils import HEARTBEAT_PATH, atomic_write_json, load_json

BASE_DIR = Path(__file__).parent
WATCHDOG_STATE = BASE_DIR / 'watchdog_state.json'
ACCOUNT_JSON = BASE_DIR / 'account.json'

# P2-25: silent failure 検知の対象拡張
# 重要 JSON ファイル: 破損 / 必須フィールド不在を検知
CRITICAL_JSON_FILES = {
    'holdings.json':           {'type': 'dict_of_records', 'min_records': 1},
    'account.json':            {'type': 'flat', 'required': ['balance', 'usd_balance']},
    'cash_transactions.json':  {'type': 'wrapped_list', 'key': 'transactions'},
    'nisa_portfolio.json':     {'type': 'flat', 'optional': True},
    'guard_state.json':        {'type': 'flat', 'optional': True},
}

# data/ohlcv 配下の parquet が古すぎる閾値 (秒)
PARQUET_STALE_SEC = 7 * 24 * 3600  # 7 日

# P1-3: 計測テーブル (daily_performance / benchmark_daily) の最終日付 stale 閾値 (営業日相当)
MEASUREMENT_TABLE_MAX_STALE_DAYS = 4
NEW_LANE_MAX_STALE_BUSINESS_DAYS = 2
DISK_WARNING_FREE_BYTES = 15 * 1024 ** 3
DISK_CRITICAL_FREE_BYTES = 8 * 1024 ** 3

OFFSITE_DISABLED_VALUES = {'0', 'false', 'no', 'off'}

# screener 出力が異常 (空 or 古い) と判定する閾値
SCREENER_FILES = {
    'screen_results.json':              {'key': 'candidates',   'max_stale_sec': 3 * 24 * 3600},
    'screen_results_morning.json':      {'key': 'candidates',   'max_stale_sec': 3 * 24 * 3600},
    'screen_results_jp.json':           {'key': 'candidates',   'max_stale_sec': 3 * 24 * 3600},
    'long_term_screen_results.json':    {'key': 'passed',       'max_stale_sec': 14 * 24 * 3600},
    'short_candidates.json':            {'key': 'candidates',   'max_stale_sec': 3 * 24 * 3600},
    'short_candidates_morning.json':    {'key': 'candidates',   'max_stale_sec': 3 * 24 * 3600},
    'margin_long_candidates.json':      {'key': 'candidates',   'max_stale_sec': 3 * 24 * 3600},
    'margin_long_candidates_morning.json': {'key': 'candidates', 'max_stale_sec': 3 * 24 * 3600},
}

# short_universe.json は部分実行時に未評価tickerを保持するため、ticker単位の鮮度を見る。
SHORT_UNIVERSE_MAX_STALE_SEC = 7 * 24 * 3600
SHORT_UNIVERSE_FILE = 'data/short_universe.json'

# LLM 出力 (ai_portfolio_analysis.json) の sanity check
AI_OUTPUT_FILE = 'ai_portfolio_analysis.json'
AI_OUTPUT_MAX_STALE_SEC = 26 * 3600   # 平日次の analyzer cron 想定

# 想定周期（秒）。これを超えて heartbeat が来ないと stale とみなす。
# cron/LaunchAgent の実行間隔 + 1 回分の猶予。
EXPECTED_INTERVALS = {
    # 平日朝・引け後の 2 回。週末は実行されないので weekday_only=True で評価。
    # portfolio_analyst が 06:00 の本線、analyzer_delta はその後の軽量差分監視。
    'portfolio_analyst': {'max_stale_sec': 26 * 3600, 'weekday_only': True},
    # 'analyzer' は --delta-only 運用で 'analyzer_delta' に heartbeat されるため
    # こちらを監視する（旧 'analyzer' キーは永遠に空で false positive の原因だった）。
    'analyzer_delta':    {'max_stale_sec': 24 * 3600, 'weekday_only': True},
    'data_fetcher':      {'max_stale_sec': 26 * 3600, 'weekday_only': True},
    'margin_manager':    {'max_stale_sec': 26 * 3600, 'weekday_only': True},
    'long_term_screener':{'max_stale_sec': 8 * 24 * 3600, 'weekday_only': False},
    'behavioral_guard_snapshot': {'max_stale_sec': 26 * 3600, 'weekday_only': True},
    # Auto Tune runs four times on weekdays; use a daily threshold so a single
    # missed slot is visible without generating intraday false positives.
    'auto_tune':        {'max_stale_sec': 26 * 3600, 'weekday_only': True},
    'backup_manager':    {'max_stale_sec': 26 * 3600, 'weekday_only': False},
    # 以下は heartbeat 未登録・優先度低のため監視対象外（必要になったら復活）:
    #   'short_screener', 'weekly_report', 'portfolio_agent'
}

# FX as-of が古すぎる閾値
FX_STALE_SEC = 3 * 24 * 3600  # 3 日

# Telegram は「今すぐ見に行くべき問題」だけに絞る。
# parquet stale / screener 欠落などの保守メモは status 出力に残し、通知対象から外す。
WATCHDOG_NOTIFY_COOLDOWN_SEC = 24 * 3600
NOTIFY_STALE_SCRIPTS = {
    'analyzer_delta',
    'data_fetcher',
    'margin_manager',
}
RECENT_EXECUTION_ISSUE_HOURS = 48


def _check_critical_json() -> list:
    """
    P2-25: 重要 JSON ファイルの schema 妥当性チェック。
      - ファイルが存在し
      - JSON としてパース可能で
      - 必須フィールド (required) を含み
      - dict_of_records / wrapped_list 型は最低限の中身がある
    """
    issues: list = []
    for fname, cfg in CRITICAL_JSON_FILES.items():
        path = BASE_DIR / fname
        if not path.exists():
            if not cfg.get('optional'):
                issues.append({'file': fname, 'issue': 'missing'})
            continue
        try:
            data = _json.loads(path.read_text(encoding='utf-8'))
        except _json.JSONDecodeError as e:
            issues.append({'file': fname, 'issue': f'json_parse_error: {str(e)[:120]}'})
            continue
        except OSError as e:
            issues.append({'file': fname, 'issue': f'read_error: {e}'})
            continue

        t = cfg.get('type')
        if t == 'dict_of_records':
            if not isinstance(data, dict):
                issues.append({'file': fname, 'issue': f'expected dict, got {type(data).__name__}'})
            elif cfg.get('min_records', 0) > 0 and len(data) < cfg['min_records']:
                issues.append({'file': fname, 'issue': f'too few records: {len(data)} < {cfg["min_records"]}'})
        elif t == 'flat':
            if not isinstance(data, dict):
                issues.append({'file': fname, 'issue': f'expected dict, got {type(data).__name__}'})
            else:
                missing = [k for k in cfg.get('required', []) if k not in data]
                if missing:
                    issues.append({'file': fname, 'issue': f'missing required fields: {missing}'})
        elif t == 'wrapped_list':
            key = cfg.get('key')
            if not isinstance(data, dict) or not isinstance(data.get(key), list):
                issues.append({'file': fname, 'issue': f'expected {{"{key}": [...]}}'})
    return issues


def _check_old_parquet() -> list:
    """
    P2-25: data/ohlcv/*.parquet の mtime を見て stale なものを列挙。
    旧コードは差分 append のみで auto_adjust=True の再補正に弱かった (Codex H4)。
    本ファイルではあくまで「古さ」を検知する。月次再構築は parquet_rebuilder.py が担当。
    """
    stale: list = []
    ohlcv_dir = BASE_DIR / 'data' / 'ohlcv'
    if not ohlcv_dir.exists():
        return stale
    now = time.time()
    for path in ohlcv_dir.glob('*.parquet'):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > PARQUET_STALE_SEC:
            stale.append({
                'file':      f'data/ohlcv/{path.name}',
                'age_hours': round(age / 3600, 1),
            })
    return stale


def _check_price_sanity() -> list:
    """Surface unresolved >30% moves from the sole OHLCV source.

    NAV, guardrails, and signals all depend on yfinance-derived local parquet.
    This check does not guess whether a move is real; it makes the single-source
    dependency and required corporate-action/alternate-source review visible.
    """
    path = BASE_DIR / "data" / "price_sanity_flags.jsonl"
    if not path.exists():
        return []
    latest = {}
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            row = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            latest[row.get("flag_id") or f"row-{index}"] = row
    return [row for row in latest.values() if row.get("status") == "review_required"]


def _check_measurement_tables() -> list:
    """
    P1-3: 計測テーブル (daily_performance / benchmark_daily) の最終日付 stale 検知。

    excess α / TWR / VaR の前提となるこれらが古いと、clean window 比較が壊れる
    (benchmark が portfolio より遅れて偽の excess α を生む等)。NAV snapshot は平日 23:00、
    benchmark rebuild は平日 23:05 cron 想定 (crontab.proposed)。
    """
    import sqlite3
    from datetime import date as _date

    issues: list = []
    db = resolve_db_path(BASE_DIR)
    if not db.exists():
        return issues
    today = _date.today()
    try:
        con = sqlite3.connect(str(db))
    except sqlite3.Error:
        return issues
    try:
        for table in ('daily_performance', 'benchmark_daily'):
            try:
                row = con.execute(f"SELECT MAX(date) FROM {table}").fetchone()
            except sqlite3.Error:
                continue
            last = row[0] if row else None
            if not last:
                issues.append({'table': table, 'issue': 'empty (no rows)'})
                continue
            try:
                last_d = _date.fromisoformat(str(last)[:10])
            except ValueError:
                continue
            # 暦日差で判定 (週末ぶん +2 日緩める)
            cal_gap = (today - last_d).days
            if cal_gap > MEASUREMENT_TABLE_MAX_STALE_DAYS + 2:
                issues.append({'table': table, 'last_date': str(last), 'stale_days': cal_gap})
    finally:
        con.close()
    return issues


def _parse_iso_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def _business_days_elapsed(start: date, end: date) -> int:
    """Count weekdays strictly after start through end."""
    if end <= start:
        return 0
    current = start + timedelta(days=1)
    count = 0
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _latest_jsonl_timestamp(path: Path, fields: tuple[str, ...]) -> datetime | None:
    latest: datetime | None = None
    if not path.exists():
        return None
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            row = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        parsed = next(
            (_parse_iso_datetime(row.get(field)) for field in fields if row.get(field)),
            None,
        )
        if parsed and (latest is None or parsed > latest):
            latest = parsed
    return latest


def _stale_lane_issue(
    path: Path,
    *,
    timestamp: datetime | None,
    now: datetime,
    label: str,
) -> dict | None:
    if not path.exists():
        return {'file': str(path.relative_to(BASE_DIR)), 'issue': 'missing', 'lane': label}
    if timestamp is None:
        return {'file': str(path.relative_to(BASE_DIR)), 'issue': 'no_valid_timestamp', 'lane': label}
    local_now = now.astimezone(timestamp.tzinfo) if timestamp.tzinfo else now.replace(tzinfo=None)
    age = _business_days_elapsed(timestamp.date(), local_now.date())
    if age < NEW_LANE_MAX_STALE_BUSINESS_DAYS:
        return None
    return {
        'file': str(path.relative_to(BASE_DIR)),
        'issue': f'stale ({age} business days)',
        'lane': label,
        'last_at': timestamp.isoformat(),
    }


def _check_outcome_logs(now: datetime | None = None) -> list:
    """Detect a silently open catalyst/sell outcome loop."""
    now = now or datetime.now()
    issues = []
    for rel, label in (
        ('catalyst_outcome_log.jsonl', 'catalyst_outcomes'),
        ('sell_outcome_log.jsonl', 'sell_outcomes'),
    ):
        path = BASE_DIR / rel
        latest = _latest_jsonl_timestamp(
            path,
            ('measured_at', 'recorded_at', 'created_at', 'event_at'),
        )
        issue = _stale_lane_issue(path, timestamp=latest, now=now, label=label)
        if issue:
            issues.append(issue)
    return issues


def _check_disclosure_freshness(now: datetime | None = None) -> list:
    """Use PIT ingest_time, not mtime, to detect a dead disclosure feed."""
    now = now or datetime.now()
    path = BASE_DIR / 'data' / 'disclosure_features.jsonl'
    latest = _latest_jsonl_timestamp(path, ('ingest_time',))
    issue = _stale_lane_issue(
        path,
        timestamp=latest,
        now=now,
        label='disclosure_features',
    )
    return [issue] if issue else []


def _check_shadow_book(now: datetime | None = None) -> list:
    now = now or datetime.now()
    path = BASE_DIR / 'data' / 'disclosure_shadow_book.json'
    if not path.exists():
        return [{'file': 'data/disclosure_shadow_book.json', 'issue': 'missing'}]
    try:
        payload = _json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        return [{'file': 'data/disclosure_shadow_book.json', 'issue': f'parse_error:{exc}'}]
    generated = _parse_iso_datetime(payload.get('generated_at') if isinstance(payload, dict) else None)
    issue = _stale_lane_issue(
        path,
        timestamp=generated,
        now=now,
        label='disclosure_shadow_book',
    )
    return [issue] if issue else []


def _check_lane_registry(now: datetime | None = None) -> list:
    """Warn when a measured lane has no outcome evidence for 90/180 days."""
    from almanac.observability.lane_registry import (
        load_lane_registry,
        validate_lane_registry,
    )

    now = now or datetime.now()
    path = BASE_DIR / "lane_registry.json"
    if not path.exists():
        return [{"lane": "registry", "issue": "lane_registry.json missing"}]
    try:
        errors = validate_lane_registry(path)
        lanes = load_lane_registry(path)
    except Exception as exc:
        return [{"lane": "registry", "issue": f"invalid registry: {exc}"}]
    issues = [{"lane": "registry", "issue": error} for error in errors]
    for lane in lanes:
        if lane.get("status") != "measured":
            continue
        measurement = BASE_DIR / str(lane.get("measurement_path") or "")
        latest = _parse_iso_datetime(lane.get("final_outcome_date"))
        if latest is None:
            latest = _latest_jsonl_timestamp(
                measurement,
                ("measured_at", "recorded_at", "analysis_date", "created_at", "event_at"),
            )
        if latest is None:
            issues.append({
                "lane": lane.get("name"),
                "issue": "no outcome evidence",
                "proposal": "review measurement wiring",
            })
            continue
        age_days = max(0, (now.date() - latest.date()).days)
        if age_days >= 180:
            issues.append({
                "lane": lane.get("name"),
                "issue": f"no recent outcome for {age_days} days",
                "proposal": "downgrade to display_only",
            })
        elif age_days >= 90:
            issues.append({
                "lane": lane.get("name"),
                "issue": f"no recent outcome for {age_days} days",
                "proposal": "review lane",
            })
    return issues


def _check_disk_space() -> list:
    """The market-data pipeline cannot fail loudly once the disk is full."""
    usage = shutil.disk_usage(BASE_DIR)
    if usage.free >= DISK_WARNING_FREE_BYTES:
        return []
    severity = 'critical' if usage.free < DISK_CRITICAL_FREE_BYTES else 'warning'
    return [{
        'path': str(BASE_DIR),
        'severity': severity,
        'free_gb': round(usage.free / 1024 ** 3, 2),
        'issue': f'free space below {8 if severity == "critical" else 15}GB',
    }]


def _offsite_backup_required() -> bool:
    raw = get_env("ALMANAC_REQUIRE_OFFSITE_BACKUP", "1")
    return str(raw).strip().lower() not in OFFSITE_DISABLED_VALUES


def _check_backup_offsite(heartbeats: dict | None = None) -> list:
    """Offsite backup is required for asset-state recovery unless explicitly disabled."""
    if not _offsite_backup_required():
        return []
    hb = heartbeats if heartbeats is not None else load_json(HEARTBEAT_PATH, default={})
    entry = hb.get('backup_manager') if isinstance(hb, dict) else None
    if not isinstance(entry, dict):
        return []
    extra = entry.get('extra') if isinstance(entry.get('extra'), dict) else {}
    status = extra.get('offsite_status')
    if status == 'copied':
        return []
    reason = extra.get('offsite_reason') or entry.get('error') or 'offsite_status_missing'
    severity = 'critical' if status == 'error' else 'warning'
    return [{
        'severity': severity,
        'check': 'backup_offsite',
        'status': status or 'unknown',
        'reason': reason,
        'last_run_iso': entry.get('last_run_iso'),
        'message': 'offsite backup did not complete',
    }]


def _check_screener_outputs() -> list:
    """
    P2-25: screener 出力が空 (candidates=0) or stale なら検知。
    """
    issues: list = []
    now = time.time()
    for fname, cfg in SCREENER_FILES.items():
        path = BASE_DIR / fname
        if not path.exists():
            issues.append({'file': fname, 'issue': 'missing'})
            continue
        try:
            data = _json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            issues.append({'file': fname, 'issue': 'parse_error'})
            continue

        items = data.get(cfg['key']) if isinstance(data, dict) else None
        if not isinstance(items, list):
            issues.append({'file': fname, 'issue': f'key "{cfg["key"]}" not list'})
            continue
        if len(items) == 0:
            issues.append({'file': fname, 'issue': 'empty (0 candidates)'})

        try:
            age = now - path.stat().st_mtime
            if age > cfg.get('max_stale_sec', 7 * 24 * 3600):
                issues.append({'file': fname, 'issue': f'stale ({round(age / 3600, 1)}h)'})
        except OSError:
            pass
    return issues


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def _check_short_universe_staleness(
    max_stale_sec: int = SHORT_UNIVERSE_MAX_STALE_SEC,
) -> list:
    """
    short_universe.json のticker単位の評価鮮度を検知する。

    short_universe.json は部分実行時に前回評価tickerを保持するため、ファイルmtimeだけでは
    古い銘柄行が残っているか判断できない。各行の last_evaluated_at を見る。
    """
    path = BASE_DIR / SHORT_UNIVERSE_FILE
    if not path.exists():
        # short universe は現状メイン消費者が同一実行内の戻り値を見るため、未生成は警告しない。
        return []

    try:
        data = _json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return [{'file': SHORT_UNIVERSE_FILE, 'issue': 'parse_error'}]

    tickers = data.get('tickers') if isinstance(data, dict) else None
    if not isinstance(tickers, dict):
        return [{'file': SHORT_UNIVERSE_FILE, 'issue': 'key "tickers" not dict'}]

    issues: list = []
    for ticker, row in sorted(tickers.items()):
        if not isinstance(row, dict):
            issues.append({
                'file': SHORT_UNIVERSE_FILE,
                'ticker': ticker,
                'issue': 'malformed_entry',
            })
            continue
        last_raw = row.get('last_evaluated_at')
        if not last_raw:
            issues.append({
                'file': SHORT_UNIVERSE_FILE,
                'ticker': ticker,
                'issue': 'missing_last_evaluated_at',
            })
            continue
        last_dt = _parse_iso_datetime(str(last_raw))
        if not last_dt:
            issues.append({
                'file': SHORT_UNIVERSE_FILE,
                'ticker': ticker,
                'issue': 'invalid_last_evaluated_at',
                'last_evaluated_at': str(last_raw),
            })
            continue
        now_dt = datetime.now(last_dt.tzinfo) if last_dt.tzinfo else datetime.now()
        age_sec = (now_dt - last_dt).total_seconds()
        if age_sec > max_stale_sec:
            issues.append({
                'file': SHORT_UNIVERSE_FILE,
                'ticker': ticker,
                'issue': f'stale_entry ({round(age_sec / 3600, 1)}h)',
                'last_evaluated_at': str(last_raw),
                'max_stale_hours': round(max_stale_sec / 3600, 1),
            })
    return issues


def _check_llm_output() -> list:
    """
    P2-25: ai_portfolio_analysis.json の sanity check。
      - 存在 & 24h 以内 (平日)
      - synthesis フィールドあり
      - error フィールドが入っていない
      - priority_actions が list (空でも OK = no-trade は valid)
    """
    issues: list = []
    path = BASE_DIR / AI_OUTPUT_FILE
    if not path.exists():
        if _is_weekend() or _is_monday_morning_grace():
            return issues
        issues.append({'file': AI_OUTPUT_FILE, 'issue': 'missing'})
        return issues

    try:
        data = _json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        issues.append({'file': AI_OUTPUT_FILE, 'issue': f'parse_error: {str(e)[:120]}'})
        return issues

    if data.get('error'):
        issues.append({'file': AI_OUTPUT_FILE, 'issue': f'error: {str(data["error"])[:120]}'})

    synthesis = data.get('synthesis')
    if not isinstance(synthesis, dict):
        issues.append({'file': AI_OUTPUT_FILE, 'issue': 'synthesis missing or not dict'})
    else:
        pa = synthesis.get('priority_actions')
        if pa is None:
            issues.append({'file': AI_OUTPUT_FILE, 'issue': 'priority_actions is None'})
        elif not isinstance(pa, list):
            issues.append({'file': AI_OUTPUT_FILE, 'issue': f'priority_actions wrong type: {type(pa).__name__}'})
        # 空配列 (no-trade) は valid なので警告しない

    # stale
    try:
        age = time.time() - path.stat().st_mtime
        if (not _is_weekend() and not _is_monday_morning_grace()) and age > AI_OUTPUT_MAX_STALE_SEC:
            issues.append({'file': AI_OUTPUT_FILE, 'issue': f'stale ({round(age / 3600, 1)}h)'})
    except OSError:
        pass
    return issues


def _check_portfolio_integrity() -> list:
    """
    account / holdings / cash_transactions / action_executions / event_ledger の整合性監査。
    SQLite 移行前の安全ネットとして、内部台帳のズレを watchdog に載せる。
    """
    try:
        from portfolio_integrity import run_integrity_check
        result = run_integrity_check(base_dir=BASE_DIR, db_path=resolve_db_path(BASE_DIR))
    except Exception as e:
        return [{'severity': 'critical', 'check': 'portfolio_integrity', 'message': f'integrity checker failed: {e}'}]
    return result.get('issues', [])


def _is_weekend() -> bool:
    """土日か？（JST 基準）"""
    # time.localtime() はシステム timezone 使用。macOS は JST 設定前提。
    return time.localtime().tm_wday >= 5


def _is_monday_morning_grace() -> bool:
    """月曜 9:00 前は週末分の stale を猶予（週末の未実行を責めない）"""
    lt = time.localtime()
    return lt.tm_wday == 0 and lt.tm_hour < 9


def evaluate_health() -> Dict:
    """
    heartbeats.json を評価して問題リストを返す。

    Returns:
        {
            'stale': [{'script': str, 'age_hours': float, ...}, ...],
            'errors': [{'script': str, 'error': str, ...}, ...],
            'fx_stale': bool,
            'fx_age_hours': float | None,
            'ok': [script_name, ...],
        }
    """
    hb = load_json(HEARTBEAT_PATH, default={})
    now = time.time()

    stale = []
    errors = []
    ok = []

    for script, cfg in EXPECTED_INTERVALS.items():
        entry = hb.get(script)
        if entry is None:
            # 一度も走っていない → weekend は猶予、平日は stale
            if _is_weekend() or _is_monday_morning_grace():
                continue
            stale.append({
                'script': script,
                'age_hours': None,
                'reason': 'never_run',
            })
            continue

        last = float(entry.get('last_run_ts', 0))
        age = now - last
        status = entry.get('status', 'ok')

        # weekday_only は週末を猶予
        if cfg.get('weekday_only') and (_is_weekend() or _is_monday_morning_grace()):
            if status == 'error':
                errors.append({
                    'script': script,
                    'error': entry.get('error'),
                    'age_hours': round(age / 3600, 1),
                })
            else:
                ok.append(script)
            continue

        if age > cfg['max_stale_sec']:
            stale.append({
                'script': script,
                'age_hours': round(age / 3600, 1),
                'reason': f'older_than_{cfg["max_stale_sec"] // 3600}h',
            })
        elif status == 'error':
            errors.append({
                'script': script,
                'error': entry.get('error'),
                'age_hours': round(age / 3600, 1),
            })
        else:
            ok.append(script)

    # FX as-of チェック
    acc = load_json(ACCOUNT_JSON, default={})
    fx_as_of = acc.get('fx_rate_usdjpy_as_of')
    fx_stale = False
    fx_age_hours = None
    if fx_as_of:
        fx_age_sec = now - float(fx_as_of)
        fx_age_hours = round(fx_age_sec / 3600, 1)
        fx_stale = fx_age_sec > FX_STALE_SEC

    # P2-25: silent failure 検知拡張
    schema_issues   = _check_critical_json()
    parquet_stale   = _check_old_parquet()
    price_sanity_issues = _check_price_sanity()
    screener_issues = _check_screener_outputs()
    short_universe_issues = _check_short_universe_staleness()
    llm_issues      = _check_llm_output()
    integrity_issues = _check_portfolio_integrity()
    measurement_stale = _check_measurement_tables()
    outcome_log_issues = _check_outcome_logs()
    disclosure_freshness = _check_disclosure_freshness()
    shadow_book_issues = _check_shadow_book()
    disk_space_issues = _check_disk_space()
    backup_issues = _check_backup_offsite(hb)
    lane_registry_issues = _check_lane_registry()

    return {
        'stale': stale,
        'errors': errors,
        'fx_stale': fx_stale,
        'fx_age_hours': fx_age_hours,
        'ok': ok,
        'schema_issues':   schema_issues,
        'parquet_stale':   parquet_stale,
        'price_sanity_issues': price_sanity_issues,
        'screener_issues': screener_issues,
        'short_universe_issues': short_universe_issues,
        'llm_issues':      llm_issues,
        'integrity_issues': integrity_issues,
        'measurement_stale': measurement_stale,
        'outcome_log_issues': outcome_log_issues,
        'disclosure_freshness': disclosure_freshness,
        'shadow_book_issues': shadow_book_issues,
        'disk_space_issues': disk_space_issues,
        'backup_issues': backup_issues,
        'lane_registry_issues': lane_registry_issues,
    }


def _parse_issue_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def _is_recent_issue(value: str | None, *, hours: int) -> bool:
    dt = _parse_issue_time(value)
    if not dt:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return (now - dt) <= timedelta(hours=hours)


def _should_notify_integrity_issue(issue: dict) -> bool:
    """内部台帳のうち、Telegram に載せるべき現在進行形の問題だけ選ぶ。"""
    if not isinstance(issue, dict):
        return False
    severity = str(issue.get('severity') or '').lower()
    if severity not in {'critical', 'error'}:
        return False
    check = issue.get('check')
    if check == 'cash_mirror':
        return True
    if check == 'execution_portfolio_not_applied':
        return _is_recent_issue(issue.get('saved_at'), hours=RECENT_EXECUTION_ISSUE_HOURS)
    return True


def _is_critical_or_error_issue(issue: dict) -> bool:
    if not isinstance(issue, dict):
        return False
    return str(issue.get('severity') or '').lower() in {'critical', 'error'}


def _blocking_backup_issues(report: dict) -> list:
    return [
        i for i in report.get('backup_issues', [])
        if _is_critical_or_error_issue(i)
    ]


def _notification_report(report: dict) -> dict:
    """Telegram 用に health report を高シグナル項目へ圧縮する。"""
    return {
        'stale': [
            s for s in report.get('stale', [])
            if s.get('script') in NOTIFY_STALE_SCRIPTS
        ],
        'errors': report.get('errors', []),
        'fx_stale': report.get('fx_stale'),
        'fx_age_hours': report.get('fx_age_hours'),
        'schema_issues': report.get('schema_issues', []),
        'llm_issues': report.get('llm_issues', []),
        'integrity_issues': [
            i for i in report.get('integrity_issues', [])
            if _should_notify_integrity_issue(i)
        ],
        # 計測テーブル stale は α/TWR/VaR を汚染するため高シグナル (通知対象)。
        'measurement_stale': report.get('measurement_stale', []),
        'outcome_log_issues': report.get('outcome_log_issues', []),
        'disclosure_freshness': report.get('disclosure_freshness', []),
        'shadow_book_issues': report.get('shadow_book_issues', []),
        'disk_space_issues': [
            issue for issue in report.get('disk_space_issues', [])
            if str(issue.get('severity') or '').lower() == 'critical'
        ],
        'backup_issues': _blocking_backup_issues(report),
    }


def _notification_problem_count(report: dict) -> int:
    return (
        len(report.get('stale', []))
        + len(report.get('errors', []))
        + (1 if report.get('fx_stale') else 0)
        + len(report.get('schema_issues', []))
        + len(report.get('llm_issues', []))
        + len(report.get('integrity_issues', []))
        + len(report.get('measurement_stale', []))
        + len(report.get('outcome_log_issues', []))
        + len(report.get('disclosure_freshness', []))
        + len(report.get('shadow_book_issues', []))
        + len(report.get('disk_space_issues', []))
        + len(report.get('backup_issues', []))
        + len(report.get('lane_registry_issues', []))
    )


def _notification_fingerprint(report: dict) -> str:
    payload = {
        'stale': sorted(s.get('script') for s in report.get('stale', [])),
        'errors': sorted((e.get('script'), str(e.get('error'))[:80]) for e in report.get('errors', [])),
        'fx_stale': bool(report.get('fx_stale')),
        'schema': sorted((s.get('file'), s.get('issue')) for s in report.get('schema_issues', [])),
        'llm': sorted((l.get('file'), l.get('issue')) for l in report.get('llm_issues', [])),
        'integrity': sorted(
            (i.get('check'), i.get('ticker'), i.get('execution_id'), str(i.get('message'))[:80])
            for i in report.get('integrity_issues', [])
        ),
        'outcomes': sorted((i.get('file'), i.get('issue')) for i in report.get('outcome_log_issues', [])),
        'disclosure': sorted((i.get('file'), i.get('issue')) for i in report.get('disclosure_freshness', [])),
        'shadow': sorted((i.get('file'), i.get('issue')) for i in report.get('shadow_book_issues', [])),
        'disk': sorted((i.get('severity'), i.get('free_gb')) for i in report.get('disk_space_issues', [])),
        'backup': sorted((i.get('severity'), i.get('status'), i.get('reason')) for i in report.get('backup_issues', [])),
    }
    return _json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _build_watchdog_message(report: dict) -> str:
    msg_lines = ['🚨 ALMANAC watchdog 重要アラート']
    if report.get('stale'):
        msg_lines.append('\n⏰ 主要ジョブ停止:')
        for s in report['stale'][:5]:
            age = s.get('age_hours')
            age_str = f"{age}h" if age is not None else 'never'
            msg_lines.append(f"  • {s['script']}: {age_str} ({s['reason']})")
        if len(report['stale']) > 5:
            msg_lines.append(f"  ... 他 {len(report['stale']) - 5} 件")
    if report.get('errors'):
        msg_lines.append('\n❌ 実行エラー:')
        for e in report['errors'][:5]:
            msg_lines.append(f"  • {e['script']}: {(e.get('error') or '')[:120]}")
        if len(report['errors']) > 5:
            msg_lines.append(f"  ... 他 {len(report['errors']) - 5} 件")
    if report.get('fx_stale'):
        msg_lines.append(f"\n💱 FX レート古い: {report.get('fx_age_hours')}h 前（3日超）")
    if report.get('schema_issues'):
        msg_lines.append('\n📁 重要JSON破損/schema異常:')
        for s in report['schema_issues'][:5]:
            msg_lines.append(f"  • {s['file']}: {s['issue']}")
        if len(report['schema_issues']) > 5:
            msg_lines.append(f"  ... 他 {len(report['schema_issues']) - 5} 件")
    if report.get('llm_issues'):
        msg_lines.append('\n🤖 AI分析出力異常:')
        for l in report['llm_issues'][:5]:
            msg_lines.append(f"  • {l['file']}: {l['issue']}")
        if len(report['llm_issues']) > 5:
            msg_lines.append(f"  ... 他 {len(report['llm_issues']) - 5} 件")
    if report.get('integrity_issues'):
        msg_lines.append('\n🧾 台帳整合性（要対応）:')
        for i in report['integrity_issues'][:5]:
            msg_lines.append(f"  • [{i.get('severity')}] {i.get('check')}: {i.get('message')}")
        if len(report['integrity_issues']) > 5:
            msg_lines.append(f"  ... 他 {len(report['integrity_issues']) - 5} 件")
    if report.get('measurement_stale'):
        msg_lines.append('\n📉 計測テーブル stale（α/TWR/VaR 汚染リスク）:')
        for m in report['measurement_stale'][:5]:
            if m.get('issue'):
                msg_lines.append(f"  • {m['table']}: {m['issue']}")
            else:
                msg_lines.append(f"  • {m['table']}: 最終 {m.get('last_date')}（{m.get('stale_days')}日前）")
    if report.get('outcome_log_issues'):
        msg_lines.append('\n📏 outcome 計測停止:')
        for item in report['outcome_log_issues'][:5]:
            msg_lines.append(f"  • {item['file']}: {item['issue']}")
    if report.get('disclosure_freshness'):
        msg_lines.append('\n📰 開示ストア鮮度異常:')
        for item in report['disclosure_freshness'][:5]:
            msg_lines.append(f"  • {item['file']}: {item['issue']}")
    if report.get('shadow_book_issues'):
        msg_lines.append('\n📚 shadow book 鮮度異常:')
        for item in report['shadow_book_issues'][:5]:
            msg_lines.append(f"  • {item['file']}: {item['issue']}")
    if report.get('disk_space_issues'):
        msg_lines.append('\n💾 ディスク空き容量:')
        for item in report['disk_space_issues'][:5]:
            msg_lines.append(
                f"  • [{item['severity']}] {item['free_gb']}GB free: {item['issue']}"
            )
    if report.get('backup_issues'):
        msg_lines.append('\n🗄️ オフサイトバックアップ:')
        for item in report['backup_issues'][:5]:
            msg_lines.append(
                f"  • [{item['severity']}] status={item.get('status')} reason={item.get('reason')}"
            )
    msg_lines.append('\n※ parquet stale / screener欠落 / 古い過去約定の棚卸しは status にのみ表示。')
    return '\n'.join(msg_lines)


def run_check(notify: bool = True) -> int:
    """
    チェック 1 回分。Telegram 対象の重要問題が連続 3 回続いた場合のみ notify。
    戻り値: 問題件数（stale + errors）
    """
    report = evaluate_health()
    state = load_json(WATCHDOG_STATE, default={'consecutive_failures': 0, 'last_notified': 0})

    blocking_backup_issues = _blocking_backup_issues(report)
    advisory_count = len(report.get('backup_issues', [])) - len(blocking_backup_issues)

    problem_count = (
        len(report['stale'])
        + len(report['errors'])
        + (1 if report['fx_stale'] else 0)
        + len(report.get('schema_issues', []))
        + len(report.get('parquet_stale', []))
        + len(report.get('price_sanity_issues', []))
        + len(report.get('screener_issues', []))
        + len(report.get('short_universe_issues', []))
        + len(report.get('llm_issues', []))
        + len(report.get('integrity_issues', []))
        + len(report.get('measurement_stale', []))
        + len(report.get('outcome_log_issues', []))
        + len(report.get('disclosure_freshness', []))
        + len(report.get('shadow_book_issues', []))
        + len(report.get('disk_space_issues', []))
        + len(blocking_backup_issues)
        + len(report.get('lane_registry_issues', []))
    )

    notify_report = _notification_report(report)
    notify_problem_count = _notification_problem_count(notify_report)

    if problem_count == 0:
        state['consecutive_failures'] = 0
        state['consecutive_notify_failures'] = 0
        state['last_notification_status'] = 'skipped'
        state['last_notification_failure_reason'] = 'healthy'
        atomic_write_json(WATCHDOG_STATE, state)
        if advisory_count:
            print(f'[watchdog] 重要問題なし (advisory={advisory_count})')
        else:
            print('[watchdog] すべて OK')
        return 0

    state['consecutive_failures'] = int(state.get('consecutive_failures', 0)) + 1
    if notify_problem_count:
        state['consecutive_notify_failures'] = int(state.get('consecutive_notify_failures', 0)) + 1
    else:
        state['consecutive_notify_failures'] = 0

    # ノイズ抑制: Telegram 対象の重要問題が連続した場合のみ通知、かつ同一内容は 24h 空ける
    should_notify = notify and notify_problem_count > 0 and state['consecutive_notify_failures'] >= 3
    fingerprint = _notification_fingerprint(notify_report)
    last_fingerprint = state.get('last_notification_fingerprint')
    if should_notify and fingerprint == last_fingerprint and (
        time.time() - float(state.get('last_notified', 0))
    ) < WATCHDOG_NOTIFY_COOLDOWN_SEC:
        should_notify = False

    if notify_problem_count:
        message = _build_watchdog_message(notify_report)
        print(message)
    else:
        message = ''
        print(
            '[watchdog] 問題はありますが Telegram 対象の重大問題はありません '
            f'(total={problem_count})'
        )

    if should_notify:
        try:
            from alert import send_telegram
            sent = send_telegram(message)
            if not sent:
                raise RuntimeError('Telegram credentials are unavailable')
            state['last_notified'] = time.time()
            state['last_notification_fingerprint'] = fingerprint
            state['last_notification_status'] = 'sent'
            state['last_notification_failure_reason'] = None
            print('[watchdog] Telegram 通知送信済み')
        except Exception as e:
            state['last_notification_status'] = 'failed'
            state['last_notification_failure_reason'] = str(e)
            print(f'[watchdog] Telegram 通知失敗: {e}')
    else:
        state['last_notification_status'] = 'skipped'
        state['last_notification_failure_reason'] = (
            'notify_disabled' if not notify
            else 'no_notifiable_problem' if notify_problem_count == 0
            else 'debounce_or_cooldown'
        )

    atomic_write_json(WATCHDOG_STATE, state)
    return problem_count


def print_status() -> None:
    report = evaluate_health()
    print('=== ALMANAC ヘルスチェック ===')
    print(f"OK: {len(report['ok'])} / stale: {len(report['stale'])} / errors: {len(report['errors'])}")
    print(f"FX: {'STALE' if report['fx_stale'] else 'fresh'} ({report['fx_age_hours']}h 前)")
    print(f"schema_issues: {len(report.get('schema_issues', []))} / "
          f"parquet_stale: {len(report.get('parquet_stale', []))} / "
          f"price_sanity_issues: {len(report.get('price_sanity_issues', []))} / "
          f"screener_issues: {len(report.get('screener_issues', []))} / "
          f"short_universe_issues: {len(report.get('short_universe_issues', []))} / "
          f"llm_issues: {len(report.get('llm_issues', []))} / "
          f"integrity_issues: {len(report.get('integrity_issues', []))} / "
          f"outcome_log_issues: {len(report.get('outcome_log_issues', []))} / "
          f"disclosure_freshness: {len(report.get('disclosure_freshness', []))} / "
          f"shadow_book_issues: {len(report.get('shadow_book_issues', []))} / "
          f"disk_space_issues: {len(report.get('disk_space_issues', []))} / "
          f"backup_issues: {len(report.get('backup_issues', []))}")
    if report['stale']:
        print('\n-- stale --')
        for s in report['stale']:
            print(f"  {s}")
    if report['errors']:
        print('\n-- errors --')
        for e in report['errors']:
            print(f"  {e}")
    for cat in (
        'schema_issues',
        'parquet_stale',
        'price_sanity_issues',
        'screener_issues',
        'short_universe_issues',
        'llm_issues',
        'integrity_issues',
        'measurement_stale',
        'outcome_log_issues',
        'disclosure_freshness',
        'shadow_book_issues',
        'disk_space_issues',
        'backup_issues',
    ):
        items = report.get(cat, [])
        if items:
            print(f'\n-- {cat} --')
            for it in items[:20]:
                print(f"  {it}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ALMANAC watchdog')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('check', help='ヘルスチェック実行（連続失敗で Telegram 通知）')
    sub.add_parser('status', help='現在のヘルス状態を表示（通知なし）')
    args = parser.parse_args()

    if args.cmd == 'check':
        n = run_check(notify=True)
        sys.exit(0 if n == 0 else 1)
    elif args.cmd == 'status':
        print_status()

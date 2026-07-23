#!/usr/bin/env python3
"""
backup_manager.py (P3-14 / O2-O3): 復元可能な日次バックアップ & ローテーション

対象ファイル（税務根拠・資産状態の根幹）:
  - holdings.json
  - account.json
  - guard_state.json
  - signal_history.json
  - nisa_portfolio.json
  - nisa_sale_history.json
  - action_executions.json
  - trade_history.csv
  - beliefs/agent_beliefs.json

保存先: backups/YYYYMMDD/
ローテーション:
  - 直近 7 日: 毎日保持
  - 8〜30 日: 週次（月曜のみ）
  - 31〜365 日: 月次（1 日のみ）
  - 365 日超: 削除

破損検知:
  起動時に JSON ファイルの妥当性を検査。破損検知時は最新バックアップから復元提案。

使い方:
  python backup_manager.py snapshot    # 今日のバックアップ + repo/frontend bundle を作成
  python backup_manager.py offsite     # rclone crypt remote へ当日分をコピー
  python backup_manager.py rotate      # ローテーションのみ実行
  python backup_manager.py verify      # JSON 妥当性検査
  python backup_manager.py restore YYYYMMDD <file>   # 特定日から復元
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, List

from almanac.runtime_config import get_env

BASE_DIR = Path(__file__).parent
BACKUP_DIR = BASE_DIR / 'backups'
BACKUP_DIR.mkdir(exist_ok=True)

# バックアップ対象（税法7年保持 + 資産状態の根幹）
# P1-12: cash_transactions / tunable_params を追加（旧コードは対象漏れだった）
TARGETS = [
    'holdings.json',
    'account.json',
    'guard_state.json',
    'signal_history.json',
    'nisa_portfolio.json',
    'nisa_sale_history.json',
    'action_executions.json',
    'trade_history.csv',
    'beliefs/agent_beliefs.json',
    'heartbeats.json',
    # P1-12 追加
    'cash_transactions.json',
    'tunable_params.json',
    'tunable_params_state.json',
    'tunable_params_history.jsonl',
    'tuning_auto_state.json',
    'tuning_auto_runs.jsonl',
    'bl_views.json',
    'agent_briefing.json',
    # O2: PIT / outcome / audit stores. JSONL files are validated line-by-line.
    'data/disclosure_features.jsonl',
    'catalyst_hypothesis_log.jsonl',
    'catalyst_outcome_log.jsonl',
    'sell_decision_log.jsonl',
    'sell_outcome_log.jsonl',
    'feature_certifications.jsonl',
    'human_feedback_log.jsonl',
    'data/disclosure_push_state.json',
    'logs/llm_calls.jsonl',
    # 2026-07: AI 動的外貨比率方針の state / 監査 log。
    'currency_policy_state.json',
    'currency_policy_log.jsonl',
    # 2026-07: 楽天かぶミニ対象銘柄のローカル確認台帳。
    'data/kabu_mini_eligible.json',
    'data/kabu_mini_verification_needed.json',
]

SQLITE_TARGETS = [
    'almanac.db',
    'nexustrader.db',
]

NESTED_REPOSITORIES = [
    ('frontend', 'frontend.bundle'),
]

FRONTEND_WORKTREE_ARCHIVE = 'frontend_worktree.tar.gz'
FRONTEND_WORKTREE_EXCLUDE_PARTS = {
    '.git',
    '.next',
    '.turbo',
    '.vercel',
    'coverage',
    'node_modules',
}

DEFAULT_OFFSITE_REMOTE = 'crypt-gdrive:almanac_backup'
RCLONE_FALLBACK_PATHS = (
    Path('/opt/homebrew/bin/rclone'),
    Path('/usr/local/bin/rclone'),
)

# ローテーションポリシー（日数）
DAILY_RETENTION_DAYS   = 7     # 直近7日: 全て保持
WEEKLY_RETENTION_DAYS  = 30    # 8-30日: 週次（月曜）
MONTHLY_RETENTION_DAYS = 365   # 31-365日: 月次（1日）
# 365日超は削除


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def _backup_sqlite(src: Path, dst: Path) -> None:
    """Create a transactionally consistent SQLite backup, including WAL state."""
    dst.parent.mkdir(exist_ok=True, parents=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _create_git_bundle(
    repo_dir: Path,
    bundle: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    try:
        result = runner(
            ['git', '-C', str(repo_dir), 'bundle', 'create', str(bundle), '--all'],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {'status': 'skipped', 'reason': f'git_unavailable:{exc}'}
    if result.returncode != 0:
        return {
            'status': 'skipped',
            'reason': f'git_bundle_failed:{(result.stderr or result.stdout).strip()[:200]}',
        }
    return {
        'status': 'created',
        'path': str(bundle.relative_to(BACKUP_DIR)),
        'sha256': _sha256(bundle),
    }


def _create_repo_bundle(
    target_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Create a restorable git bundle without changing remotes or the worktree."""
    bundle = target_dir / 'repo.bundle'
    return _create_git_bundle(BASE_DIR, bundle, runner=runner)


def _create_nested_repo_bundles(
    target_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Bundle nested git repositories that are outside the parent repo bundle."""
    bundles = {}
    for repo_rel, bundle_name in NESTED_REPOSITORIES:
        repo_dir = BASE_DIR / repo_rel
        if not (repo_dir / '.git').exists():
            bundles[repo_rel] = {'status': 'skipped', 'reason': 'not_a_git_repo'}
            continue
        bundles[repo_rel] = _create_git_bundle(
            repo_dir,
            target_dir / bundle_name,
            runner=runner,
        )
    return bundles


def _skip_frontend_archive_path(rel: Path) -> bool:
    if any(part in FRONTEND_WORKTREE_EXCLUDE_PARTS for part in rel.parts):
        return True
    name = rel.name
    return name in {'.env', '.env.local'} or (name.startswith('.env.') and name.endswith('.local'))


def _create_frontend_worktree_archive(target_dir: Path) -> dict:
    """Archive frontend sources, including untracked work, without heavy build artifacts."""
    frontend_dir = BASE_DIR / 'frontend'
    if not frontend_dir.exists():
        return {'status': 'skipped', 'reason': 'frontend_missing'}

    archive = target_dir / FRONTEND_WORKTREE_ARCHIVE
    try:
        with tarfile.open(archive, 'w:gz') as tar:
            for path in sorted(frontend_dir.rglob('*')):
                rel = path.relative_to(frontend_dir)
                if _skip_frontend_archive_path(rel):
                    continue
                tar.add(path, arcname=str(Path('frontend') / rel), recursive=False)
    except OSError as exc:
        return {'status': 'skipped', 'reason': f'archive_failed:{exc}'}

    return {
        'status': 'created',
        'path': str(archive.relative_to(BACKUP_DIR)),
        'sha256': _sha256(archive),
    }


def snapshot(
    today: date = None,
    *,
    bundle_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """今日のバックアップを backups/YYYYMMDD/ に作成。"""
    today = today or date.today()
    target_dir = BACKUP_DIR / today.strftime('%Y%m%d')
    target_dir.mkdir(exist_ok=True, parents=True)

    results = {
        'date': today.isoformat(),
        'copied': [],
        'missing': [],
        'hashes': {},
        'sqlite_backups': [],
        'nested_repo_bundles': {},
        'worktree_archives': {},
    }

    for rel in TARGETS:
        src = BASE_DIR / rel
        if not src.exists():
            results['missing'].append(rel)
            continue
        dst = target_dir / rel
        dst.parent.mkdir(exist_ok=True, parents=True)
        shutil.copy2(src, dst)
        results['copied'].append(rel)
        try:
            results['hashes'][rel] = _sha256(dst)
        except Exception:
            results['hashes'][rel] = None

    for rel in SQLITE_TARGETS:
        src = BASE_DIR / rel
        if not src.exists():
            results['missing'].append(rel)
            continue
        dst = target_dir / rel
        _backup_sqlite(src, dst)
        results['copied'].append(rel)
        results['sqlite_backups'].append(rel)
        results['hashes'][rel] = _sha256(dst)

    results['repo_bundle'] = _create_repo_bundle(
        target_dir,
        runner=bundle_runner,
    )
    results['nested_repo_bundles'] = _create_nested_repo_bundles(
        target_dir,
        runner=bundle_runner,
    )
    results['worktree_archives']['frontend'] = _create_frontend_worktree_archive(target_dir)

    # ハッシュマニフェストを同梱（改竄検知用）
    manifest_path = target_dir / 'manifest.json'
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump({
            'created_at': datetime.now().isoformat(),
            'files':      results['copied'],
            'hashes':     results['hashes'],
            'repo_bundle': results['repo_bundle'],
            'nested_repo_bundles': results['nested_repo_bundles'],
            'worktree_archives': results['worktree_archives'],
        }, f, indent=2, ensure_ascii=False)

    return results


def offsite_copy(
    today: date = None,
    *,
    remote: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Copy today's backup to an rclone crypt remote, or skip gracefully."""
    today = today or date.today()
    source = BACKUP_DIR / today.strftime('%Y%m%d')
    if not source.exists():
        return {'status': 'skipped', 'reason': 'backup_missing', 'source': str(source)}

    rclone = _find_rclone()
    if not rclone:
        return {'status': 'skipped', 'reason': 'rclone_not_installed'}

    destination_root = (
        remote
        or get_env('ALMANAC_OFFSITE_REMOTE', DEFAULT_OFFSITE_REMOTE)
        or DEFAULT_OFFSITE_REMOTE
    ).rstrip('/')
    remote_name = destination_root.split(':', 1)[0]
    try:
        remotes = runner(
            [rclone, 'listremotes'],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {'status': 'skipped', 'reason': f'rclone_unavailable:{exc}'}
    configured = {
        line.strip().rstrip(':')
        for line in (remotes.stdout or '').splitlines()
        if line.strip()
    }
    if remotes.returncode != 0 or remote_name not in configured:
        return {'status': 'skipped', 'reason': f'remote_not_configured:{remote_name}'}

    destination = f"{destination_root}/{today.strftime('%Y%m%d')}"
    copied = runner(
        [rclone, 'copy', str(source), destination],
        capture_output=True,
        text=True,
        check=False,
    )
    if copied.returncode != 0:
        return {
            'status': 'error',
            'reason': (copied.stderr or copied.stdout).strip()[:500],
            'destination': destination,
        }
    return {'status': 'copied', 'source': str(source), 'destination': destination}


def _find_rclone() -> str | None:
    rclone = shutil.which('rclone')
    if rclone:
        return rclone
    for candidate in RCLONE_FALLBACK_PATHS:
        if candidate.exists():
            return str(candidate)
    return None


def _parse_backup_date(dirname: str) -> date | None:
    try:
        return datetime.strptime(dirname, '%Y%m%d').date()
    except Exception:
        return None


def rotate(today: date = None) -> dict:
    """
    バックアップをポリシーに従ってローテーション削除する。
    """
    today = today or date.today()
    kept = []
    removed = []

    for entry in sorted(BACKUP_DIR.iterdir()):
        if not entry.is_dir():
            continue
        bdate = _parse_backup_date(entry.name)
        if bdate is None:
            continue
        age = (today - bdate).days

        keep = False
        if age <= DAILY_RETENTION_DAYS:
            keep = True
        elif age <= WEEKLY_RETENTION_DAYS:
            # 週次（月曜のみ）
            keep = (bdate.weekday() == 0)
        elif age <= MONTHLY_RETENTION_DAYS:
            # 月次（1日のみ）
            keep = (bdate.day == 1)
        else:
            keep = False

        if keep:
            kept.append(entry.name)
        else:
            shutil.rmtree(entry)
            removed.append(entry.name)

    return {'kept': kept, 'removed': removed, 'rotated_at': today.isoformat()}


def verify() -> dict:
    """
    重要 JSON ファイルの妥当性を検査し、破損検知時は最新バックアップを提案する。
    """
    broken = []
    ok = []
    for rel in TARGETS:
        p = BASE_DIR / rel
        if not p.exists():
            continue
        if p.suffix == '.csv':
            # CSV は非空チェックのみ
            if p.stat().st_size == 0:
                broken.append({'file': rel, 'reason': 'empty'})
            else:
                ok.append(rel)
            continue
        if p.suffix == '.jsonl':
            line_no = 0
            try:
                for line_no, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1):
                    if line.strip():
                        json.loads(line)
                ok.append(rel)
            except Exception as e:
                broken.append({'file': rel, 'reason': f'jsonl line {line_no}: {str(e)[:160]}'})
            continue
        try:
            with open(p, encoding='utf-8') as f:
                json.load(f)
            ok.append(rel)
        except Exception as e:
            broken.append({'file': rel, 'reason': str(e)[:200]})

    for rel in SQLITE_TARGETS:
        p = BASE_DIR / rel
        if not p.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                integrity = con.execute('PRAGMA integrity_check').fetchone()
            finally:
                con.close()
            if not integrity or integrity[0] != 'ok':
                broken.append({'file': rel, 'reason': f'integrity_check:{integrity}'})
            else:
                ok.append(rel)
        except sqlite3.Error as exc:
            broken.append({'file': rel, 'reason': f'sqlite_error:{exc}'})

    # 破損ファイルごとに最新バックアップを提案
    restore_suggestions = []
    if broken:
        available_dates = sorted(
            [_parse_backup_date(d.name) for d in BACKUP_DIR.iterdir() if d.is_dir() and _parse_backup_date(d.name)],
            reverse=True,
        )
        for b in broken:
            for bdate in available_dates:
                candidate = BACKUP_DIR / bdate.strftime('%Y%m%d') / b['file']
                if candidate.exists():
                    restore_suggestions.append({
                        'file':    b['file'],
                        'restore_from': bdate.isoformat(),
                        'command': f'python backup_manager.py restore {bdate.strftime("%Y%m%d")} {b["file"]}',
                    })
                    break

    return {
        'verified_at': datetime.now().isoformat(),
        'ok':          ok,
        'broken':      broken,
        'restore_suggestions': restore_suggestions,
    }


def restore(backup_date: str, file_rel: str, *, confirm: bool = False) -> bool:
    """
    指定日のバックアップから特定ファイルを復元する（現状ファイルは .bak に退避）。
    """
    src = BACKUP_DIR / backup_date / file_rel
    if not src.exists():
        print(f'[restore] 見つかりません: {src}')
        return False

    dst = BASE_DIR / file_rel
    if dst.exists():
        backup_current = dst.with_suffix(dst.suffix + '.bak')
        shutil.copy2(dst, backup_current)
        print(f'[restore] 現在ファイルを退避: {backup_current}')

    if not confirm:
        print(f'[restore] 確認: {src} -> {dst} ? (--yes で実行)')
        return False

    dst.parent.mkdir(exist_ok=True, parents=True)
    shutil.copy2(src, dst)
    print(f'[restore] 復元完了: {dst} <- {src}')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ALMANAC backup manager')
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('snapshot', help='今日のバックアップを作成')
    sub.add_parser('offsite', help='当日バックアップを rclone crypt remote へコピー')
    sub.add_parser('rotate',   help='古いバックアップを削除（7d/30d/365d ローテ）')
    sub.add_parser('verify',   help='重要ファイルの妥当性検査')

    r = sub.add_parser('restore', help='特定日のバックアップから復元')
    r.add_argument('date',  help='YYYYMMDD')
    r.add_argument('file',  help='相対パス（例: holdings.json）')
    r.add_argument('--yes', action='store_true', help='確認無しで実行')

    sub.add_parser('daily', help='snapshot + rotate を続けて実行（cron 用）')

    args = parser.parse_args()

    if args.cmd == 'snapshot':
        r = snapshot()
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == 'rotate':
        r = rotate()
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == 'verify':
        r = verify()
        print(json.dumps(r, indent=2, ensure_ascii=False))
        sys.exit(0 if not r['broken'] else 1)
    elif args.cmd == 'restore':
        ok = restore(args.date, args.file, confirm=args.yes)
        sys.exit(0 if ok else 1)
    elif args.cmd == 'offsite':
        r = offsite_copy()
        print(json.dumps(r, indent=2, ensure_ascii=False))
        sys.exit(1 if r.get('status') == 'error' else 0)
    elif args.cmd == 'daily':
        s = snapshot()
        o = offsite_copy()
        r = rotate()
        print(json.dumps({'snapshot': s, 'offsite': o, 'rotate': r}, indent=2, ensure_ascii=False))
        # P2-9 heartbeat
        try:
            from utils import heartbeat
            heartbeat(
                'backup_manager',
                'ok' if o.get('status') != 'error' else 'error',
                extra={
                    'copied': len(s['copied']),
                    'removed': len(r['removed']),
                    'offsite_status': o.get('status'),
                    'offsite_reason': o.get('reason'),
                    'offsite_destination': o.get('destination'),
                    'repo_bundle_status': (s.get('repo_bundle') or {}).get('status'),
                },
            )
        except Exception:
            pass

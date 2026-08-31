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
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from almanac.runtime_config import get_env
from utils import process_lock

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
    'action_state.json',
    'action_executions.json',
    'execution_invalidation_state.json',
    'execution_reconciliation_state.json',
    'execution_preflight_acknowledgements.jsonl',
    'flow_adjusted_dd_shadow.json',
    'drawdown_state.json',
    'deployment_rollout_state.json',
    'deployment_rollout_audit.jsonl',
    'feature_control_state.json',
    'decision_snapshot_state.json',
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
    # Broker reconciliation and FX shadow evidence are not reconstructable
    # from Git.  Missing optional snapshots are reported, not fabricated.
    'broker_position_snapshot_monex.json',
    'broker_position_snapshot_rakuten.json',
    'broker_position_snapshot_sbi.json',
    'fx_actual_hedge_state.json',
    'fx_instrument_master.json',
    'hedge_target.json',
    'hedge_target_shadow.json',
    # 2026-07: 楽天かぶミニ対象銘柄のローカル確認台帳。
    'data/kabu_mini_eligible.json',
    'data/kabu_mini_verification_needed.json',
]

# A missing required item makes the generation non-publishable.  Everything
# else remains useful evidence but is explicitly optional instead of silently
# sharing one undifferentiated ``missing`` list.
REQUIRED_TARGETS = [
    'holdings.json',
    'account.json',
    'action_state.json',
    'action_executions.json',
    'trade_history.csv',
    'nisa_portfolio.json',
    'cash_transactions.json',
]
OPTIONAL_TARGETS = [rel for rel in TARGETS if rel not in REQUIRED_TARGETS]

# ⚠️ TARGETS はファイルの明示リストであって、snapshot() のコピーは
# shutil.copy2(src, dst) を直に呼ぶ。ここへディレクトリを1行足すと
# IsADirectoryError で日次バックアップ全体が失敗する。ディレクトリ単位で
# 増えていく証跡 (隔離ライブ検証のハッシュマニフェストなど) は別リストで
# 扱い、配下の通常ファイルを個別に再帰コピーする (2026-08-29、
# logs/verification_manifests/ 追加時)。
EVIDENCE_DIRECTORIES = [
    'logs/verification_manifests',
]

SQLITE_TARGETS = [
    'almanac.db',
    'nexustrader.db',
]
REQUIRED_SQLITE_TARGETS = list(SQLITE_TARGETS)

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
        'path': bundle.name,
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
        'path': archive.name,
        'sha256': _sha256(archive),
    }


def snapshot(
    today: date = None,
    *,
    bundle_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Build a complete generation, then atomically publish it by date."""
    today = today or date.today()
    generation_name = today.strftime('%Y%m%d')
    published_dir = BACKUP_DIR / generation_name
    target_dir = Path(tempfile.mkdtemp(prefix=f'.{generation_name}.tmp.', dir=BACKUP_DIR))

    results = {
        'date': today.isoformat(),
        'status': 'building',
        'published': False,
        'copied': [],
        'missing': [],
        'missing_required': [],
        'missing_optional': [],
        'hashes': {},
        'sqlite_backups': [],
        'nested_repo_bundles': {},
        'worktree_archives': {},
        'portfolio_lock_acquired': False,
    }

    # Keep cross-file portfolio facts and the transactionally consistent
    # SQLite image in the same write-free capture window.  Git/archive work is
    # intentionally outside this lock because it can take much longer and is
    # unrelated to financial-state consistency.
    try:
        with process_lock('portfolio_ledger', timeout=30.0):
            results['portfolio_lock_acquired'] = True
            for rel in TARGETS:
                src = BASE_DIR / rel
                if not src.is_file():
                    results['missing'].append(rel)
                    bucket = 'missing_required' if rel in REQUIRED_TARGETS else 'missing_optional'
                    results[bucket].append(rel)
                    continue
                dst = target_dir / rel
                dst.parent.mkdir(exist_ok=True, parents=True)
                shutil.copy2(src, dst)
                results['copied'].append(rel)
                results['hashes'][rel] = _sha256(dst)

            for rel in SQLITE_TARGETS:
                src = BASE_DIR / rel
                if not src.is_file():
                    results['missing'].append(rel)
                    bucket = 'missing_required' if rel in REQUIRED_SQLITE_TARGETS else 'missing_optional'
                    results[bucket].append(rel)
                    continue
                dst = target_dir / rel
                _backup_sqlite(src, dst)
                results['copied'].append(rel)
                results['sqlite_backups'].append(rel)
                results['hashes'][rel] = _sha256(dst)

    # ⚠️ portfolio lock の外。財務state と違って書込み一貫性を要らず、
    # ディレクトリが育つほど時間もかかる — repo_bundle 等と同じ理由
    # (上のコメント参照)。ディレクトリ不在は「証跡がまだ無いだけ」であり
    # 日次バックアップ自体を失敗させない (optional 扱い)。
        for rel_dir in EVIDENCE_DIRECTORIES:
            src_dir = BASE_DIR / rel_dir
            if not src_dir.is_dir():
                results['missing'].append(rel_dir)
                results['missing_optional'].append(rel_dir)
                continue
            for src in sorted(p for p in src_dir.rglob('*') if p.is_file()):
                rel = str(src.relative_to(BASE_DIR))
                dst = target_dir / rel
                dst.parent.mkdir(exist_ok=True, parents=True)
                shutil.copy2(src, dst)
                results['copied'].append(rel)
                results['hashes'][rel] = _sha256(dst)

        results['repo_bundle'] = _create_repo_bundle(target_dir, runner=bundle_runner)
        results['nested_repo_bundles'] = _create_nested_repo_bundles(
            target_dir, runner=bundle_runner
        )
        results['worktree_archives']['frontend'] = _create_frontend_worktree_archive(target_dir)

        artifact_hashes: dict[str, str] = {}
        for meta in [
            results['repo_bundle'],
            *results['nested_repo_bundles'].values(),
            *results['worktree_archives'].values(),
        ]:
            if isinstance(meta, dict) and meta.get('status') == 'created':
                artifact_hashes[str(meta['path'])] = str(meta['sha256'])

        results['status'] = 'complete' if not results['missing_required'] else 'incomplete'
        manifest = {
            'schema_version': 2,
            'status': results['status'],
            'created_at': datetime.now().astimezone().isoformat(),
            'files': sorted(results['copied']),
            'hashes': results['hashes'],
            'artifact_hashes': artifact_hashes,
            'expected_files': sorted({*results['copied'], *artifact_hashes}),
            'missing_required': sorted(results['missing_required']),
            'missing_optional': sorted(results['missing_optional']),
            'repo_bundle': results['repo_bundle'],
            'nested_repo_bundles': results['nested_repo_bundles'],
            'worktree_archives': results['worktree_archives'],
            'portfolio_lock_acquired': results['portfolio_lock_acquired'],
        }
        with open(target_dir / 'manifest.json', 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False, allow_nan=False)

        if results['status'] != 'complete':
            return results

        previous = BACKUP_DIR / f'.{generation_name}.previous.{os.getpid()}'
        with process_lock('backup_snapshot', timeout=30.0):
            if previous.exists():
                shutil.rmtree(previous)
            if published_dir.exists():
                os.replace(published_dir, previous)
            try:
                os.replace(target_dir, published_dir)
            except Exception:
                if previous.exists() and not published_dir.exists():
                    os.replace(previous, published_dir)
                raise
            if previous.exists():
                shutil.rmtree(previous)
        results['published'] = True
        results['path'] = str(published_dir)
        return results
    finally:
        if target_dir.exists():
            shutil.rmtree(target_dir)


def offsite_copy(
    today: date = None,
    *,
    remote: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Mirror one verified generation and verify the remote byte set."""
    today = today or date.today()
    source = BACKUP_DIR / today.strftime('%Y%m%d')
    if not source.exists():
        return {'status': 'skipped', 'reason': 'backup_missing', 'source': str(source)}
    local_verification = verify_snapshot(today.strftime('%Y%m%d'))
    if local_verification.get('status') != 'ok':
        return {
            'status': 'error',
            'reason': 'local_backup_verification_failed',
            'source': str(source),
            'verification': local_verification,
        }

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
        [rclone, 'sync', str(source), destination],
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
    checked = runner(
        [rclone, 'check', str(source), destination, '--download'],
        capture_output=True,
        text=True,
        check=False,
    )
    if checked.returncode != 0:
        return {
            'status': 'error',
            'reason': f"remote_verification_failed:{(checked.stderr or checked.stdout).strip()[:400]}",
            'source': str(source),
            'destination': destination,
        }
    return {
        'status': 'copied',
        'source': str(source),
        'destination': destination,
        'verified': True,
    }


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


def _manifest_artifact_hashes(manifest: dict) -> dict[str, str]:
    hashes = dict(manifest.get('artifact_hashes') or {})
    metas = [manifest.get('repo_bundle')]
    metas.extend((manifest.get('nested_repo_bundles') or {}).values())
    metas.extend((manifest.get('worktree_archives') or {}).values())
    for meta in metas:
        if not isinstance(meta, dict) or meta.get('status') != 'created':
            continue
        raw_path = str(meta.get('path') or '')
        if raw_path and meta.get('sha256'):
            hashes[Path(raw_path).name] = str(meta['sha256'])
    return hashes


def _snapshot_manifest(snapshot_dir: Path) -> tuple[dict | None, list[dict]]:
    issues: list[dict] = []
    manifest_path = snapshot_dir / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception as exc:
        return None, [{'file': 'manifest.json', 'reason': f'unreadable:{exc}'}]
    if not isinstance(manifest, dict):
        return None, [{'file': 'manifest.json', 'reason': 'not_an_object'}]
    if manifest.get('status', 'complete') != 'complete':
        issues.append({'file': 'manifest.json', 'reason': 'generation_not_complete'})
    if manifest.get('missing_required'):
        issues.append({
            'file': 'manifest.json',
            'reason': f"missing_required:{manifest.get('missing_required')}",
        })
    return manifest, issues


def _sqlite_integrity_without_sidecars(path: Path) -> str | None:
    """Check a backup copy without opening the published file in place."""
    with tempfile.TemporaryDirectory(prefix='almanac-backup-verify.') as tmp:
        isolated = Path(tmp) / path.name
        shutil.copy2(path, isolated)
        con = sqlite3.connect(f"file:{isolated}?mode=ro&immutable=1", uri=True)
        try:
            row = con.execute('PRAGMA integrity_check').fetchone()
        finally:
            con.close()
    return str(row[0]) if row else None


def _safe_tar_members(path: Path) -> bool:
    with tarfile.open(path, 'r:*') as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or '..' in member_path.parts:
                return False
            if member.issym() or member.islnk():
                return False
    return True


def verify_snapshot(
    backup_date: str | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Verify exact contents, hashes, SQLite images, bundles, and archives."""
    if backup_date is None:
        dates = sorted(
            entry.name for entry in BACKUP_DIR.iterdir()
            if entry.is_dir() and _parse_backup_date(entry.name)
        )
        if not dates:
            return {'status': 'error', 'issues': [{'reason': 'no_backup_generation'}]}
        backup_date = dates[-1]
    if not re.fullmatch(r'\d{8}', str(backup_date)) or _parse_backup_date(str(backup_date)) is None:
        return {'status': 'error', 'issues': [{'reason': 'invalid_backup_date'}]}

    snapshot_dir = BACKUP_DIR / str(backup_date)
    manifest, issues = _snapshot_manifest(snapshot_dir)
    if manifest is None:
        return {'status': 'error', 'date': str(backup_date), 'issues': issues}

    file_hashes = {str(k): str(v) for k, v in (manifest.get('hashes') or {}).items() if v}
    artifact_hashes = _manifest_artifact_hashes(manifest)
    expected_hashes = {**file_hashes, **artifact_hashes}
    expected = set(manifest.get('expected_files') or expected_hashes)
    if manifest.get('schema_version') == 2 and expected != set(expected_hashes):
        issues.append({'file': 'manifest.json', 'reason': 'unhashed_expected_file'})
    actual = {
        str(path.relative_to(snapshot_dir))
        for path in snapshot_dir.rglob('*')
        if path.is_file() and path.name != 'manifest.json'
    }
    for path in snapshot_dir.rglob('*'):
        if path.is_symlink():
            issues.append({
                'file': str(path.relative_to(snapshot_dir)),
                'reason': 'symlink_not_allowed',
            })
    if actual != expected:
        issues.append({
            'file': 'manifest.json',
            'reason': 'file_set_mismatch',
            'missing': sorted(expected - actual),
            'unexpected': sorted(actual - expected),
        })
    for rel, expected_hash in expected_hashes.items():
        path = snapshot_dir / rel
        if not path.is_file():
            continue
        if _sha256(path) != expected_hash:
            issues.append({'file': rel, 'reason': 'sha256_mismatch'})

    for rel in SQLITE_TARGETS:
        if rel not in expected:
            continue
        try:
            integrity = _sqlite_integrity_without_sidecars(snapshot_dir / rel)
            if integrity != 'ok':
                issues.append({'file': rel, 'reason': f'integrity_check:{integrity}'})
        except Exception as exc:
            issues.append({'file': rel, 'reason': f'sqlite_error:{exc}'})

    for name in ('repo.bundle', *(bundle for _, bundle in NESTED_REPOSITORIES)):
        if name not in expected:
            continue
        try:
            check = runner(
                ['git', '-C', str(BASE_DIR), 'bundle', 'verify', str(snapshot_dir / name)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            check = None
        if check is None or check.returncode != 0:
            issues.append({'file': name, 'reason': 'git_bundle_verify_failed'})
    if FRONTEND_WORKTREE_ARCHIVE in expected:
        try:
            if not _safe_tar_members(snapshot_dir / FRONTEND_WORKTREE_ARCHIVE):
                issues.append({'file': FRONTEND_WORKTREE_ARCHIVE, 'reason': 'unsafe_archive_member'})
        except Exception as exc:
            issues.append({'file': FRONTEND_WORKTREE_ARCHIVE, 'reason': f'archive_error:{exc}'})

    return {
        'status': 'ok' if not issues else 'error',
        'date': str(backup_date),
        'files_verified': len(expected_hashes),
        'issues': issues,
    }


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
    missing_required = []
    missing_optional = []
    for rel in TARGETS:
        p = BASE_DIR / rel
        if not p.exists():
            (missing_required if rel in REQUIRED_TARGETS else missing_optional).append(rel)
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
            (missing_required if rel in REQUIRED_SQLITE_TARGETS else missing_optional).append(rel)
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

    for rel in missing_required:
        broken.append({'file': rel, 'reason': 'missing_required'})

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

    backup_verification = verify_snapshot()
    if backup_verification.get('status') != 'ok':
        broken.append({
            'file': 'backups',
            'reason': 'latest_generation_verification_failed',
            'issues': backup_verification.get('issues'),
        })

    return {
        'verified_at': datetime.now().isoformat(),
        'ok':          ok,
        'broken':      broken,
        'missing_required': missing_required,
        'missing_optional': missing_optional,
        'backup_verification': backup_verification,
        'restore_suggestions': restore_suggestions,
    }


def _validated_restore_paths(backup_date: str, file_rel: str) -> tuple[Path, Path, dict] | None:
    if not re.fullmatch(r'\d{8}', backup_date) or _parse_backup_date(backup_date) is None:
        print(f'[restore] 不正な日付: {backup_date}')
        return None
    rel = Path(file_rel)
    if rel.is_absolute() or not rel.parts or '..' in rel.parts:
        print(f'[restore] 不正な相対パス: {file_rel}')
        return None

    snapshot_dir = (BACKUP_DIR / backup_date).resolve()
    backup_root = BACKUP_DIR.resolve()
    try:
        snapshot_dir.relative_to(backup_root)
    except ValueError:
        print(f'[restore] バックアップ境界外: {snapshot_dir}')
        return None
    manifest, issues = _snapshot_manifest(snapshot_dir)
    if manifest is None or issues:
        print(f'[restore] 検証済みmanifestがありません: {issues}')
        return None

    # ``restore`` is for live state only.  Repo bundles and worktree archives
    # have their own recovery procedures and must not be copied into BASE_DIR.
    expected_hashes = {
        str(k): str(v) for k, v in (manifest.get('hashes') or {}).items() if v
    }
    normalized_rel = str(rel)
    if normalized_rel not in expected_hashes:
        print(f'[restore] manifest対象外: {normalized_rel}')
        return None

    src = snapshot_dir / rel
    dst = BASE_DIR.resolve() / rel
    try:
        src.resolve().relative_to(snapshot_dir)
        dst.resolve(strict=False).relative_to(BASE_DIR.resolve())
    except ValueError:
        print(f'[restore] パス境界外: {file_rel}')
        return None
    if src.is_symlink() or not src.is_file():
        print(f'[restore] 通常ファイルではありません: {src}')
        return None
    if _sha256(src) != expected_hashes[normalized_rel]:
        print(f'[restore] hash不一致: {src}')
        return None
    return src, dst, manifest


def restore(backup_date: str, file_rel: str, *, confirm: bool = False) -> bool:
    """
    指定日のバックアップから特定ファイルを復元する（現状ファイルは .bak に退避）。
    """
    resolved = _validated_restore_paths(backup_date, file_rel)
    if resolved is None:
        return False
    src, dst, _manifest = resolved

    if not confirm:
        print(f'[restore] 確認: {src} -> {dst} ? (--yes で実行)')
        return False

    with process_lock('portfolio_ledger', timeout=30.0):
        if dst.exists():
            backup_current = dst.with_suffix(dst.suffix + '.bak')
            shutil.copy2(dst, backup_current)
            print(f'[restore] 現在ファイルを退避: {backup_current}')
        dst.parent.mkdir(exist_ok=True, parents=True)
        restore_tmp = dst.with_name(f'.{dst.name}.restore.{os.getpid()}')
        try:
            shutil.copy2(src, restore_tmp)
            os.replace(restore_tmp, dst)
        finally:
            if restore_tmp.exists():
                restore_tmp.unlink()
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
        sys.exit(0 if r.get('status') == 'complete' and r.get('published') else 1)
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
        snapshot_ok = s.get('status') == 'complete' and s.get('published') is True
        o = offsite_copy() if snapshot_ok else {
            'status': 'skipped',
            'reason': 'snapshot_not_published',
        }
        r = rotate()
        print(json.dumps({'snapshot': s, 'offsite': o, 'rotate': r}, indent=2, ensure_ascii=False))
        daily_ok = snapshot_ok and o.get('status') == 'copied' and o.get('verified') is True
        # P2-9 heartbeat
        try:
            from utils import heartbeat
            heartbeat(
                'backup_manager',
                'ok' if daily_ok else 'error',
                extra={
                    'copied': len(s['copied']),
                    'removed': len(r['removed']),
                    'offsite_status': o.get('status'),
                    'offsite_reason': o.get('reason'),
                    'offsite_destination': o.get('destination'),
                    'repo_bundle_status': (s.get('repo_bundle') or {}).get('status'),
                    'snapshot_status': s.get('status'),
                    'snapshot_published': s.get('published'),
                    'offsite_verified': o.get('verified'),
                },
            )
        except Exception:
            pass
        sys.exit(0 if daily_ok else 1)

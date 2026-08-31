from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import tarfile

import backup_manager as bm
from catalyst_outcome_catchup import run_catchup
from almanac.observability.disclosure_features import read_features
import watchdog as wd


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_backup_is_restorable_and_bundle_cloneable(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    backup_dir = root / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(bm, "BASE_DIR", root)
    monkeypatch.setattr(bm, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bm, "REQUIRED_TARGETS", [])
    monkeypatch.setattr(bm, "REQUIRED_SQLITE_TARGETS", ["nexustrader.db"])

    feature_path = root / "data" / "disclosure_features.jsonl"
    feature_path.parent.mkdir()
    feature_path.write_text(
        json.dumps({"feature_id": "f1", "ticker": "1234.T"}) + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(root / "nexustrader.db")
    con.execute("CREATE TABLE ledger_events (event_id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO ledger_events VALUES ('e1')")
    con.commit()
    con.close()

    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("restorable\n", encoding="utf-8")
    (root / "execution_reconciliation_state.json").write_text(
        json.dumps({"schema_version": 1, "corrections": [{"correction_id": "route-1"}]}),
        encoding="utf-8",
    )
    (root / "broker_position_snapshot_sbi.json").write_text(
        json.dumps({"schema_version": 1, "broker": "sbi", "complete": True}),
        encoding="utf-8",
    )
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "test")

    frontend = root / "frontend"
    frontend.mkdir()
    _git(frontend, "init")
    _git(frontend, "config", "user.email", "test@example.com")
    _git(frontend, "config", "user.name", "Test")
    (frontend / "package.json").write_text('{"name":"almanac-console"}\n', encoding="utf-8")
    _git(frontend, "add", "package.json")
    _git(frontend, "commit", "-m", "frontend")
    (frontend / "app").mkdir()
    (frontend / "app" / "page.tsx").write_text("export default function Page() {}\n", encoding="utf-8")
    (frontend / ".env.local").write_text("SECRET=do-not-archive\n", encoding="utf-8")
    (frontend / "node_modules").mkdir()
    (frontend / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")

    result = bm.snapshot(date(2026, 6, 12))
    restored = backup_dir / "20260612"

    assert result["portfolio_lock_acquired"] is True
    assert result["repo_bundle"]["status"] == "created"
    assert result["nested_repo_bundles"]["frontend"]["status"] == "created"
    assert result["worktree_archives"]["frontend"]["status"] == "created"
    assert read_features(restored / "data" / "disclosure_features.jsonl")[0]["feature_id"] == "f1"
    assert json.loads(
        (restored / "execution_reconciliation_state.json").read_text(encoding="utf-8")
    )["corrections"][0]["correction_id"] == "route-1"
    assert json.loads(
        (restored / "broker_position_snapshot_sbi.json").read_text(encoding="utf-8")
    )["complete"] is True
    assert json.loads((restored / "manifest.json").read_text(encoding="utf-8"))[
        "portfolio_lock_acquired"
    ] is True
    restored_db = sqlite3.connect(restored / "nexustrader.db")
    try:
        assert restored_db.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0] == 1
    finally:
        restored_db.close()

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(restored / "repo.bundle"), str(clone)],
        check=True,
        capture_output=True,
    )
    assert (clone / "README.md").read_text(encoding="utf-8") == "restorable\n"

    frontend_clone = tmp_path / "frontend-clone"
    subprocess.run(
        ["git", "clone", str(restored / "frontend.bundle"), str(frontend_clone)],
        check=True,
        capture_output=True,
    )
    assert (frontend_clone / "package.json").read_text(encoding="utf-8") == '{"name":"almanac-console"}\n'

    with tarfile.open(restored / "frontend_worktree.tar.gz", "r:gz") as tar:
        archive_names = set(tar.getnames())
    assert "frontend/app/page.tsx" in archive_names
    assert "frontend/package.json" in archive_names
    assert "frontend/.env.local" not in archive_names
    assert "frontend/node_modules/ignored.js" not in archive_names


def test_backup_snapshot_includes_evidence_directory_files(tmp_path, monkeypatch):
    """logs/verification_manifests/ の証跡が日次バックアップに含まれること。

    ⚠️ EVIDENCE_DIRECTORIES はディレクトリ単位で扱う。TARGETS と同じ扱いで
    shutil.copy2(src, dst) に直接渡すと IsADirectoryError で日次バックアップ
    全体が失敗する (レビューで指摘・実測)。配下のファイルを個別に再帰コピー
    し、copied/hashes へそれぞれ記録する実装を検証する。
    """
    root = tmp_path / "repo"
    root.mkdir()
    backup_dir = root / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(bm, "BASE_DIR", root)
    monkeypatch.setattr(bm, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bm, "REQUIRED_TARGETS", [])
    monkeypatch.setattr(bm, "REQUIRED_SQLITE_TARGETS", [])

    evidence_dir = root / "logs" / "verification_manifests"
    evidence_dir.mkdir(parents=True)
    manifest_36 = {"verification": "news_topic isolated live run (round 36)",
                   "run_status": "success"}
    manifest_37 = {"verification": "news_topic isolated live run (round 37)",
                   "run_status": "success"}
    (evidence_dir / "round36_manifest.json").write_text(
        json.dumps(manifest_36), encoding="utf-8")
    (evidence_dir / "round37_manifest.json").write_text(
        json.dumps(manifest_37), encoding="utf-8")

    result = bm.snapshot(date(2026, 8, 29))
    restored = backup_dir / "20260829"

    expected = {
        "logs/verification_manifests/round36_manifest.json",
        "logs/verification_manifests/round37_manifest.json",
    }
    assert expected.issubset(set(result["copied"]))
    assert all(result["hashes"].get(rel) for rel in expected), (
        "証跡ファイルの hash が manifest.json 用に記録されていない")

    assert json.loads(
        (restored / "logs" / "verification_manifests" / "round36_manifest.json")
        .read_text(encoding="utf-8")
    ) == manifest_36
    assert json.loads(
        (restored / "logs" / "verification_manifests" / "round37_manifest.json")
        .read_text(encoding="utf-8")
    ) == manifest_37

    # 改竄検知用 manifest.json にも同じ2ファイルが載ること。
    top_manifest = json.loads((restored / "manifest.json").read_text(encoding="utf-8"))
    assert expected.issubset(set(top_manifest["files"]))
    assert expected.issubset(set(top_manifest["hashes"]))


def test_backup_snapshot_survives_a_missing_evidence_directory(tmp_path, monkeypatch):
    """証跡ディレクトリが無い日でも日次バックアップ自体は失敗しない。"""
    root = tmp_path / "repo"
    root.mkdir()
    backup_dir = root / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(bm, "BASE_DIR", root)
    monkeypatch.setattr(bm, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bm, "REQUIRED_TARGETS", [])
    monkeypatch.setattr(bm, "REQUIRED_SQLITE_TARGETS", [])
    # logs/verification_manifests/ を意図的に作らない。

    result = bm.snapshot(date(2026, 8, 30))

    assert "logs/verification_manifests" in result["missing"]
    assert result["portfolio_lock_acquired"] is True
    assert not any(
        rel.startswith("logs/verification_manifests") for rel in result["copied"]
    )


def test_offsite_skips_when_rclone_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "BACKUP_DIR", tmp_path)
    (tmp_path / "20260612").mkdir()
    monkeypatch.setattr(bm.shutil, "which", lambda _: None)
    monkeypatch.setattr(bm, "RCLONE_FALLBACK_PATHS", ())
    monkeypatch.setattr(bm, "verify_snapshot", lambda *_a, **_k: {"status": "ok"})

    result = bm.offsite_copy(date(2026, 6, 12))

    assert result == {"status": "skipped", "reason": "rclone_not_installed"}


def test_offsite_finds_homebrew_rclone_when_cron_path_is_minimal(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "BACKUP_DIR", tmp_path)
    (tmp_path / "20260612").mkdir()
    monkeypatch.setattr(bm.shutil, "which", lambda _: None)
    monkeypatch.setattr(bm, "verify_snapshot", lambda *_a, **_k: {"status": "ok"})
    original_exists = Path.exists

    def fake_exists(path):
        if str(path) == "/opt/homebrew/bin/rclone":
            return True
        return original_exists(path)

    commands = []

    def fake_runner(cmd, **kwargs):
        commands.append(cmd)
        if cmd[1] == "listremotes":
            return subprocess.CompletedProcess(cmd, 0, stdout="crypt-gdrive:\n", stderr="")
        if cmd[1] in {"sync", "check"}:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(Path, "exists", fake_exists)

    result = bm.offsite_copy(date(2026, 6, 12), runner=fake_runner)

    assert result["status"] == "copied"
    assert result["verified"] is True
    assert commands[0][0] == "/opt/homebrew/bin/rclone"
    assert commands[1][0] == "/opt/homebrew/bin/rclone"
    assert commands[1][1] == "sync"
    assert commands[2][1] == "check"


def test_offsite_never_reports_copied_when_remote_check_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "BACKUP_DIR", tmp_path)
    (tmp_path / "20260612").mkdir()
    monkeypatch.setattr(bm, "verify_snapshot", lambda *_a, **_k: {"status": "ok"})
    monkeypatch.setattr(bm, "_find_rclone", lambda: "/usr/bin/rclone")

    def fake_runner(cmd, **kwargs):
        if cmd[1] == "listremotes":
            return subprocess.CompletedProcess(cmd, 0, stdout="crypt-gdrive:\n", stderr="")
        if cmd[1] == "sync":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[1] == "check":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="hash mismatch")
        raise AssertionError(cmd)

    result = bm.offsite_copy(date(2026, 6, 12), runner=fake_runner)

    assert result["status"] == "error"
    assert "remote_verification_failed" in result["reason"]


def test_snapshot_rerun_replaces_generation_without_stale_files(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    backup_dir = root / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(bm, "BASE_DIR", root)
    monkeypatch.setattr(bm, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bm, "TARGETS", ["account.json", "optional.json"])
    monkeypatch.setattr(bm, "REQUIRED_TARGETS", ["account.json"])
    monkeypatch.setattr(bm, "SQLITE_TARGETS", [])
    monkeypatch.setattr(bm, "REQUIRED_SQLITE_TARGETS", [])
    monkeypatch.setattr(bm, "EVIDENCE_DIRECTORIES", [])
    (root / "account.json").write_text('{"cash": 1}', encoding="utf-8")
    (root / "optional.json").write_text('{"old": true}', encoding="utf-8")

    first = bm.snapshot(date(2026, 8, 31))
    assert first["published"] is True
    (root / "optional.json").unlink()
    (root / "account.json").write_text('{"cash": 2}', encoding="utf-8")
    second = bm.snapshot(date(2026, 8, 31))

    published = backup_dir / "20260831"
    assert second["published"] is True
    assert not (published / "optional.json").exists()
    assert json.loads((published / "account.json").read_text()) == {"cash": 2}
    assert not list(backup_dir.glob(".20260831.previous.*"))


def test_incomplete_rerun_cannot_replace_a_complete_generation(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    backup_dir = root / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(bm, "BASE_DIR", root)
    monkeypatch.setattr(bm, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bm, "TARGETS", ["account.json"])
    monkeypatch.setattr(bm, "REQUIRED_TARGETS", ["account.json"])
    monkeypatch.setattr(bm, "SQLITE_TARGETS", [])
    monkeypatch.setattr(bm, "REQUIRED_SQLITE_TARGETS", [])
    monkeypatch.setattr(bm, "EVIDENCE_DIRECTORIES", [])
    account = root / "account.json"
    account.write_text('{"cash": 1}', encoding="utf-8")
    assert bm.snapshot(date(2026, 8, 31))["published"] is True
    original_hash = bm._sha256(backup_dir / "20260831" / "account.json")
    account.unlink()

    failed = bm.snapshot(date(2026, 8, 31))

    assert failed["status"] == "incomplete"
    assert failed["published"] is False
    assert bm._sha256(backup_dir / "20260831" / "account.json") == original_hash


def test_verify_snapshot_detects_extra_and_modified_files(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    backup_dir = root / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(bm, "BASE_DIR", root)
    monkeypatch.setattr(bm, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bm, "TARGETS", ["account.json"])
    monkeypatch.setattr(bm, "REQUIRED_TARGETS", ["account.json"])
    monkeypatch.setattr(bm, "SQLITE_TARGETS", [])
    monkeypatch.setattr(bm, "REQUIRED_SQLITE_TARGETS", [])
    monkeypatch.setattr(bm, "EVIDENCE_DIRECTORIES", [])
    (root / "account.json").write_text('{"cash": 1}', encoding="utf-8")
    bm.snapshot(date(2026, 8, 31))
    published = backup_dir / "20260831"
    assert bm.verify_snapshot("20260831")["status"] == "ok"

    (published / "unexpected.txt").write_text("stale", encoding="utf-8")
    assert bm.verify_snapshot("20260831")["status"] == "error"
    (published / "unexpected.txt").unlink()
    (published / "account.json").write_text('{"cash": 999}', encoding="utf-8")
    result = bm.verify_snapshot("20260831")
    assert result["status"] == "error"
    assert any(row.get("reason") == "sha256_mismatch" for row in result["issues"])


def test_restore_rejects_traversal_and_dry_run_has_no_side_effect(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    backup_dir = root / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(bm, "BASE_DIR", root)
    monkeypatch.setattr(bm, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(bm, "TARGETS", ["account.json"])
    monkeypatch.setattr(bm, "REQUIRED_TARGETS", ["account.json"])
    monkeypatch.setattr(bm, "SQLITE_TARGETS", [])
    monkeypatch.setattr(bm, "REQUIRED_SQLITE_TARGETS", [])
    monkeypatch.setattr(bm, "EVIDENCE_DIRECTORIES", [])
    account = root / "account.json"
    account.write_text('{"cash": 1}', encoding="utf-8")
    bm.snapshot(date(2026, 8, 31))
    account.write_text('{"cash": 2}', encoding="utf-8")

    assert bm.restore("../20260831", "account.json", confirm=True) is False
    assert bm.restore("20260831", "../outside.json", confirm=True) is False
    assert bm.restore("20260831", "account.json", confirm=False) is False
    assert not (root / "account.json.bak").exists()
    assert json.loads(account.read_text()) == {"cash": 2}

    assert bm.restore("20260831", "account.json", confirm=True) is True
    assert json.loads(account.read_text()) == {"cash": 1}
    assert json.loads((root / "account.json.bak").read_text()) == {"cash": 2}


def test_outcome_catchup_requires_explicit_apply(tmp_path):
    result = run_catchup(root=tmp_path, today=date(2026, 6, 12))

    assert result["status"] == "review_required"
    assert not (tmp_path / "catalyst_outcome_log.jsonl").exists()


def test_watchdog_detects_stale_new_lanes(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "BASE_DIR", tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    stale = datetime(2026, 6, 8, 12, 0, 0)
    now = datetime(2026, 6, 12, 12, 0, 0)
    (tmp_path / "catalyst_outcome_log.jsonl").write_text(
        json.dumps({"measured_at": stale.isoformat()}) + "\n"
    )
    (tmp_path / "sell_outcome_log.jsonl").write_text(
        json.dumps({"measured_at": stale.isoformat()}) + "\n"
    )
    (data / "disclosure_features.jsonl").write_text(
        json.dumps({"ingest_time": stale.isoformat()}) + "\n"
    )
    (data / "disclosure_shadow_book.json").write_text(
        json.dumps({"generated_at": stale.isoformat()})
    )

    assert len(wd._check_outcome_logs(now)) == 2
    assert len(wd._check_disclosure_freshness(now)) == 1
    assert len(wd._check_shadow_book(now)) == 1


def test_watchdog_recent_new_lanes_are_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "BASE_DIR", tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    recent = datetime(2026, 6, 11, 12, 0, 0)
    now = datetime(2026, 6, 12, 12, 0, 0)
    for name in ("catalyst_outcome_log.jsonl", "sell_outcome_log.jsonl"):
        (tmp_path / name).write_text(json.dumps({"measured_at": recent.isoformat()}) + "\n")
    (data / "disclosure_features.jsonl").write_text(
        json.dumps({"ingest_time": recent.isoformat()}) + "\n"
    )
    (data / "disclosure_shadow_book.json").write_text(
        json.dumps({"generated_at": recent.isoformat()})
    )

    assert wd._check_outcome_logs(now) == []
    assert wd._check_disclosure_freshness(now) == []
    assert wd._check_shadow_book(now) == []


def test_watchdog_disk_thresholds(monkeypatch):
    usage_type = type("usage", (), {})
    warning = usage_type()
    warning.free = 10 * 1024 ** 3
    monkeypatch.setattr(wd.shutil, "disk_usage", lambda _: warning)
    assert wd._check_disk_space()[0]["severity"] == "warning"

    critical = usage_type()
    critical.free = 7 * 1024 ** 3
    monkeypatch.setattr(wd.shutil, "disk_usage", lambda _: critical)
    assert wd._check_disk_space()[0]["severity"] == "critical"

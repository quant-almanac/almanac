from __future__ import annotations

from datetime import date, datetime, timedelta
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

    assert result["repo_bundle"]["status"] == "created"
    assert result["nested_repo_bundles"]["frontend"]["status"] == "created"
    assert result["worktree_archives"]["frontend"]["status"] == "created"
    assert read_features(restored / "data" / "disclosure_features.jsonl")[0]["feature_id"] == "f1"
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


def test_offsite_skips_when_rclone_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "BACKUP_DIR", tmp_path)
    (tmp_path / "20260612").mkdir()
    monkeypatch.setattr(bm.shutil, "which", lambda _: None)
    monkeypatch.setattr(bm, "RCLONE_FALLBACK_PATHS", ())

    result = bm.offsite_copy(date(2026, 6, 12))

    assert result == {"status": "skipped", "reason": "rclone_not_installed"}


def test_offsite_finds_homebrew_rclone_when_cron_path_is_minimal(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "BACKUP_DIR", tmp_path)
    (tmp_path / "20260612").mkdir()
    monkeypatch.setattr(bm.shutil, "which", lambda _: None)
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
        if cmd[1] == "copy":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(Path, "exists", fake_exists)

    result = bm.offsite_copy(date(2026, 6, 12), runner=fake_runner)

    assert result["status"] == "copied"
    assert commands[0][0] == "/opt/homebrew/bin/rclone"
    assert commands[1][0] == "/opt/homebrew/bin/rclone"


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

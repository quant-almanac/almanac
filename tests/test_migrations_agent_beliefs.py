"""Tests for almanac.migrations.agent_beliefs_v1_to_v2.

The migration is a one-shot, production-touching script — it had better be
correct, idempotent, and refuse to migrate a malformed file rather than
half-migrate it. Coverage focuses on:

- Round-trip on a realistic v1 fixture (top-level shape preserved).
- Idempotency (re-run is a no-op).
- Backup file is created with a non-clobbering timestamped name.
- ``conviction_score`` is preserved (additive migration, Codex R11-D).
- Top-level ``version`` (semantic) and new ``schema_version`` (storage)
  do not collide.
- Refusal to migrate structurally invalid files.
- CLI entry point works end-to-end.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.migrations.agent_beliefs_v1_to_v2 import (  # noqa: E402
    TARGET_SCHEMA_VERSION,
    MigrationResult,
    _main,
    migrate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _v1_fixture(n_beliefs: int = 3) -> dict:
    """Return a v1-shaped beliefs payload with ``n_beliefs`` entries."""
    return {
        "beliefs": [
            {
                "id": f"id{i:04d}",
                "ticker": "NVDA" if i % 2 == 0 else "9984.T",
                "theme": "opportunity",
                "conviction_score": 55 + i,
                "rationale": f"rationale {i} — 上方修正",  # Japanese OK
                "source_agent": "opus_synthesis",
                "evidence": "evidence text",
                "created_at": "2026-04-11T01:15:47.029547",
                "last_updated": "2026-04-11T01:15:47.029547",
                "expires_at": "2026-05-11T01:15:47",
            }
            for i in range(n_beliefs)
        ],
        "last_updated": "2026-04-11T01:15:47.029547",
        "version": "1.0",  # semantic content version (NOT schema_version)
    }


@pytest.fixture
def v1_file(tmp_path: Path) -> Path:
    """A clean v1 agent_beliefs.json with 3 beliefs."""
    p = tmp_path / "agent_beliefs.json"
    p.write_text(json.dumps(_v1_fixture(3), ensure_ascii=False, indent=2))
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_migrate_returns_summary(v1_file: Path) -> None:
    result = migrate(v1_file)
    assert isinstance(result, MigrationResult)
    assert result.migrated is True
    assert result.beliefs_migrated == 3
    assert result.skipped == 0
    assert result.schema_version_after == TARGET_SCHEMA_VERSION
    assert result.backup_path is not None
    assert result.backup_path.exists()


def test_migrate_adds_three_fields_per_belief(v1_file: Path) -> None:
    migrate(v1_file)
    data = json.loads(v1_file.read_text())
    for b in data["beliefs"]:
        assert "base_conviction" in b
        assert "adjusted_conviction" in b
        assert "adjustment_log" in b
        assert b["base_conviction"] == b["conviction_score"]
        assert b["adjusted_conviction"] == b["base_conviction"]
        assert b["adjustment_log"] == []


def test_migrate_preserves_conviction_score_for_backward_compat(v1_file: Path) -> None:
    """Codex R11-D — analyst/__init__.py has ~14 readers of conviction_score.

    The migration MUST keep the field in place; dropping it would break
    legacy callers immediately. A future v2→v3 can drop it after callers
    are rewritten.
    """
    original = json.loads(v1_file.read_text())
    original_scores = {b["id"]: b["conviction_score"] for b in original["beliefs"]}

    migrate(v1_file)

    migrated = json.loads(v1_file.read_text())
    for b in migrated["beliefs"]:
        assert b["conviction_score"] == original_scores[b["id"]], (
            "conviction_score must be preserved verbatim (additive migration)"
        )


def test_migrate_preserves_top_level_version_field(v1_file: Path) -> None:
    """Semantic ``version`` and storage ``schema_version`` are distinct."""
    migrate(v1_file)
    data = json.loads(v1_file.read_text())
    assert data["version"] == "1.0", "top-level version (semantic) untouched"
    assert data["schema_version"] == TARGET_SCHEMA_VERSION
    # And the two keys must not be conflated.
    assert "version" in data and "schema_version" in data


def test_migrate_preserves_unrelated_belief_fields(v1_file: Path) -> None:
    """Round-trip every non-migration field exactly."""
    original = json.loads(v1_file.read_text())
    migrate(v1_file)
    migrated = json.loads(v1_file.read_text())

    for orig, post in zip(original["beliefs"], migrated["beliefs"]):
        for k in (
            "id", "ticker", "theme", "rationale", "source_agent",
            "evidence", "created_at", "expires_at",
        ):
            assert post[k] == orig[k], f"field {k!r} mutated unexpectedly"


def test_migrate_handles_japanese_text(v1_file: Path) -> None:
    """ensure_ascii=False round-trip keeps headlines readable in the file."""
    migrate(v1_file)
    raw = v1_file.read_text(encoding="utf-8")
    assert "上方修正" in raw  # not escaped to \uXXXX


def test_migrate_updates_last_updated(v1_file: Path) -> None:
    original = json.loads(v1_file.read_text())
    migrate(v1_file)
    migrated = json.loads(v1_file.read_text())
    assert migrated["last_updated"] != original["last_updated"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_migrate_is_idempotent(v1_file: Path) -> None:
    first = migrate(v1_file)
    second = migrate(v1_file)
    assert first.migrated is True
    assert second.migrated is False
    assert second.beliefs_migrated == 0
    assert second.backup_path is None
    assert second.schema_version_after == TARGET_SCHEMA_VERSION


def test_idempotent_run_does_not_modify_file(v1_file: Path) -> None:
    migrate(v1_file)
    after_first = v1_file.read_text()
    # Second run must not rewrite the file.
    mtime_after_first = v1_file.stat().st_mtime_ns
    migrate(v1_file)
    assert v1_file.read_text() == after_first
    assert v1_file.stat().st_mtime_ns == mtime_after_first


def test_idempotent_run_creates_no_extra_backup(tmp_path: Path, v1_file: Path) -> None:
    migrate(v1_file)
    backups_after_first = list(tmp_path.glob("agent_beliefs.json.v1.bak.*"))
    migrate(v1_file)
    backups_after_second = list(tmp_path.glob("agent_beliefs.json.v1.bak.*"))
    assert backups_after_first == backups_after_second


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def test_backup_contains_original_data(v1_file: Path) -> None:
    original = v1_file.read_text()
    result = migrate(v1_file)
    assert result.backup_path is not None
    assert result.backup_path.read_text() == original


def test_backup_filename_includes_v1_bak_and_timestamp(v1_file: Path) -> None:
    result = migrate(v1_file)
    assert result.backup_path is not None
    name = result.backup_path.name
    assert ".v1.bak." in name
    # Timestamp should end with 'Z' (UTC marker).
    assert name.endswith("Z")


# ---------------------------------------------------------------------------
# Atomic write — no .tmp file should linger
# ---------------------------------------------------------------------------


def test_no_tmp_file_remains_after_success(tmp_path: Path, v1_file: Path) -> None:
    migrate(v1_file)
    tmp_files = list(tmp_path.glob("agent_beliefs.json.tmp"))
    assert tmp_files == []


# ---------------------------------------------------------------------------
# Refusal paths (we'd rather fail than half-migrate)
# ---------------------------------------------------------------------------


def test_migrate_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        migrate(tmp_path / "nope.json")


def test_migrate_rejects_non_dict_top_level(tmp_path: Path) -> None:
    p = tmp_path / "agent_beliefs.json"
    p.write_text(json.dumps([{"id": "x", "conviction_score": 50}]))
    with pytest.raises(ValueError, match="top-level must be dict"):
        migrate(p)


def test_migrate_rejects_missing_beliefs_list(tmp_path: Path) -> None:
    p = tmp_path / "agent_beliefs.json"
    p.write_text(json.dumps({"last_updated": "x", "version": "1.0"}))
    with pytest.raises(ValueError, match="missing or non-list 'beliefs'"):
        migrate(p)


def test_migrate_rejects_beliefs_not_a_list(tmp_path: Path) -> None:
    p = tmp_path / "agent_beliefs.json"
    p.write_text(json.dumps({"beliefs": {"id": "x"}, "version": "1.0"}))
    with pytest.raises(ValueError, match="missing or non-list 'beliefs'"):
        migrate(p)


def test_migrate_rejects_belief_missing_id(tmp_path: Path) -> None:
    payload = _v1_fixture(2)
    del payload["beliefs"][1]["id"]
    p = tmp_path / "agent_beliefs.json"
    p.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="missing required field 'id'"):
        migrate(p)


def test_migrate_rejects_belief_missing_conviction_score(tmp_path: Path) -> None:
    payload = _v1_fixture(2)
    del payload["beliefs"][0]["conviction_score"]
    p = tmp_path / "agent_beliefs.json"
    p.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="missing required field 'conviction_score'"):
        migrate(p)


def test_migrate_rejects_float_conviction_score(tmp_path: Path) -> None:
    payload = _v1_fixture(1)
    payload["beliefs"][0]["conviction_score"] = 55.5
    p = tmp_path / "agent_beliefs.json"
    p.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="conviction_score is float"):
        migrate(p)


def test_failed_validation_does_not_write_backup(tmp_path: Path) -> None:
    """Validation runs BEFORE the backup so a malformed file leaves no
    stray .bak files."""
    payload = _v1_fixture(2)
    del payload["beliefs"][0]["id"]
    p = tmp_path / "agent_beliefs.json"
    p.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        migrate(p)
    assert list(tmp_path.glob("*.v1.bak.*")) == []


def test_failed_validation_leaves_file_unchanged(tmp_path: Path) -> None:
    payload = _v1_fixture(2)
    del payload["beliefs"][0]["id"]
    p = tmp_path / "agent_beliefs.json"
    original = json.dumps(payload)
    p.write_text(original)
    with pytest.raises(ValueError):
        migrate(p)
    assert p.read_text() == original


# ---------------------------------------------------------------------------
# Production file shape compatibility
# ---------------------------------------------------------------------------


_PRODUCTION_FILE = _REPO_ROOT / "beliefs" / "agent_beliefs.json"


@pytest.mark.skipif(
    not _PRODUCTION_FILE.exists(),
    reason="production agent_beliefs.json not available in this checkout",
)
def test_real_production_file_shape_is_migratable(tmp_path: Path) -> None:
    """Codex R11-D — verify we can migrate the actual production file
    (against a temp copy, never touching the real one)."""
    copy = tmp_path / "agent_beliefs.json"
    shutil.copy2(_PRODUCTION_FILE, copy)
    result = migrate(copy)
    data = json.loads(copy.read_text(encoding="utf-8"))
    assert data["schema_version"] == TARGET_SCHEMA_VERSION
    if result.migrated:
        assert result.beliefs_migrated > 0
    else:
        # Production may already be v2; migration must be a no-op then.
        assert result.beliefs_migrated == 0
    # Every belief in the real file must now have the three new fields.
    for b in data["beliefs"]:
        assert "base_conviction" in b
        assert "adjusted_conviction" in b
        assert b["adjustment_log"] == []
        # And the v1 alias survives.
        assert b["conviction_score"] == b["base_conviction"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_runs_migration(v1_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = _main([str(v1_file)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "migrated 3 beliefs" in out
    assert "schema_version_after=2" in out


def test_cli_reports_noop_on_already_migrated(
    v1_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _main([str(v1_file)])
    capsys.readouterr()  # discard first-run output
    exit_code = _main([str(v1_file)])
    assert exit_code == 0
    assert "no-op" in capsys.readouterr().out


def test_cli_returns_2_on_validation_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": "1.0"}))  # missing beliefs
    exit_code = _main([str(bad)])
    assert exit_code == 2
    assert "migration failed" in capsys.readouterr().err


def test_cli_returns_2_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _main([str(tmp_path / "nope.json")])
    assert exit_code == 2

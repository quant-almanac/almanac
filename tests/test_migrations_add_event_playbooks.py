"""Tests for almanac.migrations.add_event_playbooks.

The migration is a one-shot data change against ``event_playbook.json``.
Tests pin down the exact invariants we want a re-run / future similar
migration to inherit:

- Migration creates the file when absent.
- Migration appends to an existing file.
- Idempotent re-runs are no-ops.
- Backup written with non-clobbering timestamp (only when file pre-existed).
- Atomic write: no ``.tmp`` residue.
- Validation rejects non-conforming files instead of half-migrating.
- Both new playbooks carry Round 11 #C feature flags
  (``enabled_for_decision`` true / ``observe_only`` false).
- Every buy entry has ``allocation_amount`` + ``currency`` (R12 P1 #2).
- CLI runs, prints summary, exits 0.
- CLI returns 2 on validation failure.
- CLI returns 2 on missing file (only when FileNotFoundError path is hit).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.migrations.add_event_playbooks import (  # noqa: E402
    EARNINGS_REVISION_DRIFT_ID,
    EARNINGS_REVISION_DRIFT_PLAYBOOK,
    IPO_PROXY_EVENT_ID,
    IPO_PROXY_EVENT_PLAYBOOK,
    MigrationResult,
    _main,
    migrate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stub_event_playbook_file(tmp_path: Path, n_existing: int = 2) -> Path:
    """Realistic-shape stub with ``n_existing`` playbooks (not the real ones)."""
    payload = {
        "version": "1.0",
        "description": "event_playbook stub",
        "updated_at": "2026-05-01T00:00:00",
        "playbooks": [
            {
                "id": f"stub_playbook_{i}",
                "name": f"stub {i}",
                "detect": {"news_keywords": [], "indicators": {}, "min_signals": 1},
                "actions": {},
            }
            for i in range(n_existing)
        ],
    }
    p = tmp_path / "event_playbook.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return p


# ---------------------------------------------------------------------------
# File-creation when absent
# ---------------------------------------------------------------------------


def test_migrate_creates_file_when_absent(tmp_path: Path) -> None:
    p = tmp_path / "event_playbook.json"
    assert not p.exists()
    result = migrate(p)
    assert p.exists()
    assert result.migrated is True
    assert result.playbooks_after == 2


def test_migrate_created_file_has_both_playbooks(tmp_path: Path) -> None:
    p = tmp_path / "event_playbook.json"
    migrate(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    ids = [pb["id"] for pb in data["playbooks"]]
    assert IPO_PROXY_EVENT_ID in ids
    assert EARNINGS_REVISION_DRIFT_ID in ids


def test_migrate_created_file_has_correct_version(tmp_path: Path) -> None:
    p = tmp_path / "event_playbook.json"
    migrate(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == "1.0"
    assert "playbooks" in data
    assert isinstance(data["playbooks"], list)


def test_migrate_no_backup_when_file_absent(tmp_path: Path) -> None:
    """No backup should be written when the file didn't exist before."""
    p = tmp_path / "event_playbook.json"
    result = migrate(p)
    assert result.backup_path is None
    assert list(tmp_path.glob("*.bak.*")) == []


# ---------------------------------------------------------------------------
# Migration core (file pre-exists)
# ---------------------------------------------------------------------------


def test_migrate_returns_summary(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path, n_existing=2)
    result = migrate(p)
    assert isinstance(result, MigrationResult)
    assert result.migrated is True
    assert result.playbooks_after == 4  # 2 existing + 2 new
    assert result.backup_path is not None
    assert result.backup_path.exists()


def test_migrate_appends_both_playbooks(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path, n_existing=2)
    migrate(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    ids = [pb["id"] for pb in data["playbooks"]]
    assert IPO_PROXY_EVENT_ID in ids
    assert EARNINGS_REVISION_DRIFT_ID in ids
    assert ids.count(IPO_PROXY_EVENT_ID) == 1
    assert ids.count(EARNINGS_REVISION_DRIFT_ID) == 1


def test_migrate_preserves_existing_playbooks_verbatim(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path, n_existing=3)
    original = json.loads(p.read_text(encoding="utf-8"))
    migrate(p)
    migrated = json.loads(p.read_text(encoding="utf-8"))
    # First 3 entries unchanged.
    assert migrated["playbooks"][:3] == original["playbooks"]


def test_migrate_preserves_top_level_fields(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path)
    original = json.loads(p.read_text(encoding="utf-8"))
    migrate(p)
    migrated = json.loads(p.read_text(encoding="utf-8"))
    assert migrated["version"] == original["version"]
    assert migrated["description"] == original["description"]
    # updated_at IS allowed to change.


def test_migrate_updates_updated_at(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path)
    original_ts = json.loads(p.read_text(encoding="utf-8"))["updated_at"]
    migrate(p)
    after_ts = json.loads(p.read_text(encoding="utf-8"))["updated_at"]
    assert after_ts != original_ts


# ---------------------------------------------------------------------------
# Partial presence (one playbook already in file)
# ---------------------------------------------------------------------------


def test_migrate_appends_only_missing_playbook(tmp_path: Path) -> None:
    """If only one of the two playbooks is absent, only that one is added."""
    payload = {
        "version": "1.0",
        "description": "partial",
        "updated_at": "2026-05-01T00:00:00",
        "playbooks": [json.loads(json.dumps(IPO_PROXY_EVENT_PLAYBOOK))],
    }
    p = tmp_path / "event_playbook.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    result = migrate(p)
    assert result.migrated is True
    assert result.playbooks_after == 2
    data = json.loads(p.read_text(encoding="utf-8"))
    ids = [pb["id"] for pb in data["playbooks"]]
    assert ids.count(IPO_PROXY_EVENT_ID) == 1
    assert EARNINGS_REVISION_DRIFT_ID in ids


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path)
    first = migrate(p)
    second = migrate(p)
    assert first.migrated is True
    assert second.migrated is False
    assert second.backup_path is None
    assert second.playbooks_after == first.playbooks_after


def test_idempotent_run_creates_no_extra_backup(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path)
    migrate(p)
    backups_first = sorted(tmp_path.glob("*.bak.*"))
    migrate(p)
    backups_second = sorted(tmp_path.glob("*.bak.*"))
    assert backups_first == backups_second


def test_idempotent_run_does_not_rewrite_file(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path)
    migrate(p)
    after_first = p.read_text()
    mtime = p.stat().st_mtime_ns
    migrate(p)
    assert p.read_text() == after_first
    assert p.stat().st_mtime_ns == mtime


def test_idempotent_on_created_file(tmp_path: Path) -> None:
    """Re-running on a newly-created file is also a no-op."""
    p = tmp_path / "event_playbook.json"
    first = migrate(p)
    second = migrate(p)
    assert first.migrated is True
    assert second.migrated is False
    assert second.playbooks_after == 2


# ---------------------------------------------------------------------------
# Atomic / backup discipline
# ---------------------------------------------------------------------------


def test_no_tmp_file_remains_after_success(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path)
    migrate(p)
    assert not (tmp_path / "event_playbook.json.tmp").exists()


def test_no_tmp_file_remains_after_create(tmp_path: Path) -> None:
    p = tmp_path / "event_playbook.json"
    migrate(p)
    assert not (tmp_path / "event_playbook.json.tmp").exists()


def test_backup_contains_original_data(tmp_path: Path) -> None:
    p = _stub_event_playbook_file(tmp_path)
    original = p.read_text()
    result = migrate(p)
    assert result.backup_path is not None
    assert result.backup_path.read_text() == original


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_migrate_rejects_non_dict_top_level(tmp_path: Path) -> None:
    p = tmp_path / "event_playbook.json"
    p.write_text(json.dumps([{"id": "x"}]))
    with pytest.raises(ValueError, match="'playbooks' field"):
        migrate(p)


def test_migrate_rejects_missing_playbooks_field(tmp_path: Path) -> None:
    p = tmp_path / "event_playbook.json"
    p.write_text(json.dumps({"version": "1.0"}))
    with pytest.raises(ValueError, match="'playbooks' field"):
        migrate(p)


def test_migrate_rejects_playbooks_not_a_list(tmp_path: Path) -> None:
    p = tmp_path / "event_playbook.json"
    p.write_text(json.dumps({"playbooks": {"id": "x"}, "version": "1.0"}))
    with pytest.raises(ValueError, match="'playbooks' must be a list"):
        migrate(p)


def test_validation_failure_leaves_no_backup(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"version": "1.0"}))
    with pytest.raises(ValueError):
        migrate(p)
    assert list(tmp_path.glob("*.bak.*")) == []


def test_validation_failure_leaves_no_tmp(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"version": "1.0"}))
    with pytest.raises(ValueError):
        migrate(p)
    assert not (tmp_path / "bad.json.tmp").exists()


# ---------------------------------------------------------------------------
# IPO_PROXY_EVENT_PLAYBOOK shape (Round 11 #C + plan §5 step 5)
# ---------------------------------------------------------------------------


def test_ipo_proxy_carries_round11_feature_flags() -> None:
    assert IPO_PROXY_EVENT_PLAYBOOK["enabled_for_decision"] is True
    assert IPO_PROXY_EVENT_PLAYBOOK["observe_only"] is False


def test_ipo_proxy_id_matches_constant() -> None:
    assert IPO_PROXY_EVENT_PLAYBOOK["id"] == IPO_PROXY_EVENT_ID
    assert IPO_PROXY_EVENT_ID == "ipo_proxy_event"


def test_ipo_proxy_has_two_phase_actions() -> None:
    actions = IPO_PROXY_EVENT_PLAYBOOK["actions"]
    assert set(actions.keys()) == {"phase_1_seed_only", "phase_2_llm_confirmed"}
    for phase in actions.values():
        assert "buy" in phase
        assert isinstance(phase["buy"], list)
        assert phase["buy"]  # non-empty


def test_ipo_proxy_detect_min_signals_two() -> None:
    assert IPO_PROXY_EVENT_PLAYBOOK["detect"]["min_signals"] == 2


def test_ipo_proxy_detect_has_vix_indicator() -> None:
    indicators = IPO_PROXY_EVENT_PLAYBOOK["detect"]["indicators"]
    assert "vix" in indicators
    assert indicators["vix"]["threshold"] == 30


def test_ipo_proxy_detect_has_self_consistency_indicator() -> None:
    indicators = IPO_PROXY_EVENT_PLAYBOOK["detect"]["indicators"]
    assert "proxy_self_consistency" in indicators
    assert indicators["proxy_self_consistency"]["threshold"] == 0.5


def test_ipo_proxy_every_buy_has_allocation_amount_and_currency() -> None:
    """Codex Round 12 P1 #2."""
    actions = IPO_PROXY_EVENT_PLAYBOOK["actions"]
    for phase_name, phase in actions.items():
        for entry in phase.get("buy", []):
            assert "allocation_amount" in entry, (
                f"{phase_name}/{entry.get('ticker')} missing allocation_amount"
            )
            assert "currency" in entry, (
                f"{phase_name}/{entry.get('ticker')} missing currency"
            )
            assert entry["currency"] in {"USD", "JPY"}, entry
            assert "allocation_usd" not in entry, (
                f"{phase_name}/{entry.get('ticker')} still uses deprecated allocation_usd"
            )


def test_ipo_proxy_constant_not_mutated_by_migration(tmp_path: Path) -> None:
    before = json.dumps(IPO_PROXY_EVENT_PLAYBOOK, sort_keys=True, ensure_ascii=False)
    p = tmp_path / "event_playbook.json"
    migrate(p)
    after = json.dumps(IPO_PROXY_EVENT_PLAYBOOK, sort_keys=True, ensure_ascii=False)
    assert before == after


# ---------------------------------------------------------------------------
# EARNINGS_REVISION_DRIFT_PLAYBOOK shape
# ---------------------------------------------------------------------------


def test_earnings_drift_carries_round11_feature_flags() -> None:
    assert EARNINGS_REVISION_DRIFT_PLAYBOOK["enabled_for_decision"] is True
    assert EARNINGS_REVISION_DRIFT_PLAYBOOK["observe_only"] is False


def test_earnings_drift_id_matches_constant() -> None:
    assert EARNINGS_REVISION_DRIFT_PLAYBOOK["id"] == EARNINGS_REVISION_DRIFT_ID
    assert EARNINGS_REVISION_DRIFT_ID == "earnings_revision_drift"


def test_earnings_drift_has_two_phase_actions() -> None:
    actions = EARNINGS_REVISION_DRIFT_PLAYBOOK["actions"]
    assert set(actions.keys()) == {"phase_1_initial", "phase_2_drift_continuation"}
    for phase in actions.values():
        assert "buy" in phase
        assert isinstance(phase["buy"], list)
        assert phase["buy"]  # non-empty


def test_earnings_drift_detect_min_signals_three() -> None:
    assert EARNINGS_REVISION_DRIFT_PLAYBOOK["detect"]["min_signals"] == 3


def test_earnings_drift_detect_has_surprise_score_indicator() -> None:
    indicators = EARNINGS_REVISION_DRIFT_PLAYBOOK["detect"]["indicators"]
    assert "revision_surprise_score" in indicators
    assert indicators["revision_surprise_score"]["threshold"] == 0.5


def test_earnings_drift_detect_has_priced_in_penalty_indicator() -> None:
    indicators = EARNINGS_REVISION_DRIFT_PLAYBOOK["detect"]["indicators"]
    assert "revision_priced_in_penalty" in indicators
    assert indicators["revision_priced_in_penalty"]["threshold"] == 0.3


def test_earnings_drift_every_buy_has_allocation_amount_and_currency() -> None:
    """Codex Round 12 P1 #2."""
    actions = EARNINGS_REVISION_DRIFT_PLAYBOOK["actions"]
    for phase_name, phase in actions.items():
        for entry in phase.get("buy", []):
            assert "allocation_amount" in entry, (
                f"{phase_name}/{entry.get('ticker')} missing allocation_amount"
            )
            assert "currency" in entry, (
                f"{phase_name}/{entry.get('ticker')} missing currency"
            )
            assert entry["currency"] in {"USD", "JPY"}, entry
            assert "allocation_usd" not in entry, (
                f"{phase_name}/{entry.get('ticker')} still uses deprecated allocation_usd"
            )


def test_earnings_drift_constant_not_mutated_by_migration(tmp_path: Path) -> None:
    before = json.dumps(
        EARNINGS_REVISION_DRIFT_PLAYBOOK, sort_keys=True, ensure_ascii=False
    )
    p = tmp_path / "event_playbook.json"
    migrate(p)
    after = json.dumps(
        EARNINGS_REVISION_DRIFT_PLAYBOOK, sort_keys=True, ensure_ascii=False
    )
    assert before == after


def test_earnings_drift_japanese_text_survives_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "event_playbook.json"
    migrate(p)
    raw = p.read_text(encoding="utf-8")
    assert "業績上方修正ドリフト" in raw
    assert "上方修正" in raw


def test_ipo_proxy_japanese_text_survives_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "event_playbook.json"
    migrate(p)
    raw = p.read_text(encoding="utf-8")
    assert "非上場プロキシ・イベントドリブン" in raw


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_creates_file_and_reports_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "event_playbook.json"
    code = _main([str(p)])
    assert code == 0
    out = capsys.readouterr().out
    assert "appended playbooks" in out
    assert "playbooks_after=2" in out


def test_cli_reports_noop_on_re_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "event_playbook.json"
    _main([str(p)])
    capsys.readouterr()
    code = _main([str(p)])
    assert code == 0
    assert "no-op" in capsys.readouterr().out


def test_cli_runs_on_existing_stub(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _stub_event_playbook_file(tmp_path)
    code = _main([str(p)])
    assert code == 0
    out = capsys.readouterr().out
    assert "appended playbooks" in out
    assert "playbooks_after=4" in out


def test_cli_returns_2_on_validation_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": "1.0"}))
    code = _main([str(bad)])
    assert code == 2
    assert "migration failed" in capsys.readouterr().err


def test_cli_returns_2_on_non_dict_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([1, 2, 3]))
    code = _main([str(bad)])
    assert code == 2

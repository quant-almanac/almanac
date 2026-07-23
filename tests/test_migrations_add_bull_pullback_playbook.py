"""Tests for almanac.migrations.add_bull_pullback_playbook.

The migration is a one-shot data change against ``scenario_playbook.json``.
Tests pin down the exact invariants we want a re-run / future similar
migration (``ipo_proxy_event`` / ``earnings_revision_drift`` in Phase 2)
to inherit:

- Idempotent re-runs are no-ops.
- Backup written with non-clobbering timestamp.
- Atomic write: no ``.tmp`` residue.
- Validation rejects non-conforming files instead of half-migrating.
- The new playbook carries the Round 7 / Round 11 #C feature flags
  (``enabled_for_decision`` true / ``observe_only`` false) and the
  three-phase action structure described in plan §5 step 5.
- The real production playbook file shape is migratable (run against a
  temp copy, never the live file).
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

from almanac.migrations.add_bull_pullback_playbook import (  # noqa: E402
    BULL_PULLBACK_ID,
    BULL_PULLBACK_PLAYBOOK,
    MigrationResult,
    _main,
    migrate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stub_playbook_file(tmp_path: Path, n_existing: int = 3) -> Path:
    """Realistic-shape stub with ``n_existing`` scenarios."""
    payload = {
        "version": "1.0",
        "description": "scenario_playbook stub",
        "updated_at": "2026-05-01T00:00:00",
        "scenarios": [
            {
                "id": f"stub_scenario_{i}",
                "name": f"stub {i}",
                "detect": {"news_keywords": [], "indicators": {}, "min_signals": 1},
                "actions": {},
            }
            for i in range(n_existing)
        ],
        "global_rules": {"max_active_scenarios": 3},
    }
    p = tmp_path / "scenario_playbook.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return p


# ---------------------------------------------------------------------------
# Migration core
# ---------------------------------------------------------------------------


def test_migrate_returns_summary(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path, n_existing=3)
    result = migrate(p)
    assert isinstance(result, MigrationResult)
    assert result.migrated is True
    assert result.scenarios_after == 4
    assert result.backup_path is not None
    assert result.backup_path.exists()


def test_migrate_appends_bull_pullback_to_scenarios_list(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path)
    migrate(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    ids = [s["id"] for s in data["scenarios"]]
    assert ids[-1] == BULL_PULLBACK_ID  # appended at the end
    assert ids.count(BULL_PULLBACK_ID) == 1


def test_migrate_preserves_existing_scenarios_verbatim(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path, n_existing=5)
    original = json.loads(p.read_text(encoding="utf-8"))
    migrate(p)
    migrated = json.loads(p.read_text(encoding="utf-8"))
    # First 5 entries unchanged (the new bull_pullback is at index 5).
    assert migrated["scenarios"][:5] == original["scenarios"]


def test_migrate_preserves_top_level_fields(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path)
    original = json.loads(p.read_text(encoding="utf-8"))
    migrate(p)
    migrated = json.loads(p.read_text(encoding="utf-8"))
    assert migrated["version"] == original["version"]
    assert migrated["description"] == original["description"]
    assert migrated["global_rules"] == original["global_rules"]
    # updated_at IS allowed to change (we just touched the file).


def test_migrate_updates_updated_at(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path)
    original = json.loads(p.read_text(encoding="utf-8"))["updated_at"]
    migrate(p)
    after = json.loads(p.read_text(encoding="utf-8"))["updated_at"]
    assert after != original


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path)
    first = migrate(p)
    second = migrate(p)
    assert first.migrated is True
    assert second.migrated is False
    assert second.backup_path is None
    assert second.scenarios_after == first.scenarios_after


def test_idempotent_run_creates_no_extra_backup(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path)
    migrate(p)
    backups_first = sorted(tmp_path.glob("*.bak.*"))
    migrate(p)
    backups_second = sorted(tmp_path.glob("*.bak.*"))
    assert backups_first == backups_second


def test_idempotent_run_does_not_rewrite_file(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path)
    migrate(p)
    after_first = p.read_text()
    mtime = p.stat().st_mtime_ns
    migrate(p)
    assert p.read_text() == after_first
    assert p.stat().st_mtime_ns == mtime


# ---------------------------------------------------------------------------
# Atomic / backup discipline
# ---------------------------------------------------------------------------


def test_no_tmp_file_remains_after_success(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path)
    migrate(p)
    assert not (tmp_path / "scenario_playbook.json.tmp").exists()


def test_backup_contains_original_data(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path)
    original = p.read_text()
    result = migrate(p)
    assert result.backup_path is not None
    assert result.backup_path.read_text() == original


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_migrate_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        migrate(tmp_path / "nope.json")


def test_migrate_rejects_non_dict_top_level(tmp_path: Path) -> None:
    p = tmp_path / "scenario_playbook.json"
    p.write_text(json.dumps([{"id": "x"}]))
    with pytest.raises(ValueError, match="'scenarios' field"):
        migrate(p)


def test_migrate_rejects_missing_scenarios_field(tmp_path: Path) -> None:
    p = tmp_path / "scenario_playbook.json"
    p.write_text(json.dumps({"version": "1.0"}))
    with pytest.raises(ValueError, match="'scenarios' field"):
        migrate(p)


def test_migrate_rejects_scenarios_not_a_list(tmp_path: Path) -> None:
    p = tmp_path / "scenario_playbook.json"
    p.write_text(json.dumps({"scenarios": {"id": "x"}, "version": "1.0"}))
    with pytest.raises(ValueError, match="'scenarios' must be a list"):
        migrate(p)


def test_validation_failure_leaves_no_backup(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"version": "1.0"}))
    with pytest.raises(ValueError):
        migrate(p)
    assert list(tmp_path.glob("*.bak.*")) == []


# ---------------------------------------------------------------------------
# Playbook shape (Round 7 / Round 11 feature flags + plan §5 step 5)
# ---------------------------------------------------------------------------


def test_playbook_carries_round11_feature_flags() -> None:
    """Round 7 C7-3 / Round 11 #C — 2-axis enable/observe."""
    assert BULL_PULLBACK_PLAYBOOK["enabled_for_decision"] is True
    assert BULL_PULLBACK_PLAYBOOK["observe_only"] is False


def test_playbook_has_three_phase_actions() -> None:
    """Conservative / Aggressive / Tactical per plan §5 step 5."""
    actions = BULL_PULLBACK_PLAYBOOK["actions"]
    assert set(actions.keys()) == {
        "phase_1_conservative",
        "phase_2_aggressive",
        "phase_3_tactical",
    }
    for phase in actions.values():
        assert "buy" in phase
        assert isinstance(phase["buy"], list)
        assert phase["buy"]  # non-empty


def test_playbook_detect_uses_min_signals_three() -> None:
    """All three of (VIX < 25), (pullback 3-8%), (regime_bull_confirmed)
    must be present to fire — plan §5 step 5."""
    assert BULL_PULLBACK_PLAYBOOK["detect"]["min_signals"] == 3


def test_playbook_id_is_canonical_constant() -> None:
    """ID is exported as a constant so other modules can reference it
    without string duplication."""
    assert BULL_PULLBACK_PLAYBOOK["id"] == BULL_PULLBACK_ID
    assert BULL_PULLBACK_ID == "bull_pullback"


def test_playbook_constant_is_not_mutated_by_migration(tmp_path: Path) -> None:
    """The module-level constant must remain pristine after a migration
    so subsequent imports / re-runs do not double-mutate."""
    before = json.dumps(BULL_PULLBACK_PLAYBOOK, sort_keys=True, ensure_ascii=False)
    p = _stub_playbook_file(tmp_path)
    migrate(p)
    after = json.dumps(BULL_PULLBACK_PLAYBOOK, sort_keys=True, ensure_ascii=False)
    assert before == after


def test_every_buy_entry_uses_allocation_amount_and_currency() -> None:
    """Codex Round 12 P1 #2 — no buy entry may use ``allocation_usd``;
    every entry must carry an explicit ``currency`` so a future executor
    cannot interpret ``200000`` (JPY) as 200K USD."""
    actions = BULL_PULLBACK_PLAYBOOK["actions"]
    for phase_name, phase in actions.items():
        for entry in phase.get("buy", []):
            assert "allocation_amount" in entry, (
                f"{phase_name}/{entry.get('ticker')} missing allocation_amount"
            )
            assert "currency" in entry, (
                f"{phase_name}/{entry.get('ticker')} missing currency"
            )
            assert entry["currency"] in {"USD", "JPY"}, entry
            # The deprecated key must NOT survive in the playbook.
            assert "allocation_usd" not in entry, (
                f"{phase_name}/{entry.get('ticker')} still uses allocation_usd"
            )


def test_jp_tickers_carry_jpy_currency() -> None:
    """JP-listed tickers (``.T`` suffix) must be JPY-denominated; a
    JPY-amount entry mis-tagged as USD would 150x the position size."""
    actions = BULL_PULLBACK_PLAYBOOK["actions"]
    for phase in actions.values():
        for entry in phase.get("buy", []):
            ticker = entry.get("ticker", "")
            expected = "JPY" if ticker.endswith(".T") else "USD"
            assert entry["currency"] == expected, (
                f"{ticker}: currency {entry['currency']} but expected {expected}"
            )


def test_jpy_amounts_are_at_least_50k_yen() -> None:
    """Sanity guard against a JPY entry accidentally carrying a USD-scale
    integer like 2000 — ¥2,000 is less than one share of any JP listing."""
    for phase in BULL_PULLBACK_PLAYBOOK["actions"].values():
        for entry in phase.get("buy", []):
            if entry.get("currency") == "JPY":
                assert entry["allocation_amount"] >= 50_000, entry


def test_playbook_japanese_text_survives_round_trip(tmp_path: Path) -> None:
    p = _stub_playbook_file(tmp_path)
    migrate(p)
    raw = p.read_text(encoding="utf-8")
    assert "強気相場の押し目買い" in raw
    assert "押し" in raw


# ---------------------------------------------------------------------------
# Production file shape compatibility
# ---------------------------------------------------------------------------


_PROD_FILE = _REPO_ROOT / "scenario_playbook.json"


@pytest.mark.skipif(
    not _PROD_FILE.exists(),
    reason="production scenario_playbook.json not available in this checkout",
)
def test_real_production_file_shape_is_migratable(tmp_path: Path) -> None:
    """Codex R11-D-style sanity check: migrate a copy of the real file."""
    copy = tmp_path / "scenario_playbook.json"
    shutil.copy2(_PROD_FILE, copy)
    original = json.loads(_PROD_FILE.read_text(encoding="utf-8"))
    original_ids = [s["id"] for s in original["scenarios"]]

    result = migrate(copy)
    data = json.loads(copy.read_text(encoding="utf-8"))
    ids = [s["id"] for s in data["scenarios"]]
    assert BULL_PULLBACK_ID in ids
    # The live file may already be migrated; in either case migration is
    # idempotent and every existing scenario survives the round-trip.
    expected_len = len(original["scenarios"]) if BULL_PULLBACK_ID in original_ids else len(original["scenarios"]) + 1
    assert len(data["scenarios"]) == expected_len


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_runs_migration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _stub_playbook_file(tmp_path)
    code = _main([str(p)])
    assert code == 0
    out = capsys.readouterr().out
    assert "appended bull_pullback" in out
    assert "scenarios_after=4" in out


def test_cli_reports_noop_on_re_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _stub_playbook_file(tmp_path)
    _main([str(p)])
    capsys.readouterr()
    code = _main([str(p)])
    assert code == 0
    assert "no-op" in capsys.readouterr().out


def test_cli_returns_2_on_validation_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": "1.0"}))
    code = _main([str(bad)])
    assert code == 2
    assert "migration failed" in capsys.readouterr().err


def test_cli_returns_2_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _main([str(tmp_path / "nope.json")])
    assert code == 2

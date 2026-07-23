"""Forward-only migration of beliefs/agent_beliefs.json from v1 to v2.

Background (plan Round 3 + Round 9 #D + Round 11 #D)
-----------------------------------------------------

The v1 schema (in production today, ``schema_version`` not yet set) stores a
single ``conviction_score`` field per belief, mutated in place by
:func:`analyst._update_beliefs`. Round 3 established that we want
deterministic *adjustment* events (``belief_adjustments.jsonl``, see plan
§6.3 / :func:`almanac.observability.logs.write_belief_adjustment`) feeding a
computed ``adjusted_conviction``, while preserving the original
``base_conviction`` for audit.

The v2 schema therefore introduces three new per-belief fields:

- ``base_conviction``  — copy of the v1 ``conviction_score`` at migration
  time. Never mutated thereafter.
- ``adjusted_conviction`` — initially equal to ``base_conviction``; at
  runtime it equals ``base_conviction + Σdelta`` over
  ``belief_adjustments.jsonl`` rows matching this ``belief_id``.
- ``adjustment_log`` — list of ``adjustment_id`` values referencing rows in
  ``belief_adjustments.jsonl``. Empty at migration time.

Top level gains a ``schema_version: 2`` field so this migration is
idempotent (re-runs are a no-op).

Design constraints from Codex Round 11 #D
------------------------------------------

1. **Do NOT assume a dict-keyed beliefs structure.** Real file has
   ``{"beliefs": [...list...], "last_updated": str, "version": str}``.
   The pre-existing top-level ``version`` (semantic content version) is
   distinct from ``schema_version`` (storage layout version) and must be
   preserved.
2. **Migration is purely additive.** The v1 field ``conviction_score`` is
   left intact so the ~14 existing readers in :mod:`analyst.__init__`
   continue to work. A Phase 2 refactor will switch those readers to
   ``adjusted_conviction`` and a v2→v3 migration can drop the alias.
3. **Backup before write.** A ``.v1.bak`` copy (with a UTC timestamp suffix
   so re-runs do not clobber prior backups) is written before the new file
   is committed.
4. **Atomic commit.** Write to ``<path>.tmp`` then ``os.replace`` so a
   crash mid-write cannot corrupt the live file.
5. **Idempotent.** If ``schema_version >= 2`` already, log and return.

Usage
-----

Library::

    from almanac.migrations.agent_beliefs_v1_to_v2 import migrate
    result = migrate(Path("beliefs/agent_beliefs.json"))
    print(result.beliefs_migrated, result.skipped, result.backup_path)

CLI::

    python -m almanac.migrations.agent_beliefs_v1_to_v2 beliefs/agent_beliefs.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["MigrationResult", "migrate", "TARGET_SCHEMA_VERSION"]

TARGET_SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationResult:
    """Summary returned by :func:`migrate` for callers and tests."""

    #: Path the migration ran on.
    path: Path
    #: True if a write actually occurred. False on idempotent re-run.
    migrated: bool
    #: Number of beliefs the migration touched. Zero on idempotent re-run.
    beliefs_migrated: int
    #: Number of beliefs already at v2 that were skipped during a partial
    #: re-run (defensive — should always be 0 in practice).
    skipped: int
    #: Backup file location, or ``None`` if no write occurred.
    backup_path: Path | None
    #: ``schema_version`` after the call (always ``TARGET_SCHEMA_VERSION``
    #: on success).
    schema_version_after: int


def _utc_stamp() -> str:
    """UTC timestamp suitable for filenames (no colons)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_path_for(src: Path) -> Path:
    """Return a non-clobbering backup path next to *src*.

    Example: ``beliefs/agent_beliefs.json`` →
    ``beliefs/agent_beliefs.json.v1.bak.20260524T013412Z``.
    """
    return src.with_suffix(src.suffix + f".v1.bak.{_utc_stamp()}")


def _validate_belief(belief: Any, index: int) -> None:
    """Raise if a belief is missing fields the migration depends on."""
    if not isinstance(belief, dict):
        raise ValueError(
            f"beliefs[{index}] is {type(belief).__name__}, expected dict"
        )
    for required in ("id", "conviction_score"):
        if required not in belief:
            raise ValueError(
                f"beliefs[{index}] missing required field {required!r}; "
                f"refusing to migrate (would lose data)"
            )
    cs = belief["conviction_score"]
    if not isinstance(cs, int):
        raise ValueError(
            f"beliefs[{index}].conviction_score is "
            f"{type(cs).__name__}, expected int"
        )


def migrate(path: Path | str) -> MigrationResult:
    """Migrate ``path`` from v1 to v2 in place (with backup).

    Parameters
    ----------
    path : Path or str
        ``agent_beliefs.json`` to migrate.

    Returns
    -------
    MigrationResult
        Summary; see dataclass docstring.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    ValueError
        If the file is structurally invalid (missing ``beliefs`` list,
        belief missing ``id``/``conviction_score``, wrong types).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: top-level must be dict, got {type(data).__name__}"
        )
    if "beliefs" not in data or not isinstance(data["beliefs"], list):
        raise ValueError(
            f"{path}: missing or non-list 'beliefs' field. "
            "Real production shape is {beliefs: [...], last_updated, version}; "
            "if this changed, update almanac.migrations.agent_beliefs_v1_to_v2."
        )

    existing_schema_version = data.get("schema_version", 1)
    if not isinstance(existing_schema_version, int):
        raise ValueError(
            f"{path}: schema_version must be int, got "
            f"{type(existing_schema_version).__name__}"
        )

    # ------------------------------------------------------------------
    # Idempotent fast path
    # ------------------------------------------------------------------
    if existing_schema_version >= TARGET_SCHEMA_VERSION:
        logger.info(
            "agent_beliefs already at schema_version=%s; nothing to do",
            existing_schema_version,
        )
        return MigrationResult(
            path=path,
            migrated=False,
            beliefs_migrated=0,
            skipped=0,
            backup_path=None,
            schema_version_after=existing_schema_version,
        )

    # ------------------------------------------------------------------
    # Validate every belief BEFORE writing anything. We refuse to migrate
    # a partially-broken file because doing so half-way is worse than not
    # doing it at all (the .bak protects the user either way).
    # ------------------------------------------------------------------
    for i, b in enumerate(data["beliefs"]):
        _validate_belief(b, i)

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------
    backup = _backup_path_for(path)
    shutil.copy2(path, backup)
    logger.info("backup written: %s", backup)

    # ------------------------------------------------------------------
    # Apply migration in memory
    # ------------------------------------------------------------------
    migrated = 0
    skipped = 0
    for b in data["beliefs"]:
        if "base_conviction" in b:
            # Defensive — should not happen on a clean v1 file; only
            # possible if a previous failed run wrote partial data.
            skipped += 1
            continue
        b["base_conviction"] = b["conviction_score"]
        b["adjusted_conviction"] = b["conviction_score"]
        b["adjustment_log"] = []
        # NOTE: deliberately leave conviction_score in place as an alias.
        # ~14 readers in analyst/__init__.py rely on it. A future v2→v3
        # migration can drop it once those readers switch to
        # adjusted_conviction.
        migrated += 1

    data["schema_version"] = TARGET_SCHEMA_VERSION
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    # `version` is the semantic content version and stays untouched.

    # ------------------------------------------------------------------
    # Atomic write: tmp → replace
    # ------------------------------------------------------------------
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    logger.info(
        "migrated %d beliefs (skipped %d) → schema_version=%d",
        migrated,
        skipped,
        TARGET_SCHEMA_VERSION,
    )

    return MigrationResult(
        path=path,
        migrated=True,
        beliefs_migrated=migrated,
        skipped=skipped,
        backup_path=backup,
        schema_version_after=TARGET_SCHEMA_VERSION,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate beliefs/agent_beliefs.json from v1 to v2 "
        "(adds base_conviction / adjusted_conviction / adjustment_log).",
    )
    parser.add_argument(
        "path",
        type=Path,
        help="path to agent_beliefs.json",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable INFO logging",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        result = migrate(args.path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2

    if result.migrated:
        print(
            f"migrated {result.beliefs_migrated} beliefs "
            f"(skipped {result.skipped}); "
            f"backup={result.backup_path}; "
            f"schema_version_after={result.schema_version_after}"
        )
    else:
        print(
            "no-op (already at schema_version="
            f"{result.schema_version_after})"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())

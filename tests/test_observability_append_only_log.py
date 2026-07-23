"""Tests for almanac.observability.append_only_log.

Covers the operational guarantees the catalyst observability layer depends on:

- One row per call, JSON-parseable.
- ``fcntl.flock`` serializes concurrent writers from multiple processes.
- ``fsync=False`` skips durability for tests/backfill but keeps the lock.
- Currency normalization is exact for JPY/USD and rejects unsupported codes.
- ``MeasurementQuality`` constants stay stable (referenced by outcome logs).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

# Make repo root importable so ``almanac.*`` resolves under pytest's default
# sys.path. Worktree layouts can otherwise miss it.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.append_only_log import (  # noqa: E402
    MeasurementQuality,
    append_jsonl_safe,
    normalize_to_jpy,
    normalize_to_usd,
)


# ---------------------------------------------------------------------------
# append_jsonl_safe — basics
# ---------------------------------------------------------------------------


def test_append_writes_single_json_line(tmp_path: Path) -> None:
    log = tmp_path / "catalyst_hypothesis_log.jsonl"
    append_jsonl_safe(log, {"hypothesis_id": "abc", "ticker": "NVDA"}, fsync=False)

    raw = log.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert raw.count("\n") == 1

    row = json.loads(raw)
    assert row == {"hypothesis_id": "abc", "ticker": "NVDA"}


def test_append_two_rows_preserves_order(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    append_jsonl_safe(log, {"i": 1}, fsync=False)
    append_jsonl_safe(log, {"i": 2}, fsync=False)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"i": 1}, {"i": 2}]


def test_append_creates_parent_directory(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "subdir" / "log.jsonl"
    append_jsonl_safe(log, {"ok": True}, fsync=False)
    assert log.exists()


def test_append_respects_ensure_parent_false(tmp_path: Path) -> None:
    log = tmp_path / "missing_dir" / "log.jsonl"
    with pytest.raises(FileNotFoundError):
        append_jsonl_safe(log, {"x": 1}, fsync=False, ensure_parent=False)


def test_append_accepts_str_path(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    append_jsonl_safe(str(log), {"ok": True}, fsync=False)
    assert json.loads(log.read_text()) == {"ok": True}


# ---------------------------------------------------------------------------
# append_jsonl_safe — serialization edge cases
# ---------------------------------------------------------------------------


def test_append_serializes_datetime_via_str_default(tmp_path: Path) -> None:
    """``default=str`` lets datetime land as ISO format without raising."""
    log = tmp_path / "log.jsonl"
    ts = datetime(2026, 5, 24, 18, 30, 0)
    append_jsonl_safe(log, {"recorded_at": ts}, fsync=False)
    row = json.loads(log.read_text())
    assert row["recorded_at"].startswith("2026-05-24 18:30:00")


def test_append_serializes_path_via_str_default(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    append_jsonl_safe(log, {"src": Path("/etc/passwd")}, fsync=False)
    assert json.loads(log.read_text())["src"] == "/etc/passwd"


def test_append_handles_japanese_text(tmp_path: Path) -> None:
    """ensure_ascii=False keeps headlines readable in logs."""
    log = tmp_path / "log.jsonl"
    append_jsonl_safe(log, {"headline": "サンプル企業上方修正"}, fsync=False)
    raw = log.read_text(encoding="utf-8")
    assert "サンプル企業上方修正" in raw  # not escaped to \uXXXX
    assert json.loads(raw)["headline"] == "サンプル企業上方修正"


def test_append_decimal_falls_through_to_str(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    append_jsonl_safe(log, {"price": Decimal("123.45")}, fsync=False)
    assert json.loads(log.read_text())["price"] == "123.45"


# ---------------------------------------------------------------------------
# append_jsonl_safe — fsync behavior
# ---------------------------------------------------------------------------


def test_append_calls_fsync_when_enabled(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    with patch.object(os, "fsync") as mock_fsync:
        append_jsonl_safe(log, {"x": 1}, fsync=True)
    assert mock_fsync.call_count == 1


def test_append_skips_fsync_when_disabled(tmp_path: Path) -> None:
    """Round 11 A — fsync=False used by tests and backfill."""
    log = tmp_path / "log.jsonl"
    with patch.object(os, "fsync") as mock_fsync:
        append_jsonl_safe(log, {"x": 1}, fsync=False)
    assert mock_fsync.call_count == 0


# ---------------------------------------------------------------------------
# append_jsonl_safe — concurrency (the safety-critical guarantee)
# ---------------------------------------------------------------------------


def _writer_worker(args: tuple[str, int, int]) -> None:
    """Helper for the multiprocessing concurrency test."""
    path_str, worker_id, n_rows = args
    # Re-import inside the child so each process initializes its own state.
    from almanac.observability.append_only_log import append_jsonl_safe as _append

    for i in range(n_rows):
        _append(path_str, {"worker": worker_id, "i": i}, fsync=False)


def test_concurrent_writers_produce_well_formed_jsonl(tmp_path: Path) -> None:
    """Multiple processes appending to the same file must not interleave bytes.

    This is the headline guarantee the catalyst observability layer depends
    on — cron jobs ``analyzer.py``, ``signal_tracker.py``, and
    ``invalidation_rules.py`` may run concurrently and all append to the
    same JSONL files.
    """
    log = tmp_path / "concurrent.jsonl"
    n_workers, n_rows = 4, 25
    args = [(str(log), w, n_rows) for w in range(n_workers)]

    with multiprocessing.Pool(processes=n_workers) as pool:
        pool.map(_writer_worker, args)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_workers * n_rows

    # Every line must be valid JSON — the heart of the test. If flock failed
    # to serialize, partial lines would interleave and parse would error.
    parsed = [json.loads(line) for line in lines]

    # Each (worker, i) pair must appear exactly once. flock is exclusive,
    # so no row should be lost or duplicated.
    seen = {(p["worker"], p["i"]) for p in parsed}
    assert seen == {(w, i) for w in range(n_workers) for i in range(n_rows)}


# ---------------------------------------------------------------------------
# normalize_to_jpy / normalize_to_usd
# ---------------------------------------------------------------------------


def test_normalize_to_jpy_identity_for_jpy() -> None:
    assert normalize_to_jpy(2768.5, "JPY", usdjpy=156.2) == 2768.5


def test_normalize_to_jpy_multiplies_usd() -> None:
    assert normalize_to_jpy(100.0, "USD", usdjpy=156.2) == pytest.approx(15620.0)


def test_normalize_to_jpy_rejects_unknown_currency() -> None:
    with pytest.raises(ValueError, match="Unsupported currency"):
        normalize_to_jpy(100.0, "EUR", usdjpy=156.2)


def test_normalize_to_usd_identity_for_usd() -> None:
    assert normalize_to_usd(478.3, "USD", usdjpy=156.2) == 478.3


def test_normalize_to_usd_divides_jpy() -> None:
    assert normalize_to_usd(15620.0, "JPY", usdjpy=156.2) == pytest.approx(100.0)


def test_normalize_to_usd_rejects_zero_usdjpy() -> None:
    """Guard catches the inf/NaN trap before it corrupts the outcome log."""
    with pytest.raises(ValueError, match="usdjpy must be positive"):
        normalize_to_usd(15620.0, "JPY", usdjpy=0.0)


def test_normalize_to_usd_rejects_negative_usdjpy() -> None:
    with pytest.raises(ValueError, match="usdjpy must be positive"):
        normalize_to_usd(15620.0, "JPY", usdjpy=-1.0)


# ---------------------------------------------------------------------------
# MeasurementQuality
# ---------------------------------------------------------------------------


def test_measurement_quality_constants_are_plain_strings() -> None:
    """Required so they serialize as bare strings without .value lookups."""
    assert MeasurementQuality.OK == "ok"
    assert MeasurementQuality.STALE == "stale"
    assert MeasurementQuality.MISSING == "missing"
    assert MeasurementQuality.REVISED == "revised"
    assert isinstance(MeasurementQuality.OK, str)

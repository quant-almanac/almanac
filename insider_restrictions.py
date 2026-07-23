"""Structural exclusion for employer/restricted tickers on signal surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = BASE_DIR / "insider_restricted.json"

# Config deletion or corruption must not silently re-enable the known employer.
MINIMUM_RESTRICTED_TICKERS = frozenset({"9999.T"})


def normalize_ticker(value: object) -> str:
    ticker = str(value or "").strip().upper()
    if len(ticker) == 4 and ticker.isdigit():
        ticker += ".T"
    return ticker


def load_restricted_tickers(path: Path | str | None = None) -> set[str]:
    restricted = set(MINIMUM_RESTRICTED_TICKERS)
    config = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return restricted
    values = payload.get("tickers", []) if isinstance(payload, dict) else payload
    if isinstance(values, list):
        restricted.update(normalize_ticker(value) for value in values if value)
    return restricted


def is_restricted_ticker(value: object, *, path: Path | str | None = None) -> bool:
    return normalize_ticker(value) in load_restricted_tickers(path)


def filter_allowed_tickers(
    values: Iterable[object],
    *,
    path: Path | str | None = None,
) -> list[str]:
    restricted = load_restricted_tickers(path)
    return [
        str(value)
        for value in values
        if normalize_ticker(value) not in restricted
    ]


def signal_record_is_restricted(
    row: Mapping[str, object],
    *,
    path: Path | str | None = None,
) -> bool:
    """Check common single-name and pair signal schemas."""
    restricted = load_restricted_tickers(path)
    for key in ("ticker", "primary_ticker", "long", "short"):
        if normalize_ticker(row.get(key)) in restricted:
            return True
    pair = str(row.get("pair") or "")
    if pair:
        return any(normalize_ticker(part) in restricted for part in pair.split("/"))
    return False


def filter_signal_records(
    rows: Iterable[Mapping[str, object]],
    *,
    path: Path | str | None = None,
) -> list[dict]:
    return [
        dict(row)
        for row in rows
        if isinstance(row, Mapping) and not signal_record_is_restricted(row, path=path)
    ]

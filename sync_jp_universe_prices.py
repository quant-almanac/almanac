#!/usr/bin/env python3
"""Weekly, opt-in JP disclosure-universe OHLCV parquet synchronizer."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
from typing import Callable

import pandas as pd

from data_fetcher import append_price_sanity_flags, detect_price_sanity_flags
from ingest_disclosures import resolve_scan_universe

BASE_DIR = Path(__file__).parent
DEFAULT_OUTPUT_DIR = BASE_DIR / "data" / "ohlcv"
REQUIRED_OHLCV_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def _default_fetch(ticker: str, start: str | None) -> pd.DataFrame:
    import yfinance as yf

    kwargs = {
        "progress": False,
        "threads": False,
        "auto_adjust": True,
    }
    if start:
        kwargs["start"] = start
        kwargs["end"] = (date.today() + timedelta(days=1)).isoformat()
    else:
        kwargs["period"] = "2y"
    return yf.download(ticker, **kwargs)


def _available_column_names(frame: pd.DataFrame) -> set[str]:
    if isinstance(frame.columns, pd.MultiIndex):
        names: set[str] = set()
        for level in range(frame.columns.nlevels):
            names.update(str(value) for value in frame.columns.get_level_values(level))
        return names
    return {str(column) for column in frame.columns}


def _validate_ohlcv_frame(frame: pd.DataFrame) -> None:
    available = _available_column_names(frame)
    missing = [column for column in REQUIRED_OHLCV_COLUMNS if column not in available]
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing)}")


def _price_sanity_log_path(output_dir: Path) -> Path:
    if output_dir.name == "ohlcv":
        return output_dir.parent / "price_sanity_flags.jsonl"
    return output_dir / "price_sanity_flags.jsonl"


def sync_prices(
    tickers: list[str],
    *,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    live: bool = False,
    fetch: Callable[[str, str | None], pd.DataFrame] | None = None,
) -> dict:
    """Append new rows per ticker. Network requires explicit ``live=True``."""
    if fetch is None and not live:
        return {
            "status": "skipped",
            "reason": "live_false",
            "requested": len(tickers),
            "updated": 0,
        }
    fetcher = fetch or _default_fetch
    target_dir = Path(output_dir)
    price_sanity_log = _price_sanity_log_path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "completed",
        "requested": len(tickers),
        "updated": 0,
        "unchanged": 0,
        "failed": [],
    }

    for ticker in tickers:
        path = target_dir / f"{ticker.replace('/', '_')}.parquet"
        existing = pd.DataFrame()
        start = None
        if path.exists():
            try:
                existing = pd.read_parquet(path)
                if not existing.empty:
                    last = pd.Timestamp(existing.index.max()).date()
                    start = (last + timedelta(days=1)).isoformat()
            except Exception as exc:
                report["failed"].append({"ticker": ticker, "error": f"read:{exc}"})
                continue
        try:
            fresh = fetcher(ticker, start)
        except Exception as exc:
            report["failed"].append({"ticker": ticker, "error": f"fetch:{exc}"})
            continue
        if fresh is None or fresh.empty:
            report["unchanged"] += 1
            continue
        try:
            _validate_ohlcv_frame(fresh)
        except ValueError as exc:
            report["failed"].append({"ticker": ticker, "error": f"invalid_ohlcv:{exc}"})
            continue
        combined = pd.concat([existing, fresh]) if not existing.empty else fresh.copy()
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        try:
            _validate_ohlcv_frame(combined)
            append_price_sanity_flags(
                detect_price_sanity_flags(ticker, combined),
                path=price_sanity_log,
            )
            combined.to_parquet(path)
        except Exception as exc:
            report["failed"].append({"ticker": ticker, "error": f"write:{exc}"})
            continue
        report["updated"] += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="enable yfinance network access")
    parser.add_argument("--universe", type=Path, default=BASE_DIR / "disclosure_universe_jp.json")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    tickers = resolve_scan_universe(universe_path=args.universe, market="JP")
    result = sync_prices(tickers, output_dir=args.output_dir, live=args.live)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())

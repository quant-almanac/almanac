"""Update local JSF lending/squeeze state from explicit opt-in data."""

from __future__ import annotations

import argparse
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from almanac.runtime_config import get_env

BASE_DIR = Path(__file__).parent


def parse_jsf_csv(text: str) -> dict[str, dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"ticker", "loan_ratio", "reverse_daily_fee"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError(
            "unrecognized JSF fixture headers; expected ticker, loan_ratio, reverse_daily_fee"
        )
    result: dict[str, dict] = {}
    for row in reader:
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            continue
        try:
            ratio = float(row["loan_ratio"]) if row.get("loan_ratio") else None
        except ValueError:
            ratio = None
        fee = str(row.get("reverse_daily_fee") or "").strip().lower() in {
            "1", "true", "yes", "active", "発生",
        }
        result[ticker] = {"loan_ratio": ratio, "reverse_daily_fee": fee}
    return result


def sync(
    *,
    output_path: Path | str,
    source_text: str | None = None,
    live: bool = False,
    fetcher: Callable[[str], str] | None = None,
) -> dict:
    if source_text is None:
        if not live:
            raise ValueError("source_text is required unless live=True")
        url = get_env("ALMANAC_JSF_LENDING_URL")
        if not url:
            raise ValueError("ALMANAC_JSF_LENDING_URL is required for live sync")
        source_text = (fetcher or (lambda value: requests.get(value, timeout=30).text))(url)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": parse_jsf_csv(source_text),
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(BASE_DIR / "data" / "jsf_lending_state.json"))
    parser.add_argument("--source-file")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    text = Path(args.source_file).read_text(encoding="utf-8") if args.source_file else None
    payload = sync(output_path=args.output, source_text=text, live=args.live)
    print(json.dumps({"ticker_count": len(payload["tickers"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

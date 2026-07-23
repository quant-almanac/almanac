"""Update JP loanable flags from an explicit file or opt-in live source."""

from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
from typing import Callable

import requests

from almanac.runtime_config import get_env

BASE_DIR = Path(__file__).parent
CODE_FIELDS = ("銘柄コード", "コード", "Local Code", "Code")
STATUS_FIELDS = ("貸借区分", "貸借銘柄", "Loanable", "Status")


def parse_loanable_csv(text: str) -> dict[str, bool]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ValueError("JPX loanable source has no CSV header")
    code_field = next((field for field in CODE_FIELDS if field in reader.fieldnames), None)
    status_field = next((field for field in STATUS_FIELDS if field in reader.fieldnames), None)
    if code_field is None or status_field is None:
        raise ValueError(f"unrecognized JPX loanable CSV headers: {reader.fieldnames}")
    result: dict[str, bool] = {}
    for row in reader:
        code = "".join(ch for ch in str(row.get(code_field) or "") if ch.isdigit())
        if len(code) != 4:
            continue
        status = str(row.get(status_field) or "").strip().lower()
        result[f"{code}.T"] = status in {"貸借", "貸借銘柄", "true", "1", "yes", "loanable"}
    return result


def sync(
    *,
    universe_path: Path | str,
    source_text: str | None = None,
    live: bool = False,
    fetcher: Callable[[str], str] | None = None,
) -> dict:
    path = Path(universe_path)
    universe = json.loads(path.read_text(encoding="utf-8"))
    if source_text is None:
        if not live:
            raise ValueError("source_text is required unless live=True")
        url = get_env("ALMANAC_JPX_LOANABLE_URL")
        if not url:
            raise ValueError("ALMANAC_JPX_LOANABLE_URL is required for live sync")
        source_text = (fetcher or (lambda value: requests.get(value, timeout=30).text))(url)
    parsed = parse_loanable_csv(source_text)
    tickers = universe.get("tickers") or []
    universe["loanable_by_ticker"] = {
        ticker: parsed.get(ticker)
        for ticker in tickers
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(universe, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return {
        "universe_count": len(tickers),
        "confirmed_count": sum(value is not None for value in universe["loanable_by_ticker"].values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default=str(BASE_DIR / "disclosure_universe_jp.json"))
    parser.add_argument("--source-file")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    text = Path(args.source_file).read_text(encoding="utf-8") if args.source_file else None
    print(json.dumps(sync(universe_path=args.universe, source_text=text, live=args.live)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

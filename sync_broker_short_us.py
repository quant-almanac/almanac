"""Update local US broker short-availability state from explicit opt-in CSV.

楽天/SBI の米株「売建可否」と借株コストは API/URL では取得できないため、
人手で確認した内容を CSV で供給する(JP の sync_jp_loanable / sync_jsf_lending と
同じ fail-closed 方針)。CSV に無い銘柄は供給されていない=fail-closed のまま。

CSV 形式:
  ticker,rakuten,sbi,borrow_cost_annual_pct
  TSLA,yes,no,0.05
  - rakuten / sbi: yes|true|1|可 → True、それ以外 → False
  - borrow_cost_annual_pct: 年率(小数)。空欄は None。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
_TRUE = {"yes", "true", "1", "可", "available", "y"}
_REQUIRED = {"ticker", "rakuten", "sbi"}


def _to_bool(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUE


def parse_broker_short_csv(text: str) -> dict[str, dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or not _REQUIRED.issubset(reader.fieldnames):
        raise ValueError(
            "unrecognized US broker CSV headers; expected ticker, rakuten, sbi[, borrow_cost_annual_pct]"
        )
    result: dict[str, dict] = {}
    for row in reader:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        cost_raw = str(row.get("borrow_cost_annual_pct") or "").strip()
        try:
            cost = float(cost_raw) if cost_raw else None
        except ValueError:
            cost = None
        result[ticker] = {
            "rakuten": _to_bool(row.get("rakuten")),
            "sbi": _to_bool(row.get("sbi")),
            "borrow_cost_annual_pct": cost,
        }
    return result


def sync(*, output_path: Path | str, source_text: str) -> dict:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": parse_broker_short_csv(source_text),
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(BASE_DIR / "data" / "broker_short_us.json"))
    parser.add_argument("--source-file", required=True)
    args = parser.parse_args()
    text = Path(args.source_file).read_text(encoding="utf-8")
    payload = sync(output_path=args.output, source_text=text)
    print(json.dumps({"ticker_count": len(payload["tickers"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

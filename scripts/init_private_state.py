#!/usr/bin/env python3
"""Create local private state files from safe examples.

The script never overwrites existing files. It is intended for fresh clones
where real portfolio state is intentionally absent from Git.
"""

from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = ROOT / "examples" / "private_state"

FILES = {
    "account.example.json": "account.json",
    "holdings.example.json": "holdings.json",
    "nisa_portfolio.example.json": "nisa_portfolio.json",
    "espp_plan.example.json": "espp_plan.json",
    "credit_card_plans.example.json": "credit_card_plans.json",
    "trade_history.example.csv": "trade_history.csv",
    "tickers.example.json": "tickers.json",
}


def main() -> int:
    created: list[str] = []
    skipped: list[str] = []

    for src_name, dst_name in FILES.items():
        src = EXAMPLE_DIR / src_name
        dst = ROOT / dst_name
        if dst.exists():
            skipped.append(dst_name)
            continue
        if not src.exists():
            raise FileNotFoundError(f"Missing example file: {src}")
        shutil.copyfile(src, dst)
        created.append(dst_name)

    if created:
        print("Created local private state files:")
        for name in created:
            print(f"  - {name}")
    if skipped:
        print("Skipped existing files:")
        for name in skipped:
            print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

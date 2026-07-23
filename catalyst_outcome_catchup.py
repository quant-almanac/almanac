#!/usr/bin/env python3
"""One-time, review-gated catch-up for overdue catalyst/sell outcomes."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

from almanac.observability.outcome_updater import (
    DEFAULT_HORIZONS,
    PriceProvider,
    update_all_outcomes,
)


def _parse_horizons(value: str) -> tuple[int, ...]:
    parsed = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    if not parsed or any(item <= 0 for item in parsed):
        raise ValueError("horizons must contain positive integers")
    return parsed


def run_catchup(
    *,
    root: Path,
    today: date,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    apply: bool = False,
    price_provider: PriceProvider | None = None,
) -> dict:
    """Run the idempotent updater only after an explicit review-time opt-in."""
    if not apply:
        return {
            "status": "review_required",
            "root": str(root),
            "today": today.isoformat(),
            "horizons": list(horizons),
            "message": "Re-run with --apply during the reviewed live catch-up.",
        }
    counts = update_all_outcomes(
        root=root,
        today=today,
        horizons=horizons,
        price_provider=price_provider,
    )
    return {
        "status": "applied",
        "root": str(root),
        "today": today.isoformat(),
        "horizons": list(horizons),
        **counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--horizons", default="3,5,10,20,60")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform network-backed price resolution and append due outcomes",
    )
    args = parser.parse_args(argv)
    try:
        horizons = _parse_horizons(args.horizons)
    except ValueError as exc:
        parser.error(str(exc))
    result = run_catchup(
        root=args.root,
        today=args.today,
        horizons=horizons,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

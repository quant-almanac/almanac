"""Daily VaR forecast storage and Kupiec proportion-of-failures validation."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from scipy.stats import chi2

from almanac.runtime_config import resolve_db_path

BASE_DIR = Path(__file__).parent
DB_PATH = resolve_db_path(BASE_DIR)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS var_forecasts (
    forecast_date TEXT NOT NULL,
    confidence REAL NOT NULL,
    var_pct REAL NOT NULL,
    model TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (forecast_date, confidence, model)
);
"""


def init_schema(db_path: Optional[Path] = None) -> None:
    with sqlite3.connect(str(db_path or DB_PATH)) as conn:
        conn.executescript(SCHEMA_SQL)


def kupiec_pof(
    exceptions: Iterable[bool],
    *,
    confidence: float = 0.95,
    alpha: float = 0.05,
) -> dict:
    """Kupiec unconditional coverage test for a binary VaR exception series."""
    values = [bool(v) for v in exceptions]
    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "exceptions": 0,
            "expected_rate": 1.0 - confidence,
            "observed_rate": None,
            "lr_pof": None,
            "p_value": None,
            "passed": None,
            "error": "no observations",
        }
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")

    x = sum(values)
    expected = 1.0 - confidence
    observed = x / n

    def term(count: int, probability: float) -> float:
        if count == 0:
            return 0.0
        if probability <= 0.0:
            return float("-inf")
        return count * math.log(probability)

    null_ll = term(n - x, 1.0 - expected) + term(x, expected)
    alt_ll = term(n - x, 1.0 - observed) + term(x, observed)
    lr_pof = max(0.0, -2.0 * (null_ll - alt_ll))
    p_value = float(chi2.sf(lr_pof, 1))
    return {
        "n": n,
        "exceptions": x,
        "expected_rate": expected,
        "observed_rate": observed,
        "lr_pof": lr_pof,
        "p_value": p_value,
        "passed": p_value >= alpha,
        "alpha": alpha,
        "error": None,
    }


def estimate_var_from_daily_history(
    *,
    as_of: Optional[str] = None,
    confidence: float = 0.95,
    lookback: int = 90,
    db_path: Optional[Path] = None,
) -> dict:
    """Estimate next-session VaR using only realized returns available by ``as_of``."""
    import pandas as pd

    from risk_engine import calculate_var_cornish_fisher

    cutoff = as_of or date.today().isoformat()
    with sqlite3.connect(str(db_path or DB_PATH)) as conn:
        rows = conn.execute(
            """
            SELECT daily_pnl_pct
            FROM daily_performance
            WHERE date <= ? AND COALESCE(estimated, 0) = 0
              AND daily_pnl_pct IS NOT NULL
            ORDER BY date DESC
            LIMIT ?
            """,
            (cutoff, lookback),
        ).fetchall()
    returns = pd.Series([float(row[0]) / 100.0 for row in reversed(rows)], dtype=float)
    if len(returns) < 20:
        raise ValueError(f"VaR forecast requires at least 20 clean observations (got {len(returns)})")
    result = calculate_var_cornish_fisher(returns, confidence=confidence)
    if result.get("error"):
        raise ValueError(f"VaR forecast failed: {result['error']}")
    raw = float(result["var_pct"])
    var_pct = raw * 100.0 if abs(raw) < 1.0 else raw
    return {
        "forecast_date": cutoff,
        "confidence": confidence,
        "var_pct": abs(var_pct),
        "model": "cornish_fisher_daily_performance",
        "sample_size": len(returns),
    }


def record_forecast(
    forecast: dict,
    *,
    db_path: Optional[Path] = None,
) -> dict:
    init_schema(db_path)
    with sqlite3.connect(str(db_path or DB_PATH)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO var_forecasts
              (forecast_date, confidence, var_pct, model, sample_size)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                forecast["forecast_date"],
                float(forecast["confidence"]),
                float(forecast["var_pct"]),
                str(forecast["model"]),
                int(forecast["sample_size"]),
            ),
        )
    return dict(forecast)


def load_backtest_observations(
    *,
    confidence: float = 0.95,
    model: str = "cornish_fisher_daily_performance",
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Pair each end-of-day forecast with the next clean realized P&L observation."""
    init_schema(db_path)
    with sqlite3.connect(str(db_path or DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
              f.forecast_date,
              f.var_pct,
              (
                SELECT d.date
                FROM daily_performance d
                WHERE d.date > f.forecast_date
                  AND COALESCE(d.estimated, 0) = 0
                  AND d.daily_pnl_pct IS NOT NULL
                ORDER BY d.date
                LIMIT 1
              ) AS realized_date,
              (
                SELECT d.daily_pnl_pct
                FROM daily_performance d
                WHERE d.date > f.forecast_date
                  AND COALESCE(d.estimated, 0) = 0
                  AND d.daily_pnl_pct IS NOT NULL
                ORDER BY d.date
                LIMIT 1
              ) AS realized_pnl_pct
            FROM var_forecasts f
            WHERE f.confidence = ? AND f.model = ?
            ORDER BY f.forecast_date
            """,
            (confidence, model),
        ).fetchall()
    observations = []
    for row in rows:
        if row["realized_date"] is None:
            continue
        realized = float(row["realized_pnl_pct"])
        var_pct = float(row["var_pct"])
        observations.append({
            "forecast_date": row["forecast_date"],
            "realized_date": row["realized_date"],
            "var_pct": var_pct,
            "realized_pnl_pct": realized,
            "exception": realized < -var_pct,
        })
    return observations


def validate_forecasts(
    *,
    confidence: float = 0.95,
    model: str = "cornish_fisher_daily_performance",
    db_path: Optional[Path] = None,
) -> dict:
    observations = load_backtest_observations(
        confidence=confidence,
        model=model,
        db_path=db_path,
    )
    result = kupiec_pof(
        [row["exception"] for row in observations],
        confidence=confidence,
    )
    result["model"] = model
    result["observations"] = observations
    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description="VaR forecast and Kupiec POF validation")
    sub = parser.add_subparsers(dest="cmd", required=True)
    record = sub.add_parser("record", help="record a next-session VaR forecast from local NAV history")
    record.add_argument("--as-of", default=None)
    record.add_argument("--confidence", type=float, default=0.95)
    validate = sub.add_parser("validate", help="validate stored forecasts against next realized P&L")
    validate.add_argument("--confidence", type=float, default=0.95)
    args = parser.parse_args()

    if args.cmd == "record":
        output = record_forecast(estimate_var_from_daily_history(
            as_of=args.as_of,
            confidence=args.confidence,
        ))
    else:
        output = validate_forecasts(confidence=args.confidence)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()

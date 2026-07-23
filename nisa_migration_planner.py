"""Multi-year taxable-to-NISA migration proposals (human execution only)."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from nisa_allocator import score_nisa_placement

BASE_DIR = Path(__file__).parent
TAX_RATE = 0.20315
DEFAULT_ANNUAL_GROWTH_LIMIT = 2_400_000


def _is_nav_quoted_unit(holding: dict[str, Any], currency: str) -> bool:
    return bool(holding.get("unit")) and currency != "USD"


def _per_quantity_jpy(
    raw_price: float,
    *,
    holding: dict[str, Any],
    currency: str,
    fx: float,
) -> float:
    price = float(raw_price)
    if _is_nav_quoted_unit(holding, currency):
        price /= 10_000.0
    if currency == "USD":
        price *= fx
    return price


def _current_price_jpy(
    holding: dict[str, Any],
    *,
    ticker: str,
    currency: str,
    fx: float,
) -> float:
    raw = (
        holding.get("current_price")
        or holding.get("current_nav")
        or holding.get("price")
    )
    if raw is None:
        raise ValueError(f"{ticker} requires current_price/current_nav for NISA migration")
    current = _per_quantity_jpy(float(raw), holding=holding, currency=currency, fx=fx)
    if current <= 0:
        raise ValueError(f"{ticker} current_price/current_nav must be positive")
    return current


def _cost_per_quantity_jpy(
    raw_cost: float,
    *,
    holding: dict[str, Any],
    currency: str,
    fx: float,
    current_price_jpy: float | None = None,
    raw_is_native_price: bool = False,
) -> float:
    if raw_is_native_price:
        return _per_quantity_jpy(float(raw_cost), holding=holding, currency=currency, fx=fx)
    cost = float(raw_cost)
    if (
        _is_nav_quoted_unit(holding, currency)
        and current_price_jpy is not None
        and current_price_jpy > 0
        and cost > current_price_jpy * 100
    ):
        cost /= 10_000.0
    return cost


def _growth_capacity(nisa_data: dict[str, Any], person: str, year: int, current_year: int) -> float:
    data = nisa_data.get(person) or {}
    if year == current_year:
        return float(
            data.get("growth_remaining")
            if data.get("growth_remaining") is not None
            else max(
                0,
                float(data.get("growth_limit_annual") or DEFAULT_ANNUAL_GROWTH_LIMIT)
                - float(data.get("growth_used_this_year") or 0)
                - float(data.get("growth_planned_this_year") or 0),
            )
        )
    return float(data.get("growth_limit_annual") or DEFAULT_ANNUAL_GROWTH_LIMIT)


def _taxable_candidates(
    holdings: dict[str, Any],
    lots_by_ticker: dict[str, list[dict[str, Any]]],
    fx_rate_usdjpy: float | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key, holding in holdings.items():
        if not isinstance(holding, dict) or key.startswith("CASH_"):
            continue
        account = str(holding.get("account") or "")
        if "NISA" in account or account in {"持株会", "信用"}:
            continue
        ticker = str(holding.get("ticker") or key)
        scored = score_nisa_placement({**holding, "key": key, "ticker": ticker})
        if scored["recommended_account"] != "NISA成長投資枠":
            continue
        currency = str(holding.get("currency") or "JPY").upper()
        if currency == "USD":
            fx = float(holding.get("fx_rate_usdjpy") or fx_rate_usdjpy or 0)
            if fx <= 0:
                raise ValueError(f"USD holding {ticker} requires fx_rate_usdjpy")
        else:
            fx = 1.0
        current_price_jpy = _current_price_jpy(
            holding,
            ticker=ticker,
            currency=currency,
            fx=fx,
        )
        lots = [
            lot
            for lot in lots_by_ticker.get(ticker, [])
            if lot.get("account") == account and float(lot.get("remaining_qty") or 0) > 0
        ]
        if not lots:
            lots = [{
                "lot_id": f"holding:{key}",
                "purchase_date": holding.get("entry_date") or "",
                "remaining_qty": float(holding.get("shares") or 0),
                "cost_per_share_jpy": _cost_per_quantity_jpy(
                    float(holding.get("entry_price") or 0),
                    holding=holding,
                    currency=currency,
                    fx=fx,
                    current_price_jpy=current_price_jpy,
                    raw_is_native_price=True,
                ),
                "account": account,
            }]
        for lot in lots:
            quantity = float(lot.get("remaining_qty") or 0)
            if quantity <= 0:
                continue
            cost = _cost_per_quantity_jpy(
                float(lot.get("cost_per_share_jpy") or 0),
                holding=holding,
                currency=currency,
                fx=1.0,
                current_price_jpy=current_price_jpy,
                raw_is_native_price=False,
            )
            gain_per_share = current_price_jpy - cost
            candidates.append({
                "ticker": ticker,
                "name": holding.get("name") or ticker,
                "source_account": account,
                "lot_id": lot.get("lot_id"),
                "purchase_date": lot.get("purchase_date"),
                "remaining_qty": quantity,
                "current_price_jpy": current_price_jpy,
                "gain_per_share_jpy": gain_per_share,
                "nisa_score": float(scored["score"]),
            })
    candidates.sort(
        key=lambda row: (
            -row["nisa_score"],
            row["gain_per_share_jpy"] / row["current_price_jpy"],
            row["purchase_date"] or "",
        )
    )
    return candidates


def build_migration_plan(
    *,
    nisa_data: dict[str, Any],
    holdings: dict[str, Any],
    lots_by_ticker: dict[str, list[dict[str, Any]]] | None = None,
    start_year: int | None = None,
    years: int = 3,
    fx_rate_usdjpy: float | None = None,
) -> dict[str, Any]:
    """Allocate taxable lots across future NISA growth capacity."""
    current_year = date.today().year
    start = start_year or current_year
    candidates = _taxable_candidates(
        holdings,
        lots_by_ticker or {},
        fx_rate_usdjpy=fx_rate_usdjpy,
    )
    moves: list[dict[str, Any]] = []
    remaining = [dict(row) for row in candidates]
    for year in range(start, start + max(1, years)):
        for person in ("husband", "wife"):
            capacity = _growth_capacity(nisa_data, person, year, current_year)
            for row in remaining:
                if capacity <= 0:
                    break
                qty_available = float(row["remaining_qty"])
                if qty_available <= 1e-9:
                    continue
                qty = min(qty_available, capacity / row["current_price_jpy"])
                if qty <= 1e-9:
                    continue
                market_value = qty * row["current_price_jpy"]
                realized = qty * row["gain_per_share_jpy"]
                moves.append({
                    "year": year,
                    "person": person,
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "source_account": row["source_account"],
                    "destination_account": "NISA成長投資枠",
                    "lot_id": row["lot_id"],
                    "purchase_date": row["purchase_date"],
                    "quantity": round(qty, 6),
                    "market_value_jpy": round(market_value, 0),
                    "estimated_realized_gain_jpy": round(realized, 0),
                    "estimated_tax_jpy": round(max(0.0, realized) * TAX_RATE, 0),
                    "nisa_score": row["nisa_score"],
                    "human_execution_only": True,
                })
                row["remaining_qty"] -= qty
                capacity -= market_value
    return {
        "generated_at": date.today().isoformat(),
        "start_year": start,
        "years": years,
        "fx_rate_usdjpy": round(float(fx_rate_usdjpy), 4) if fx_rate_usdjpy else None,
        "actionable": True,
        "tax_lot_source": "provided",
        "data_quality_issues": [],
        "human_execution_only": True,
        "display_only": True,
        "moves": moves,
        "total_market_value_jpy": round(sum(move["market_value_jpy"] for move in moves), 0),
        "estimated_tax_jpy": round(sum(move["estimated_tax_jpy"] for move in moves), 0),
        "note": "Each move is a taxable sale followed by a manual NISA repurchase; no transfer or order is executed.",
    }


def _read_account_fx_rate(root: Path) -> float | None:
    path = root / "account.json"
    if not path.exists():
        return None
    account = json.loads(path.read_text(encoding="utf-8"))
    rate = account.get("fx_rate_usdjpy")
    if rate is None:
        return None
    rate = float(rate)
    if not 50 < rate < 500:
        raise ValueError(f"account.json fx_rate_usdjpy is outside sanity range: {rate}")
    return rate


def _latest_close_from_parquet(root: Path, ticker: str) -> float | None:
    path = root / "data" / "ohlcv" / f"{ticker.replace('/', '_')}.parquet"
    if not path.exists():
        return None
    import pandas as pd

    frame = pd.read_parquet(path)
    if frame.empty:
        return None
    close = frame["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = pd.to_numeric(close, errors="coerce").dropna()
    if close.empty:
        return None
    return float(close.iloc[-1])


def _enrich_holdings_with_local_prices(root: Path, holdings: dict[str, Any]) -> dict[str, Any]:
    enriched: dict[str, Any] = {}
    for key, holding in holdings.items():
        if not isinstance(holding, dict):
            enriched[key] = holding
            continue
        row = dict(holding)
        if (
            row.get("current_price") is None
            and row.get("current_nav") is None
            and row.get("price") is None
        ):
            ticker = str(row.get("ticker") or key)
            close = _latest_close_from_parquet(root, ticker)
            if close is not None:
                row["current_price"] = close
        enriched[key] = row
    return enriched


def build_plan_from_files(root: Path = BASE_DIR, years: int = 3) -> dict[str, Any]:
    nisa_data = json.loads((root / "nisa_portfolio.json").read_text(encoding="utf-8"))
    holdings = _enrich_holdings_with_local_prices(
        root,
        json.loads((root / "holdings.json").read_text(encoding="utf-8")),
    )
    try:
        from tax_lot import portfolio_lot_snapshot
        lots = portfolio_lot_snapshot().get("lots", {})
        tax_lot_issue = None
    except Exception as exc:
        lots = {}
        tax_lot_issue = f"tax_lot_snapshot_error: {exc}"
    fx_rate = _read_account_fx_rate(root)
    plan = build_migration_plan(
        nisa_data=nisa_data,
        holdings=holdings,
        lots_by_ticker=lots,
        years=years,
        fx_rate_usdjpy=fx_rate,
    )
    if tax_lot_issue:
        plan["actionable"] = False
        plan["tax_lot_source"] = "holding_fallback_due_to_error"
        plan["data_quality_issues"] = [tax_lot_issue]
    else:
        plan["tax_lot_source"] = "tax_lot" if lots else "none"
    return plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--output", default="nisa_migration_plan.json")
    args = parser.parse_args()
    plan = build_plan_from_files(years=args.years)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[nisa-migration] moves={len(plan['moves'])} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rule-based NISA placement suggestions (display only)."""

from __future__ import annotations

from typing import Any


def score_nisa_placement(asset: dict[str, Any]) -> dict[str, Any]:
    """Score an asset for scarce NISA capacity without producing an order."""

    ticker = str(asset.get("ticker") or asset.get("key") or "")
    currency = str(asset.get("currency") or ("JPY" if ticker.endswith(".T") else "USD"))
    investment_type = str(asset.get("investment_type") or "long").lower()
    expected_return = float(
        asset.get("expected_return_pct")
        or {"long": 0.08, "medium": 0.10, "swing": 0.12}.get(investment_type, 0.07)
    )
    dividend_yield = float(asset.get("dividend_yield") or asset.get("yield") or 0.0)
    capital_gain_share = max(
        0.0,
        min(1.0, float(asset.get("capital_gain_share") or (1.0 - dividend_yield / max(expected_return, 0.01)))),
    )
    needs_loss_flexibility = investment_type in {"swing", "short", "speculative"} or bool(
        asset.get("needs_loss_harvest_flexibility")
    )
    foreign_dividend_penalty = currency == "USD" and dividend_yield >= 0.02

    score = expected_return * 500.0 + capital_gain_share * 35.0
    score -= dividend_yield * 300.0
    if needs_loss_flexibility:
        score -= 30.0
    if foreign_dividend_penalty:
        score -= 25.0
    score = round(max(0.0, min(100.0, score)), 1)

    is_fund = bool(asset.get("unit")) or ticker.startswith(("SLIM_", "IFREE_", "NOMURA_"))
    if is_fund and asset.get("auto_invest"):
        recommended_account = "NISAつみたて投資枠"
    elif score >= 60 and not foreign_dividend_penalty and not needs_loss_flexibility:
        recommended_account = "NISA成長投資枠"
    else:
        recommended_account = "課税口座"
    return {
        "ticker": ticker,
        "name": asset.get("name") or ticker,
        "score": score,
        "recommended_account": recommended_account,
        "current_account": asset.get("account"),
        "expected_return_pct": expected_return,
        "dividend_yield": dividend_yield,
        "capital_gain_share": round(capital_gain_share, 3),
        "foreign_dividend_tax_credit_relevant": foreign_dividend_penalty,
        "loss_harvest_flexibility_relevant": needs_loss_flexibility,
        "display_only": True,
    }


def build_placement_proposals(
    nisa_data: dict[str, Any], holdings_data: dict[str, Any]
) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for key, holding in holdings_data.items():
        if not isinstance(holding, dict) or key.startswith("CASH_"):
            continue
        assets.append({**holding, "key": key, "ticker": holding.get("ticker") or key})
    for person in ("husband", "wife"):
        for key, holding in (nisa_data.get(person, {}).get("holdings") or {}).items():
            if isinstance(holding, dict):
                assets.append({**holding, "key": key, "ticker": holding.get("ticker") or key})

    seen: set[tuple[str, str]] = set()
    proposals: list[dict[str, Any]] = []
    for asset in assets:
        result = score_nisa_placement(asset)
        identity = (result["ticker"], str(result.get("current_account") or ""))
        if identity in seen:
            continue
        seen.add(identity)
        if result["recommended_account"] != result.get("current_account"):
            proposals.append(result)
    proposals.sort(key=lambda row: row["score"], reverse=True)
    return proposals

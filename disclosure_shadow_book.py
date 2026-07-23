"""Mechanical disclosure-signal shadow book with explicit broker costs."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from almanac.observability.disclosure_features import read_features
from jp_buyback_parser import buyback_directional_score

BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = BASE_DIR / "disclosure_shadow_config.json"
DEFAULT_OUTPUT_PATH = BASE_DIR / "data" / "disclosure_shadow_book.json"
RAKUTEN_FX_SPREAD_JPY = 0.25


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def signal_from_feature(row: dict, thresholds: dict) -> Optional[dict]:
    """Translate a stored observe-only row into a pre-registered direction."""

    def number(name: str) -> Optional[float]:
        try:
            value = row.get(name)
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    candidates: list[tuple[str, float, int]] = []
    guidance = number("guidance_revision_pct")
    if guidance is not None and abs(guidance) >= thresholds["guidance_revision_pct"]:
        candidates.append(("guidance_revision_pct", abs(guidance), 1 if guidance > 0 else -1))
    monthly = number("monthly_yoy_pct")
    if monthly is not None and abs(monthly) >= thresholds["monthly_yoy_pct"]:
        candidates.append(("monthly_yoy_pct", abs(monthly), 1 if monthly > 0 else -1))
    directional = number("directional_score")
    confidence = number("directional_confidence") or 0.0
    if (
        directional is not None
        and abs(directional) >= thresholds["directional_score"]
        and confidence >= thresholds["directional_confidence"]
    ):
        candidates.append(("directional_score", abs(directional) * confidence, 1 if directional > 0 else -1))
    insider = number("insider_cluster_score")
    if insider is not None and insider >= thresholds["insider_cluster_score"]:
        candidates.append(("insider_cluster_score", min(insider / 3.0, 2.0), 1))
    if row.get("activist_flag") is True:
        candidates.append(("activist_flag", 1.0, 1))
    if row.get("dilution_flag") is True:
        strength = number("dilution_pct") or 0.5
        candidates.append(("dilution_flag", max(0.1, strength), -1))
    if row.get("buyback_flag") is True:
        ratio = number("buyback_ratio_pct")
        strength = buyback_directional_score(ratio) if ratio is not None else 0.5
        candidates.append(("buyback_flag", strength, 1))
    if row.get("going_concern_flag") is True:
        candidates.append(("going_concern_flag", 1.0, -1))
    if not candidates:
        return None
    feature_name, strength, direction = max(candidates, key=lambda item: item[1])
    return {"feature_name": feature_name, "strength": strength, "direction": direction}


def estimate_round_trip_cost_pct(
    *,
    market: str,
    notional_jpy: float,
    fx_rate: float = 150.0,
    config: Optional[dict] = None,
    direction: int = 1,
    horizon_days: int = 0,
    short_credit_type: str = "standard",
) -> float:
    cfg = (config or load_config())["cost_model"]
    if market.upper() == "JP":
        tiers = cfg["jp_spread_bps_each_side"]
        if notional_jpy <= 100_000:
            bps = tiers["notional_lte_100k"]
        elif notional_jpy <= 500_000:
            bps = tiers["notional_lte_500k"]
        else:
            bps = tiers["larger"]
        spread_cost = 2.0 * float(bps) / 10_000.0
        if direction >= 0:
            return spread_cost
        short_cfg = cfg["jp_short"]
        if short_credit_type == "general":
            annual = float(short_cfg["general_borrow_rate_annual_max"])
        else:
            annual = (
                float(short_cfg["standard_borrow_rate_annual"])
                + float(short_cfg["reverse_daily_fee_buffer_annual"])
            )
        return spread_cost + annual * max(0, int(horizon_days)) / 365.0

    notional_usd = notional_jpy / fx_rate
    commission_each = min(
        notional_usd * float(cfg["us_commission_rate_each_side"]),
        float(cfg["us_commission_cap_usd_each_side"]),
    )
    commission_pct = 2.0 * commission_each / notional_usd
    spread_pct = 2.0 * float(cfg["us_spread_bps_each_side"]) / 10_000.0
    fx_pct = 2.0 * RAKUTEN_FX_SPREAD_JPY / fx_rate
    return commission_pct + spread_pct + fx_pct


def _prepare_prices(value: Any) -> pd.DataFrame:
    frame = value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame(value)
    if frame.empty:
        return frame
    if isinstance(frame.columns, pd.MultiIndex):
        # data_fetcher stores yfinance's ('Close', 'TICKER') MultiIndex in parquet;
        # the parquet load path (unlike _download_prices) does not flatten it, so
        # str(('close','cost')) never matches "open"/"close" and the shadow book
        # crashed on every real run. Collapse to the OHLCV level here — the single
        # choke point both the parquet and yfinance paths flow through.
        frame.columns = frame.columns.get_level_values(0)
    frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()
    columns = {str(column).lower(): column for column in frame.columns}
    if "open" not in columns or "close" not in columns:
        raise ValueError("price data needs Open and Close columns")
    return frame.rename(columns={columns["open"]: "Open", columns["close"]: "Close"})


def simulate_shadow_book(
    features: list[dict],
    price_data: dict[str, Any],
    *,
    config: Optional[dict] = None,
    fx_rate: float = 150.0,
) -> dict:
    cfg = config or load_config()
    notional = float(cfg["notional_jpy"])
    trades: list[dict] = []
    signal_tickers: set[str] = set()
    missing_prices: set[str] = set()

    for row in features:
        signal = signal_from_feature(row, cfg["thresholds"])
        if signal is None:
            continue
        ticker = str(row.get("ticker") or "")
        signal_tickers.add(ticker)
        if ticker not in price_data:
            missing_prices.add(ticker)  # surfaced below so a price gap is never silent
            continue
        prices = _prepare_prices(price_data[ticker])
        event_date = pd.Timestamp(str(row.get("publish_time") or "")[:10])
        eligible = prices[prices.index.normalize() > event_date.normalize()]
        if eligible.empty:
            continue
        entry_at = eligible.index[0]
        entry_position = prices.index.get_loc(entry_at)
        entry_price = float(prices.loc[entry_at, "Open"])
        if entry_price <= 0:
            continue
        market = str(row.get("market") or ("JP" if ticker.endswith(".T") else "US"))
        tradeability = {
            "loanable": None,
            "loan_ratio": None,
            "reverse_daily_fee": False,
            "untradeable": False,
            "reasons": [],
        }
        if signal["direction"] < 0:
            if market.upper() == "JP":
                from jp_loanability import evaluate_short_tradeability
                tradeability = evaluate_short_tradeability(ticker)
            elif not bool(cfg.get("us_short_enabled", False)):
                # Rakuten US short availability/cost is unverified, so a US short
                # simulated at long-side costs would flatter the shadow book.
                # Same conservative default as JP "loanable_not_confirmed".
                tradeability = {
                    "loanable": None,
                    "loan_ratio": None,
                    "reverse_daily_fee": False,
                    "untradeable": True,
                    "reasons": ["us_short_not_enabled"],
                }
        for horizon in cfg["horizons"]:
            exit_position = entry_position + int(horizon)
            if exit_position >= len(prices):
                continue
            exit_at = prices.index[exit_position]
            exit_price = float(prices.iloc[exit_position]["Close"])
            theoretical = signal["direction"] * (exit_price - entry_price) / entry_price
            cost_pct = estimate_round_trip_cost_pct(
                market=market,
                notional_jpy=notional,
                fx_rate=fx_rate,
                config=cfg,
                direction=signal["direction"],
                horizon_days=int(horizon),
            )
            untradeable = bool(tradeability["untradeable"])
            net = None if untradeable else theoretical - cost_pct
            trades.append({
                "feature_id": row.get("feature_id"),
                "source_event_id": row.get("source_event_id"),
                "ticker": ticker,
                "market": market,
                "feature_name": signal["feature_name"],
                "direction": signal["direction"],
                "signal_strength": round(signal["strength"], 6),
                "event_at": str(row.get("publish_time") or ""),
                "entry_at": entry_at.isoformat(),
                "exit_at": exit_at.isoformat(),
                "horizon_days": int(horizon),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "theoretical_return": round(theoretical, 8),
                "cost_return": round(cost_pct, 8),
                "borrow_cost_included": signal["direction"] < 0,
                "net_return": round(net, 8) if net is not None else None,
                "pnl_jpy": round(notional * net, 2) if net is not None else None,
                "theory_execution_gap_jpy": round(notional * cost_pct, 2) if not untradeable else None,
                "loanable": tradeability["loanable"],
                "loan_ratio": tradeability["loan_ratio"],
                "reverse_daily_fee": tradeability["reverse_daily_fee"],
                "untradeable": untradeable,
                "untradeable_reasons": tradeability["reasons"],
                "excluded_from_certify": untradeable,
                "observe_only": True,
            })

    by_ticker: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        if trade["untradeable"]:
            continue
        grouped[trade["ticker"]].append(trade)
    for ticker, rows in grouped.items():
        by_ticker[ticker] = {
            "trade_count": len(rows),
            "pnl_jpy": round(sum(row["pnl_jpy"] for row in rows), 2),
            "average_net_return": round(sum(row["net_return"] for row in rows) / len(rows), 8),
        }

    capital = 0.0
    equity_curve = []
    eligible_trades = [trade for trade in trades if not trade["untradeable"]]
    for trade in sorted(eligible_trades, key=lambda row: (row["exit_at"], row["ticker"])):
        capital += trade["pnl_jpy"]
        equity_curve.append({"at": trade["exit_at"], "cumulative_pnl_jpy": round(capital, 2)})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "observe_only_shadow",
        "trade_count": len(trades),
        "tradeable_trade_count": len(eligible_trades),
        "untradeable_trade_count": len(trades) - len(eligible_trades),
        "trades": trades,
        "by_ticker": by_ticker,
        "equity_curve": equity_curve,
        "total_pnl_jpy": round(capital, 2),
        "total_theory_execution_gap_jpy": round(
            sum(trade["theory_execution_gap_jpy"] for trade in eligible_trades), 2
        ),
        # Coverage so a missing-price gap can never masquerade as "no signal".
        "signal_ticker_count": len(signal_tickers),
        "priced_ticker_count": len(signal_tickers - missing_prices),
        "missing_price_tickers": sorted(missing_prices),
    }


def _download_prices(tickers: list[str], *, lookback_days: int) -> dict[str, pd.DataFrame]:
    """Best-effort OHLCV via yfinance for tickers without a local parquet.

    The JP scan universe is not maintained by ``data_fetcher`` (which only tracks
    holdings), so without this fallback the shadow book finds 0 prices for JP
    names and produces 0 trades *silently*. Any failure skips that ticker.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            df = yf.download(
                ticker, period=f"{max(lookback_days, 5)}d",
                auto_adjust=False, progress=False,
            )
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):  # single-ticker MultiIndex frame
            df.columns = df.columns.get_level_values(0)
        if "Open" in df.columns and "Close" in df.columns:
            out[ticker] = df[["Open", "Close"]].dropna()
    return out


def _load_prices(
    tickers: set[str], *, fetch_missing: bool = True, lookback_days: int = 450
) -> dict[str, pd.DataFrame]:
    """Local parquet first; yfinance fallback for whatever is missing."""
    out: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for ticker in tickers:
        path = BASE_DIR / "data" / "ohlcv" / f"{ticker}.parquet"
        if path.exists():
            try:
                out[ticker] = pd.read_parquet(path)
                continue
            except Exception:  # noqa: BLE001 — corrupt parquet → try the network
                pass
        missing.append(ticker)
    if fetch_missing and missing:
        out.update(_download_prices(missing, lookback_days=lookback_days))
    return out


def main(argv: Optional[list[str]] = None) -> dict:
    parser = argparse.ArgumentParser(description="開示シグナルの機械ルール・シャドーブック")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    args = parser.parse_args(argv)
    features = read_features()
    prices = _load_prices({str(row.get("ticker") or "") for row in features})
    result = simulate_shadow_book(features, prices)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(output)
    print(json.dumps({k: result[k] for k in ("trade_count", "total_pnl_jpy")}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()

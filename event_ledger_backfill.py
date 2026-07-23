"""
event_ledger_backfill.py — trade_history.csv → event_ledger 一括取込

整理タスク #1: P1-18 で追加した event_ledger は空のままだったため、
過去 1 年分の trade_history.csv が NAV/TWR/tax_lot/broker_reconcile から見えない死蔵だった。
本スクリプトは trade_history.csv を読み、deterministic event_id で event_ledger に
idempotent に append する。再実行しても重複は発生しない。

idempotency:
  event_id = sha256(timestamp|action|ticker|price|quantity)[:16] で固定。
  既に同じ id がある行は append_event の duplicate check で skip。

仮定:
  - trade_history.csv の通貨は ticker suffix と国内投信の疑似 ticker で判定
    (.T / .JP / SLIM_* / IFREE_* / NOMURA_* / MNXACT → JPY, else USD)
  - 国内投信の価格は 1万口あたり NAV なので、ledger には 1口あたり価格へ正規化して保存する
  - account は不明なので "特定" を仮置き。実 broker import (P1-19) でより正確に上書きされる
  - USD の場合の fx_rate は必須。CSV に FX 列が無い場合は --fx-rate で明示する。
    現在値を暗黙使用すると historical P&L/tax lot が静かに歪むため、デフォルトでは拒否する。

使い方:
  python event_ledger_backfill.py            # dry-run
  python event_ledger_backfill.py --apply --fx-rate 150.25
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
TRADE_CSV = BASE_DIR / "trade_history.csv"

JPY_SUFFIXES = (".T", ".JP", ".JPX", ".OS")
JPY_FUND_PREFIXES = ("SLIM_", "IFREE_", "NOMURA_", "MNXACT")
JPY_CASH_TICKERS = {"CASH_JPY", "CASH_JPY_SBI", "CASH_JPY_SBI_WIFE"}
USD_CASH_TICKERS = {"CASH_USD", "GS_MMF_USD"}


def _is_domestic_fund_ticker(ticker: str) -> bool:
    t = (ticker or "").upper().strip()
    return t.startswith(JPY_FUND_PREFIXES)


def _ticker_currency(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    if t in USD_CASH_TICKERS:
        return "USD"
    if t in JPY_CASH_TICKERS:
        return "JPY"
    if t.endswith(JPY_SUFFIXES) or _is_domestic_fund_ticker(t):
        return "JPY"
    return "USD"


def _ledger_price(ticker: str, csv_price: float) -> tuple[float, dict]:
    if _is_domestic_fund_ticker(ticker):
        return round(float(csv_price) / 10000.0, 8), {
            "price_unit": "per_unit",
            "csv_price_unit": "per_10000_units",
            "price_scale": 0.0001,
        }
    return csv_price, {"price_unit": "per_share"}


def _direction(action_str: str) -> str:
    s = (action_str or "").upper().strip()
    if s == "BUY":
        return "buy"
    if s == "SELL":
        return "sell"
    return ""


def _parse_qty(s: str) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _make_event_id(row: dict) -> str:
    raw = "|".join(str(row.get(k, "")) for k in ("日時", "アクション", "ティッカー", "価格", "株数"))
    return "backfill_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _parse_optional_float(value) -> float | None:
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _row_fx_rate(
    row: dict,
    *,
    currency: str,
    explicit_fx: float | None,
    allow_current_fx: bool,
) -> tuple[float | None, str | None]:
    if currency != "USD":
        return None, None
    for key in ("fx_rate_usdjpy", "FX", "為替", "為替レート", "USDJPY"):
        fx = _parse_optional_float(row.get(key))
        if fx is not None:
            return fx, "csv"
    if explicit_fx is not None:
        return explicit_fx, "explicit"
    if not allow_current_fx:
        return None, None
    try:
        from utils import get_fx_rate_cached
        fx_now, _ = get_fx_rate_cached()
        return float(fx_now), "current"
    except Exception:
        return None, None


def _normalize_ts(s: str) -> str:
    s = (s or "").strip()
    # CSV は "YYYY-MM-DD HH:MM" 形式
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return dt.isoformat(timespec="seconds")
    except ValueError:
        return s  # そのまま (parse 失敗は event_ledger 側で扱う)


def backfill(
    *,
    apply: bool = False,
    csv_path: Path = TRADE_CSV,
    fx_rate_usdjpy: float | None = None,
    allow_current_fx: bool = False,
) -> dict:
    if not csv_path.exists():
        return {"error": f"CSV not found: {csv_path}", "inserted": 0, "skipped": 0, "duplicates": 0}

    from event_ledger import append_event

    inserted = 0
    duplicates = 0
    skipped = 0
    errors = []
    sample = []

    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            direction = _direction(row.get("アクション", ""))
            ticker = (row.get("ティッカー") or "").strip()
            try:
                csv_price = float(str(row.get("価格") or "").replace(",", ""))
            except (TypeError, ValueError):
                csv_price = 0.0
            qty = _parse_qty(row.get("株数", ""))

            if not direction or not ticker or csv_price <= 0 or qty <= 0:
                skipped += 1
                continue

            currency = _ticker_currency(ticker)
            price, price_meta = _ledger_price(ticker, csv_price)
            fx_rate, fx_source = _row_fx_rate(
                row,
                currency=currency,
                explicit_fx=fx_rate_usdjpy,
                allow_current_fx=allow_current_fx,
            )
            event_id = _make_event_id(row)
            occurred_at = _normalize_ts(row.get("日時", ""))

            if currency == "USD" and fx_rate is None:
                skipped += 1
                errors.append({
                    "row": row,
                    "error": "USD trade requires fx_rate_usdjpy. Add FX column or pass --fx-rate.",
                })
                continue

            if not apply:
                # dry-run: 出力だけ
                sample.append({
                    "event_id":    event_id,
                    "occurred_at": occurred_at,
                    "ticker":      ticker,
                    "direction":   direction,
                    "quantity":    qty,
                    "price":       price,
                    "currency":    currency,
                    "fx_rate_usdjpy": fx_rate,
                    "fx_source": fx_source,
                })
                inserted += 1
                continue

            try:
                r = append_event(
                    event_type="trade",
                    occurred_at=occurred_at,
                    ticker=ticker,
                    direction=direction,
                    quantity=qty,
                    price=price,
                    currency=currency,
                    fx_rate_usdjpy=fx_rate,
                    account="特定",
                    source="backfill",
                    event_id=event_id,
                    note=(
                        "trade_history.csv backfill"
                        + (f" (provisional FX {fx_rate})" if fx_source == "explicit" else "")
                        + (" (current FX explicitly allowed)" if fx_source == "current" else "")
                    ),
                    raw_payload={
                        "row": row,
                        "fx_source": fx_source,
                        "provisional_fx": fx_source == "explicit",
                        "csv_price": csv_price,
                        "ledger_price": price,
                        **price_meta,
                    },
                )
            except Exception as e:
                errors.append({"row": row, "error": str(e)})
                continue

            if r.get("duplicate"):
                duplicates += 1
            else:
                inserted += 1

    return {
        "csv_rows": inserted + duplicates + skipped,
        "inserted": inserted,
        "duplicates": duplicates,
        "skipped":  skipped,
        "errors":   errors,
        "dry_run":  not apply,
        "sample":   sample[:5] if not apply else [],
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="event_ledger backfill from trade_history.csv")
    parser.add_argument("--apply", action="store_true", help="実際に DB に書き込む (default: dry-run)")
    parser.add_argument("--csv",   default=str(TRADE_CSV))
    parser.add_argument("--fx-rate", type=float, default=None, help="CSV に FX 列が無い USD trade へ使う明示レート")
    parser.add_argument(
        "--allow-current-fx",
        action="store_true",
        help="現在の USDJPY を backfill に使う（履歴精度が落ちるため通常非推奨）",
    )
    args = parser.parse_args()

    r = backfill(
        apply=args.apply,
        csv_path=Path(args.csv),
        fx_rate_usdjpy=args.fx_rate,
        allow_current_fx=args.allow_current_fx,
    )
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    if r.get("errors"):
        sys.exit(1)


if __name__ == "__main__":
    _main()

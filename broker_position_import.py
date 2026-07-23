"""
broker_position_import.py — 楽天 assetbalance CSV の保有証券同期

楽天証券の assetbalance(all)_*.csv を保有証券の source of truth として、
holdings.json の楽天口座分（broker 未指定/楽天証券）を同期する。

現金は broker_balance_import.py が担当。本モジュールは株式・投信・外貨建MMFのみ。

使い方:
  python broker_position_import.py --rakuten-csv ~/Downloads/assetbalance.csv
  python broker_position_import.py --rakuten-csv ~/Downloads/assetbalance.csv --apply
"""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from broker_balance_import import _date_from_filename, _num, _read_csv_rows
from utils import atomic_write_json, load_json_strict, process_lock

BASE_DIR = Path(__file__).parent
HOLDINGS_FILE = BASE_DIR / "holdings.json"
RECONCILE_LOG = BASE_DIR / "broker_position_reconcile_log.jsonl"

SECURITY_TYPES = {"国内株式", "米国株式", "投資信託", "外貨建MMF"}
RAKUTEN_BROKER = "楽天証券"

FUND_NAME_MAP = {
    "eMAXIS Slim 米国株式(S&P500)": "SLIM_SP500",
    "eMAXIS Slim 全世界株式(オール・カントリー)(オルカン)": "SLIM_ORCAN",
    "GS米ドルファンド": "GS_MMF_USD",
}

KEY_SUFFIX_BY_ACCOUNT = {
    "一般": "ippan",
    "NISA成長投資枠": "NISA",
    "NISAつみたて投資枠": "NISA_TSUMITATE",
}


@dataclass
class BrokerPosition:
    security_type: str
    ticker: str
    name: str
    account: str
    quantity: float
    quantity_unit: Optional[str]
    entry_price: float
    currency: str
    current_price: Optional[float]
    current_price_unit: Optional[str]
    value_jpy: Optional[float]
    value_foreign: Optional[float]
    unrealized_jpy: Optional[float]
    broker: str = RAKUTEN_BROKER

    @property
    def match_key(self) -> tuple[str, str]:
        return (self.ticker.upper(), self.account)


def _ticker_from_row(row: list[str]) -> str:
    security_type, code, name = row[0], row[1].strip(), row[2].strip()
    if security_type == "国内株式":
        return f"{code}.T" if code and code.isdigit() and not code.endswith(".T") else code
    if security_type == "米国株式":
        return code.upper()
    mapped = FUND_NAME_MAP.get(name)
    if mapped:
        return mapped
    return code or name


def _currency_from_row(row: list[str]) -> str:
    if row[0] == "米国株式":
        return "USD"
    if row[0] == "外貨建MMF":
        return "USD"
    return "JPY"


def _value_foreign(row: list[str]) -> Optional[float]:
    if len(row) <= 15:
        return None
    raw = row[15]
    if not raw or raw == "-":
        return None
    return _num(raw)


def parse_rakuten_positions(path: Path) -> list[BrokerPosition]:
    rows = _read_csv_rows(path)
    positions: list[BrokerPosition] = []
    for row in rows:
        if len(row) < 18 or row[0] not in SECURITY_TYPES:
            continue
        if row[0] == "外貨預り金":
            continue
        ticker = _ticker_from_row(row)
        currency = _currency_from_row(row)
        qty = _num(row[4])
        entry_price = _num(row[6])
        current_price = _num(row[8])
        if not ticker or qty is None or entry_price is None:
            continue

        if row[0] == "外貨建MMF":
            # 既存 portfolio_manager は GS_MMF_USD を「USD価値 shares × 1 USD」として扱う。
            foreign_value = _value_foreign(row)
            if foreign_value is not None:
                qty = foreign_value
            entry_price = 1.0
            current_price = 1.0

        positions.append(BrokerPosition(
            security_type=row[0],
            ticker=ticker,
            name=row[2].strip(),
            account=row[3].strip() or "特定",
            quantity=float(qty),
            quantity_unit=(row[5].strip() or None),
            entry_price=float(entry_price),
            currency=currency,
            current_price=float(current_price) if current_price is not None else None,
            current_price_unit=(row[9].strip() or None),
            value_jpy=_num(row[14]),
            value_foreign=_value_foreign(row),
            unrealized_jpy=_num(row[16]),
        ))
    return positions


def _is_rakuten_holding(key: str, rec: dict) -> bool:
    if key.startswith("CASH_"):
        return False
    broker = rec.get("broker")
    if broker and broker != RAKUTEN_BROKER:
        return False
    return True


def _same_ticker(rec: dict, ticker: str) -> bool:
    return str(rec.get("ticker") or "").upper() == ticker.upper()


def _find_existing_key(pos: BrokerPosition, holdings: dict, desired_by_ticker_count: dict[str, int], used: set[str]) -> Optional[str]:
    exact = []
    same_ticker = []
    for key, rec in holdings.items():
        if key in used or not isinstance(rec, dict) or not _is_rakuten_holding(key, rec):
            continue
        if not _same_ticker(rec, pos.ticker):
            continue
        same_ticker.append(key)
        if rec.get("account") == pos.account:
            exact.append(key)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact[0]
    # MSFT のように account だけ古い場合は、同tickerが単独なら既存キーを更新する。
    if desired_by_ticker_count.get(pos.ticker.upper(), 0) == 1 and len(same_ticker) == 1:
        return same_ticker[0]
    return None


def _base_key(pos: BrokerPosition) -> str:
    ticker = pos.ticker
    if ticker.endswith(".T"):
        ticker = ticker[:-2]
    return ticker.replace(" ", "_")


def _new_key(pos: BrokerPosition, holdings: dict) -> str:
    base = _base_key(pos)
    candidates = [base]
    suffix = KEY_SUFFIX_BY_ACCOUNT.get(pos.account)
    if suffix:
        candidates.insert(0, f"{base}_{suffix}")
    for c in candidates:
        if c not in holdings:
            return c
    idx = 2
    while f"{base}_{idx}" in holdings:
        idx += 1
    return f"{base}_{idx}"


def _apply_position_to_record(pos: BrokerPosition, existing: Optional[dict], *, as_of: Optional[str]) -> dict:
    rec = deepcopy(existing) if isinstance(existing, dict) else {}
    rec["ticker"] = pos.ticker
    rec["name"] = rec.get("name") or pos.name
    rec["account"] = pos.account
    rec["currency"] = pos.currency
    rec["shares"] = round(pos.quantity, 4)
    rec["entry_price"] = round(pos.entry_price, 6)
    rec["investment_type"] = rec.get("investment_type") or "long"
    if pos.quantity_unit == "口":
        rec["unit"] = "口"
    elif "unit" not in rec:
        rec["unit"] = None
    if pos.security_type == "投資信託" and pos.current_price is not None:
        rec["current_nav"] = round(pos.current_price, 6)
    if pos.security_type == "外貨建MMF":
        rec["current_nav"] = 1.0
    if "broker" not in rec:
        rec["broker"] = RAKUTEN_BROKER
    rec["note"] = f"楽天CSV保有同期 {as_of or ''}".strip()
    return rec


def build_reconciled_holdings(
    *,
    positions: list[BrokerPosition],
    holdings_path: Path = HOLDINGS_FILE,
    as_of: Optional[str] = None,
    full_snapshot: bool = True,
) -> tuple[dict, dict]:
    holdings = load_json_strict(holdings_path)
    if not isinstance(holdings, dict):
        raise ValueError("holdings.json が dict ではありません")
    next_holdings = deepcopy(holdings)
    used: set[str] = set()

    desired_by_ticker_count: dict[str, int] = {}
    for p in positions:
        desired_by_ticker_count[p.ticker.upper()] = desired_by_ticker_count.get(p.ticker.upper(), 0) + 1

    updates = []
    adds = []
    for pos in positions:
        key = _find_existing_key(pos, next_holdings, desired_by_ticker_count, used)
        if key is None:
            key = _new_key(pos, next_holdings)
            before = None
            next_holdings[key] = _apply_position_to_record(pos, None, as_of=as_of)
            adds.append({"key": key, "after": next_holdings[key], "broker": asdict(pos)})
        else:
            before = deepcopy(next_holdings[key])
            next_holdings[key] = _apply_position_to_record(pos, next_holdings[key], as_of=as_of)
            if before != next_holdings[key]:
                updates.append({"key": key, "before": before, "after": next_holdings[key], "broker": asdict(pos)})
        used.add(key)

    desired_keys = set(used)
    stale = []
    for key, rec in holdings.items():
        if not isinstance(rec, dict) or not _is_rakuten_holding(key, rec):
            continue
        if key not in desired_keys:
            stale.append({"key": key, "holding": rec})

    # Codex P1 #8: 完全スナップショット (assetbalance(all) CSV = 楽天全保有) なら、
    # CSV に無い楽天保有 = 売却済みとみなし shares=0 化して NAV/売却可能数量から除外する。
    # 旧挙動は stale を記録するだけで holdings に残し続けていた (売却済みが NAV に残る)。
    zeroed = []
    if full_snapshot and stale:
        if not positions:
            # 空/破損 CSV を「完全」と誤認して全保有を 0 化する事故を防ぐ。
            raise ValueError(
                "full_snapshot=True だが positions が空です。空/破損 CSV の可能性があるため"
                " stale の 0 化を中止しました (stale 検出のみ行うには full_snapshot=False)。"
            )
        for item in stale:
            key = item["key"]
            rec = next_holdings.get(key)
            if not isinstance(rec, dict):
                continue
            before = deepcopy(rec)
            rec["shares"] = 0
            rec["reconcile_zeroed"] = True
            rec["reconcile_zeroed_at"] = as_of or datetime.now().isoformat(timespec="seconds")
            zeroed.append({"key": key, "before": before, "after": deepcopy(rec)})

    diff = {
        "updates": updates,
        "adds": adds,
        "stale": stale,
        "zeroed": zeroed,
        "position_count": len(positions),
    }
    return next_holdings, diff


def apply_reconcile(*, rakuten_csv: Path, apply: bool = False, full_snapshot: bool = True) -> dict:
    positions = parse_rakuten_positions(rakuten_csv)
    as_of = _date_from_filename(rakuten_csv)
    with process_lock("portfolio_ledger"):
        next_holdings, diff = build_reconciled_holdings(
            positions=positions,
            holdings_path=HOLDINGS_FILE,
            as_of=as_of,
            full_snapshot=full_snapshot,
        )
        if apply:
            atomic_write_json(HOLDINGS_FILE, next_holdings)
            log_entry = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "kind": "broker_position_reconcile",
                "source": str(rakuten_csv),
                "as_of": as_of,
                "diff": diff,
            }
            with RECONCILE_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    return {"dry_run": not apply, "as_of": as_of, **diff}


def _main() -> None:
    parser = argparse.ArgumentParser(description="Import Rakuten broker positions from assetbalance CSV")
    parser.add_argument("--rakuten-csv", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--partial", action="store_true",
                        help="CSV が楽天全保有の完全スナップショットでない場合に指定 "
                             "(stale を 0 化せず検出のみ)")
    args = parser.parse_args()

    result = apply_reconcile(rakuten_csv=Path(args.rakuten_csv), apply=args.apply,
                             full_snapshot=not args.partial)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not args.apply:
        print("\n[dry-run] 反映するには --apply を付けて再実行してください")


if __name__ == "__main__":
    _main()

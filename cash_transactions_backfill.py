"""
cash_transactions_backfill.py — cash_transactions.json -> event_ledger cash_flow backfill

nav_recorder.py の Modified Dietz TWR は event_ledger の cash_flow event を
外部入出金として扱う。cash_transactions.json に過去の入出金があるのに
ledger に cash_flow が無い状態だと、入出金を運用成績として誤認する。

このスクリプトは cash_transactions.json の既存 transaction を、transaction id
を event_id として idempotent に event_ledger へ反映する。再実行しても重複しない。

USD transaction は履歴 FX が必須。transaction に fx_rate_usdjpy / FX / 為替 などの
列が無い場合は --fx-rate で明示する。現在値の暗黙利用はしない。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils import load_json_strict

BASE_DIR = Path(__file__).parent
CASH_TX_FILE = BASE_DIR / "cash_transactions.json"


def _parse_float(value) -> Optional[float]:
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


def _normalize_type(value: str) -> Optional[str]:
    s = (value or "").strip().lower()
    if s in {"deposit", "in", "入金"}:
        return "deposit"
    if s in {"withdraw", "withdrawal", "out", "出金"}:
        return "withdraw"
    return None


def _direction(tx_type: str) -> str:
    return "in" if tx_type == "deposit" else "out"


def _normalize_timestamp(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return datetime.now().isoformat(timespec="seconds")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).isoformat(timespec="seconds")
        except ValueError:
            pass
    return s


def _event_id(tx: dict) -> str:
    tx_id = str(tx.get("id") or "").strip()
    if tx_id:
        return tx_id
    raw = "|".join(
        str(tx.get(k, ""))
        for k in ("timestamp", "type", "currency", "broker", "amount", "description")
    )
    return "cash_backfill_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _fx_rate(tx: dict, *, currency: str, explicit_fx: Optional[float]) -> tuple[Optional[float], Optional[str]]:
    if currency != "USD":
        return None, None
    for key in ("fx_rate_usdjpy", "FX", "為替", "為替レート", "USDJPY"):
        fx = _parse_float(tx.get(key))
        if fx is not None:
            return fx, "transaction"
    if explicit_fx is not None:
        return explicit_fx, "explicit"
    return None, None


def backfill(
    *,
    apply: bool = False,
    tx_path: Path = CASH_TX_FILE,
    fx_rate_usdjpy: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> dict:
    if not tx_path.exists():
        return {"error": f"cash transaction file not found: {tx_path}", "inserted": 0, "duplicates": 0, "skipped": 0}

    data = load_json_strict(tx_path)
    txs = data.get("transactions") if isinstance(data, dict) else None
    if not isinstance(txs, list):
        return {"error": "cash_transactions.json の transactions が list ではありません", "inserted": 0, "duplicates": 0, "skipped": 0}

    from event_ledger import append_event

    inserted = 0
    duplicates = 0
    skipped = 0
    errors: list[dict] = []
    sample: list[dict] = []

    for tx in txs:
        if not isinstance(tx, dict):
            skipped += 1
            errors.append({"transaction": tx, "error": "transaction が dict ではありません"})
            continue

        tx_type = _normalize_type(str(tx.get("type") or ""))
        amount = _parse_float(tx.get("amount"))
        currency = (tx.get("currency") or "JPY").upper().strip()
        if tx_type is None or amount is None:
            skipped += 1
            errors.append({"transaction": tx, "error": "type/amount が不正です"})
            continue
        if currency not in {"JPY", "USD"}:
            skipped += 1
            errors.append({"transaction": tx, "error": f"未対応通貨です: {currency}"})
            continue

        fx, fx_source = _fx_rate(tx, currency=currency, explicit_fx=fx_rate_usdjpy)
        if currency == "USD" and fx is None:
            skipped += 1
            errors.append({
                "transaction": tx,
                "error": "USD cash transaction requires fx_rate_usdjpy. Add FX column or pass --fx-rate.",
            })
            continue

        event_id = _event_id(tx)
        occurred_at = _normalize_timestamp(str(tx.get("timestamp") or ""))
        description = (tx.get("description") or "").strip()
        note = "cash_transactions.json backfill"
        if description:
            note += f": {description}"
        if fx_source == "explicit":
            note += f" (provisional FX {fx})"

        item = {
            "event_id": event_id,
            "occurred_at": occurred_at,
            "direction": _direction(tx_type),
            "quantity": amount,
            "currency": currency,
            "fx_rate_usdjpy": fx,
            "broker": tx.get("broker"),
            "note": note,
        }

        if not apply:
            sample.append(item)
            inserted += 1
            continue

        try:
            r = append_event(
                event_type="cash_flow",
                occurred_at=occurred_at,
                direction=item["direction"],
                quantity=amount,
                price=1.0,
                currency=currency,
                fx_rate_usdjpy=fx,
                account=tx.get("broker"),
                source="cash_transactions_backfill",
                note=note,
                raw_payload={
                    "transaction": tx,
                    "fx_source": fx_source,
                    "provisional_fx": fx_source == "explicit",
                },
                event_id=event_id,
                db_path=db_path,
            )
        except Exception as e:
            errors.append({"transaction": tx, "error": str(e)})
            continue

        if r.get("duplicate"):
            duplicates += 1
        else:
            inserted += 1

    return {
        "transactions": len(txs),
        "inserted": inserted,
        "duplicates": duplicates,
        "skipped": skipped,
        "errors": errors,
        "dry_run": not apply,
        "sample": sample[:5] if not apply else [],
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="cash_transactions.json backfill to event_ledger cash_flow")
    parser.add_argument("--apply", action="store_true", help="実際に DB に書き込む (default: dry-run)")
    parser.add_argument("--file", default=str(CASH_TX_FILE), help="cash_transactions.json path")
    parser.add_argument("--fx-rate", type=float, default=None, help="FX 列が無い USD transaction へ使う明示レート")
    args = parser.parse_args()

    r = backfill(
        apply=args.apply,
        tx_path=Path(args.file),
        fx_rate_usdjpy=args.fx_rate,
    )
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    if r.get("errors"):
        raise SystemExit(1)


if __name__ == "__main__":
    _main()

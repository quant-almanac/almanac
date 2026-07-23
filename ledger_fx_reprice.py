"""
ledger_fx_reprice.py — event_ledger の暫定 USDJPY を履歴レートで補正する。

event_ledger_backfill.py / cash_transactions_backfill.py は、CSV 側に当時の為替が無い場合
明示された --fx-rate を使い、raw_payload.provisional_fx=True として記録する。
また、初期実装で取り込まれた backfill 行には provisional marker が無いものがある。

このスクリプトは該当 USD event を yfinance の USDJPY=X 日次 Close または CSV から取得した
履歴レートで再計算し、fx_rate_usdjpy / amount_jpy / raw_payload を idempotent に更新する。

使い方:
  python ledger_fx_reprice.py                         # dry-run
  python ledger_fx_reprice.py --apply                 # DB 更新
  python ledger_fx_reprice.py --fx-csv usdjpy.csv     # オフライン補正
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from almanac.runtime_config import resolve_db_path

BASE_DIR = Path(__file__).parent
DB_PATH = resolve_db_path(BASE_DIR)
DEFAULT_TYPES = ("trade", "cash_flow")
LEGACY_BACKFILL_SOURCES = {"backfill", "cash_transactions_backfill"}


def _parse_payload(raw: object) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_event_date(value: str) -> date:
    s = (value or "").strip()
    if not s:
        raise ValueError("occurred_at is empty")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _parse_float(value: object) -> Optional[float]:
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


def _amount_jpy(row: dict, fx_rate: float) -> float:
    # 符号ルール (buy/margin_buy/cover/out=負) は event_ledger を単一の真実とし、
    # ここで再実装しない (Codex P1 #2: margin_buy/cover の符号反転バグ防止)。
    from event_ledger import _to_amount_jpy

    quantity = _parse_float(row.get("quantity"))
    price = _parse_float(row.get("price"))
    if quantity is None or price is None:
        raise ValueError(f"quantity/price が不正です: event_id={row.get('event_id')}")
    amount = _to_amount_jpy(
        quantity=quantity,
        price=price,
        currency=(row.get("currency") or "USD"),
        fx_rate_usdjpy=fx_rate,
        direction=row.get("direction"),
    )
    if amount is None:
        raise ValueError(
            f"amount_jpy 計算不能 (currency/fx 不正): event_id={row.get('event_id')}, "
            f"currency={row.get('currency')!r}, fx={fx_rate!r}"
        )
    return amount


def _is_candidate(row: dict, *, include_legacy_backfill: bool) -> tuple[bool, str]:
    if (row.get("currency") or "").upper() != "USD":
        return False, "not_usd"
    if row.get("event_type") not in DEFAULT_TYPES:
        return False, "unsupported_type"

    payload = _parse_payload(row.get("raw_payload"))

    # ── protect 条件を先に判定 (Codex 2026-05-17 P2: _is_candidate を締める) ──
    # 既に historical 補正済み (fx_reprice_history がある) → 再 reprice しない
    if isinstance(payload.get("fx_reprice_history"), list) and payload["fx_reprice_history"]:
        return False, "already_repriced"
    # 明示的に確定済み (provisional_fx=False) → 触らない
    if payload.get("provisional_fx") is False:
        return False, "explicitly_confirmed"
    # CSV 由来の FX (fx_source="csv") → 触らない
    if payload.get("fx_source") == "csv" or (isinstance(payload.get("fx_source"), str) and payload["fx_source"].startswith("historical")):
        return False, f"protected_by_source_{payload.get('fx_source')}"

    # ── 補正対象の判定 ──
    if payload.get("provisional_fx") is True:
        return True, "marked_provisional"
    if payload.get("fx_source") in {"explicit", "current"}:
        return True, f"payload_{payload.get('fx_source')}"

    note = row.get("note") or ""
    if "provisional FX" in note or "current FX explicitly allowed" in note:
        return True, "note_marker"

    if include_legacy_backfill and not payload and row.get("source") in LEGACY_BACKFILL_SOURCES:
        return True, "legacy_unmarked_backfill"

    return False, "already_historical_or_csv"


def _resolve_fx_rate(
    fx_by_date: dict[str, float],
    event_day: date,
    *,
    lookback_days: int,
) -> tuple[Optional[float], Optional[str]]:
    for i in range(lookback_days + 1):
        d = event_day - timedelta(days=i)
        key = d.isoformat()
        if key in fx_by_date:
            return fx_by_date[key], key
    return None, None


def _load_fx_csv(path: Path) -> dict[str, float]:
    rates: dict[str, float] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            day = (row.get("date") or row.get("Date") or row.get("日時") or "").strip()[:10]
            if not day:
                continue
            fx = None
            for key in ("fx_rate_usdjpy", "USDJPY", "Close", "close", "終値", "為替"):
                fx = _parse_float(row.get(key))
                if fx is not None:
                    break
            if fx is not None:
                rates[day] = fx
    return rates


def fetch_usdjpy_daily(
    start_day: date,
    end_day: date,
    *,
    pair: str = "USDJPY=X",
) -> dict[str, float]:
    import yfinance as yf
    from utils import init_yfinance_timeout

    init_yfinance_timeout()
    # yfinance の end は exclusive なので 1 日足す。
    raw = yf.download(
        pair,
        start=start_day.isoformat(),
        end=(end_day + timedelta(days=1)).isoformat(),
        progress=False,
        auto_adjust=True,
    )
    if raw is None or raw.empty:
        raise RuntimeError(f"yfinance returned empty FX data: {pair} {start_day}..{end_day}")

    close = raw["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]

    rates: dict[str, float] = {}
    for idx, value in close.dropna().items():
        fx = _parse_float(value)
        if fx is not None:
            rates[idx.date().isoformat()] = fx
    if not rates:
        raise RuntimeError("yfinance FX data has no usable Close values")
    return rates


def _clean_note(note: Optional[str], *, fx_rate: float, fx_date: str, fx_source: str) -> str:
    base = note or ""
    base = re.sub(r"\s*\(provisional FX [^)]+\)", "", base)
    base = base.replace(" (current FX explicitly allowed)", "")
    base = re.sub(r"\s*\(historical FX [^)]+\)", "", base)
    return f"{base} (historical FX {fx_date}: {fx_rate:.4f} via {fx_source})".strip()


def _query_candidate_rows(db_path: Path, types: Iterable[str]) -> list[dict]:
    from event_ledger import init_schema

    init_schema(db_path)
    selected = tuple(types)
    placeholders = ",".join("?" for _ in selected)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT *
              FROM ledger_events
             WHERE currency = 'USD'
               AND event_type IN ({placeholders})
             ORDER BY occurred_at ASC, id ASC
            """,
            selected,
        ).fetchall()
    return [dict(r) for r in rows]


def reprice_usd_events(
    *,
    apply: bool = False,
    db_path: Path = DB_PATH,
    fx_by_date: Optional[dict[str, float]] = None,
    fx_csv: Optional[Path] = None,
    pair: str = "USDJPY=X",
    types: Iterable[str] = DEFAULT_TYPES,
    include_legacy_backfill: bool = True,
    lookback_days: int = 7,
) -> dict:
    rows = _query_candidate_rows(db_path, types)
    # Codex P1 #3: 既に訂正イベントで置換済みの原行は再 reprice しない (二重訂正防止)。
    from event_ledger import _superseded_ids
    already_superseded = _superseded_ids(rows)
    candidates: list[tuple[dict, str, date]] = []
    skipped = 0
    for row in rows:
        if row.get("event_id") in already_superseded:
            skipped += 1
            continue
        ok, reason = _is_candidate(row, include_legacy_backfill=include_legacy_backfill)
        if not ok:
            skipped += 1
            continue
        try:
            candidates.append((row, reason, _parse_event_date(row["occurred_at"])))
        except Exception as e:
            skipped += 1
            candidates.append((dict(row, _candidate_error=str(e)), reason, date.min))

    errors: list[dict] = []
    candidates = [c for c in candidates if c[2] != date.min]
    if not candidates:
        return {
            "dry_run": not apply,
            "updated": 0,
            "candidates": 0,
            "skipped": skipped,
            "errors": errors,
            "sample": [],
        }

    if fx_by_date is None:
        if fx_csv is not None:
            fx_by_date = _load_fx_csv(fx_csv)
            fx_source = f"csv:{fx_csv.name}"
        else:
            start_day = min(day for _, _, day in candidates) - timedelta(days=lookback_days)
            end_day = max(day for _, _, day in candidates)
            fx_by_date = fetch_usdjpy_daily(start_day, end_day, pair=pair)
            fx_source = "yfinance"
    else:
        fx_source = "provided"

    updates: list[dict] = []
    for row, reason, event_day in candidates:
        fx, matched_day = _resolve_fx_rate(fx_by_date, event_day, lookback_days=lookback_days)
        if fx is None or matched_day is None:
            errors.append({
                "event_id": row.get("event_id"),
                "occurred_at": row.get("occurred_at"),
                "error": f"FX rate not found within {lookback_days} days",
            })
            continue

        old_fx = row.get("fx_rate_usdjpy")
        amount_jpy = _amount_jpy(row, fx)
        payload = _parse_payload(row.get("raw_payload"))
        history = payload.get("fx_reprice_history")
        if not isinstance(history, list):
            history = []
        history.append({
            "repriced_at": datetime.now().isoformat(timespec="seconds"),
            "previous_fx_rate_usdjpy": old_fx,
            "previous_amount_jpy": row.get("amount_jpy"),
            "reason": reason,
        })
        payload.update({
            "fx_source": f"historical_{fx_source}",
            "provisional_fx": False,
            "historical_fx_date": matched_day,
            "previous_fx_rate_usdjpy": old_fx,
            "fx_reprice_history": history,
            "supersedes": row["event_id"],          # Codex P1 #3: 原 event を置換する訂正
            "original_amount_jpy": row.get("amount_jpy"),
        })
        updates.append({
            "id": row["id"],
            "event_id": row["event_id"],
            "occurred_at": row["occurred_at"],
            "event_type": row["event_type"],
            "ticker": row.get("ticker"),
            "reason": reason,
            "old_fx_rate_usdjpy": old_fx,
            "new_fx_rate_usdjpy": fx,
            "fx_date": matched_day,
            "old_amount_jpy": row.get("amount_jpy"),
            "new_amount_jpy": amount_jpy,
            "delta_amount_jpy": round(amount_jpy - float(row.get("amount_jpy") or 0.0), 2),
            "note": _clean_note(row.get("note"), fx_rate=fx, fx_date=matched_day, fx_source=fx_source),
            "raw_payload": payload,
            # 訂正イベント append 用の原 event フィールド (sample からは除外)
            "_direction": row.get("direction"),
            "_quantity": row.get("quantity"),
            "_price": row.get("price"),
            "_currency": row.get("currency"),
            "_account": row.get("account"),
        })

    if apply and updates:
        # Codex P1 #3: 原行を UPDATE せず、訂正イベントを append する (append-only 維持)。
        # 訂正は原 event の全コピー + 修正 FX/amount。決定論 event_id で idempotent。
        # query_events が supersedes により原行を除外するので、読み取りは訂正後を見る。
        from event_ledger import append_event
        for item in updates:
            corr_id = f"{item['event_id']}:fxreprice:{item['fx_date']}"
            append_event(
                event_type=item["event_type"],
                occurred_at=item["occurred_at"],
                ticker=item.get("ticker"),
                direction=item.get("_direction"),
                quantity=item.get("_quantity"),
                price=item.get("_price"),
                currency=item.get("_currency"),
                fx_rate_usdjpy=item["new_fx_rate_usdjpy"],
                account=item.get("_account"),
                source="fx_reprice",
                note=item["note"],
                raw_payload=item["raw_payload"],
                event_id=corr_id,
                db_path=db_path,
            )

    return {
        "dry_run": not apply,
        "updated": len(updates) if apply else 0,
        "would_update": 0 if apply else len(updates),
        "candidates": len(candidates),
        "skipped": skipped,
        "errors": errors,
        "sample": [
            {k: v for k, v in item.items()
             if k not in {"id", "raw_payload", "note"} and not k.startswith("_")}
            for item in updates[:10]
        ],
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Reprice provisional USD FX rates in event_ledger")
    parser.add_argument("--apply", action="store_true", help="DB を更新する (default: dry-run)")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite DB path")
    parser.add_argument("--fx-csv", default=None, help="date,fx_rate_usdjpy/Close を持つ FX CSV")
    parser.add_argument("--pair", default="USDJPY=X", help="yfinance FX ticker")
    parser.add_argument("--types", default="trade,cash_flow", help="対象 event_type（カンマ区切り）")
    parser.add_argument("--no-legacy-backfill", action="store_true", help="marker 無し旧 backfill 行を対象外にする")
    parser.add_argument("--lookback-days", type=int, default=7, help="休日などで当日 Close が無い場合に遡る日数")
    args = parser.parse_args()

    result = reprice_usd_events(
        apply=args.apply,
        db_path=Path(args.db),
        fx_csv=Path(args.fx_csv) if args.fx_csv else None,
        pair=args.pair,
        types=tuple(t.strip() for t in args.types.split(",") if t.strip()),
        include_legacy_backfill=not args.no_legacy_backfill,
        lookback_days=args.lookback_days,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get("errors"):
        raise SystemExit(1)


if __name__ == "__main__":
    _main()

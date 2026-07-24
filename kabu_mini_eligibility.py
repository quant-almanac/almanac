"""
Local eligibility helper for Rakuten kabu mini odd-lot cash buys.

The official eligible universe changes over time and is broker-selected, so
AI prose alone must not be treated as eligibility. This module only trusts a
local ledger when deciding whether a JP cash buy can use 1-share sizing.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent
LEDGER_PATH = BASE_DIR / "data" / "kabu_mini_eligible.json"
VERIFICATION_NEEDED_PATH = BASE_DIR / "data" / "kabu_mini_verification_needed.json"


def normalize_ticker(ticker: str | None) -> str:
    raw = str(ticker or "").strip().upper()
    if raw.isdigit() and len(raw) == 4:
        return f"{raw}.T"
    return raw


def normalize_channel(channel: str | None) -> str | None:
    raw = str(channel or "").strip().lower()
    if not raw:
        return None
    if "realtime" in raw or "real_time" in raw or "rt" == raw:
        return "realtime"
    if "open" in raw or "寄付" in raw or "寄り付き" in raw or "yoritsuki" in raw:
        return "open"
    return raw


def action_requests_kabu_mini(action: dict | None) -> bool:
    if not isinstance(action, dict):
        return False
    channel = str(action.get("execution_channel") or action.get("broker_channel") or "").lower()
    if channel.startswith("rakuten_kabu_mini") or channel.startswith("kabu_mini"):
        return True
    text = f"{action.get('action') or ''} {action.get('reason') or ''} {action.get('amount_hint') or ''}"
    return "かぶミニ" in text or "kabu_mini" in text.lower()


def _load_ledger(path: Path | None = None) -> Any:
    ledger_path = path or LEDGER_PATH
    try:
        return json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _lookup_entry(ledger: Any, ticker: str) -> Any:
    if isinstance(ledger, list):
        return ticker if ticker in {normalize_ticker(x) for x in ledger} else None
    if not isinstance(ledger, dict):
        return None
    tickers = ledger.get("tickers", ledger)
    if isinstance(tickers, list):
        return ticker if ticker in {normalize_ticker(x) for x in tickers} else None
    if isinstance(tickers, dict):
        for key, value in tickers.items():
            if normalize_ticker(key) == ticker:
                return value
    return None


def is_kabu_mini_eligible(
    ticker: str | None,
    *,
    channel: str | None = None,
    ledger_path: Path | None = None,
) -> bool:
    normalized_ticker = normalize_ticker(ticker)
    if not normalized_ticker.endswith(".T"):
        return False
    entry = _lookup_entry(_load_ledger(ledger_path), normalized_ticker)
    if entry is None:
        return False
    if isinstance(entry, bool):
        return entry
    if isinstance(entry, str):
        return normalize_ticker(entry) == normalized_ticker
    if not isinstance(entry, dict):
        return False
    if entry.get("eligible") is False or entry.get("buy_allowed") is False or entry.get("sell_only") is True:
        return False
    requested_channel = normalize_channel(channel)
    channels = entry.get("channels")
    if requested_channel and isinstance(channels, list):
        return requested_channel in {normalize_channel(ch) for ch in channels}
    if requested_channel and requested_channel in {"open", "realtime"} and requested_channel in entry:
        return bool(entry.get(requested_channel))
    return bool(entry.get("eligible", True))


def _clean_jpy(value: Any) -> int | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return int(round(numeric))


def build_kabu_mini_verification_record(
    action: dict | None,
    *,
    reason: str = "kabu_mini_eligibility_unknown",
    estimated_jpy: Any = None,
    threshold_jpy: Any = None,
    max_single_action_cap_jpy: Any = None,
    source: str = "phase1_post_filter",
) -> dict[str, Any]:
    action = action if isinstance(action, dict) else {}
    requested_channel = str(action.get("execution_channel") or action.get("broker_channel") or "").strip()
    record: dict[str, Any] = {
        "ticker": normalize_ticker(action.get("ticker")),
        "requested_channel": requested_channel or None,
        "normalized_channel": normalize_channel(requested_channel),
        "action_type": str(action.get("type") or "").strip().lower() or None,
        "amount_hint": action.get("amount_hint"),
        "reason": reason,
        "source": source,
    }
    if action.get("action"):
        record["action"] = action.get("action")
    for key, value in (
        ("estimated_notional_jpy", estimated_jpy),
        ("threshold_jpy", threshold_jpy),
        ("max_single_action_cap_jpy", max_single_action_cap_jpy),
    ):
        clean = _clean_jpy(value)
        if clean is not None:
            record[key] = clean
    return record


def _verification_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        normalize_ticker(item.get("ticker")),
        str(item.get("requested_channel") or ""),
        str(item.get("action_type") or ""),
    )


def record_kabu_mini_verification_needed(
    records: Iterable[dict[str, Any]],
    *,
    path: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    target = path or VERIFICATION_NEEDED_PATH
    now_s = now or datetime.now(timezone.utc).isoformat()
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    existing_items = raw.get("items", raw) if isinstance(raw, dict) else raw
    if isinstance(existing_items, list):
        for item in existing_items:
            if isinstance(item, dict) and normalize_ticker(item.get("ticker")):
                merged[_verification_key(item)] = dict(item)

    for record in records:
        if not isinstance(record, dict):
            continue
        ticker = normalize_ticker(record.get("ticker"))
        if not ticker:
            continue
        row = dict(record)
        row["ticker"] = ticker
        row.setdefault("requested_channel", None)
        row.setdefault("action_type", None)
        row["last_seen_at"] = now_s
        merged[_verification_key(row)] = {**merged.get(_verification_key(row), {}), **row}

    payload = {
        "updated_at": now_s,
        "items": sorted(
            merged.values(),
            key=lambda item: (
                str(item.get("ticker") or ""),
                str(item.get("requested_channel") or ""),
                str(item.get("action_type") or ""),
            ),
        ),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    from utils import atomic_write_json

    atomic_write_json(target, payload)
    return payload

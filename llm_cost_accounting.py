"""Normalize LLM usage rows and summarize estimated spend."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PRICES_PER_MILLION = {
    # prefix match (_price_for_model) は挿入順なので、特定的なキーを
    # 汎用プレフィックスより先に置くこと。
    # Opus 4.6 以降は $5/$25。旧世代 (Opus 4.0/4.1) のみ $15/$75。
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},  # post-intro (2026-09-01+); see SONNET_5_INTRO_PRICE
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-3-5-haiku": {"input": 0.8, "output": 4.0},
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-v4-flash": {"input": 0.27, "output": 1.10},
    "deepseek-v4-pro": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
}

WEB_SEARCH_PRICE_USD_PER_REQUEST = 10.0 / 1_000
BATCH_API_DISCOUNT_FACTOR = 0.5

# Sonnet 5 intro pricing runs through 2026-08-31; self-expires to the
# standard $3/$15 rate in DEFAULT_PRICES_PER_MILLION with no code change needed.
SONNET_5_INTRO_PRICE = {"input": 2.0, "output": 10.0}
SONNET_5_INTRO_PRICE_EXPIRES = datetime(2026, 8, 31, 23, 59, 59)


def _price_for_model(model: str, *, as_of: datetime | None = None) -> dict[str, float] | None:
    lowered = model.lower()
    if "claude-sonnet-5" in lowered:
        now = as_of if as_of is not None else datetime.now()
        if now <= SONNET_5_INTRO_PRICE_EXPIRES:
            return SONNET_5_INTRO_PRICE
        return DEFAULT_PRICES_PER_MILLION["claude-sonnet-5"]
    for prefix, price in DEFAULT_PRICES_PER_MILLION.items():
        if prefix in lowered:
            return price
    if "deepseek" in lowered:
        return DEFAULT_PRICES_PER_MILLION["deepseek-chat"]
    return None


def estimate_cost_usd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    price = _price_for_model(model or "")
    if price is None or (input_tokens is None and output_tokens is None):
        return None
    total = (
        (int(input_tokens or 0) * price["input"])
        + (int(output_tokens or 0) * price["output"])
    ) / 1_000_000
    return round(total, 8)


def _server_tool_cost_usd(row: dict[str, Any]) -> float:
    server_tool_use = row.get("server_tool_use") or {}
    if not isinstance(server_tool_use, dict):
        return 0.0
    try:
        web_search_requests = int(server_tool_use.get("web_search_requests") or 0)
    except (TypeError, ValueError):
        web_search_requests = 0
    return round(web_search_requests * WEB_SEARCH_PRICE_USD_PER_REQUEST, 8)


def _is_batch_api_usage(row: dict[str, Any]) -> bool:
    return bool(row.get("batch") is True or str(row.get("api_type") or "").lower() == "batch")


def normalize_usage_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized.setdefault("provider", "anthropic" if "claude" in str(row.get("model", "")).lower() else "external")
    normalized.setdefault("lane", row.get("role") or "unknown")
    normalized.setdefault("input_tokens", row.get("prompt_tokens"))
    normalized.setdefault("output_tokens", row.get("completion_tokens"))
    token_cost = estimate_cost_usd(
        str(normalized.get("model") or ""),
        normalized.get("input_tokens"),
        normalized.get("output_tokens"),
    )
    if token_cost is not None and _is_batch_api_usage(normalized):
        token_cost = round(token_cost * BATCH_API_DISCOUNT_FACTOR, 8)
        normalized.setdefault("batch_discount_factor", BATCH_API_DISCOUNT_FACTOR)
    tool_cost = _server_tool_cost_usd(normalized)
    if tool_cost:
        normalized.setdefault("tool_cost_usd", tool_cost)
    if normalized.get("cost_usd") is None:
        normalized["cost_usd"] = (
            round(float(token_cost or 0.0) + tool_cost, 8)
            if token_cost is not None or tool_cost
            else None
        )
    return normalized


def read_usage_rows(path: Path | str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(normalize_usage_row(row))
    return rows


def summarize_month(
    rows: Iterable[dict[str, Any]],
    *,
    month: str | None = None,
) -> dict[str, Any]:
    target = month or datetime.now().strftime("%Y-%m")
    by_model: dict[str, float] = {}
    by_lane: dict[str, float] = {}
    unpriced = 0
    calls = 0
    total = 0.0
    for raw in rows:
        row = normalize_usage_row(raw)
        if str(row.get("ts") or "")[:7] != target:
            continue
        calls += 1
        cost = row.get("cost_usd")
        if cost is None:
            unpriced += 1
            continue
        value = float(cost)
        total += value
        model = str(row.get("model") or "unknown")
        lane = str(row.get("lane") or "unknown")
        by_model[model] = by_model.get(model, 0.0) + value
        by_lane[lane] = by_lane.get(lane, 0.0) + value
    return {
        "month": target,
        "calls": calls,
        "cost_usd": round(total, 6),
        "unpriced_calls": unpriced,
        "by_model": {k: round(v, 6) for k, v in sorted(by_model.items())},
        "by_lane": {k: round(v, 6) for k, v in sorted(by_lane.items())},
    }

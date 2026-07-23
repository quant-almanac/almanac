"""Alert-only watcher for newly listed large US IPOs.

The watcher is intentionally conservative: it can notify and persist candidates
that are outside ``tickers.json["all"]``, but it never mutates the investable
universe and never creates orders. Human onboarding still happens through
``download_tickers.py`` ``NEW_LISTINGS``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import anthropic

from analyst.llm_client import _append_llm_call_log
from utils import atomic_write_json

try:
    from alert import send_telegram
except ImportError:
    def send_telegram(msg: str) -> bool:
        print(f"[TELEGRAM] {msg}")
        return True


BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "data" / "ipo_watch_state.json"
TICKERS_FILE = BASE_DIR / "tickers.json"
MODEL_ID = "claude-haiku-4-5-20251001"
HIGH_CONFIDENCE = 0.75

SEARCH_QUERIES = [
    "major US IPO debut new NASDAQ NYSE listing this week 2026",
    "largest IPO recent stock market debut NASDAQ NYSE 2026 ticker",
    "newly listed US stock IPO raised billion ticker exchange 2026",
]

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="[ipo_watch] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

ListingExtractor = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
SearchFn = Callable[[str], list[dict[str, Any]]]
TelegramSender = Callable[[str], Any]


_IPO_EXTRACT_TOOL = {
    "name": "extract_ipo_listings",
    "description": "Extract recent large US IPO or new-listing candidates from public search snippets.",
    "input_schema": {
        "type": "object",
        "properties": {
            "listings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "ticker": {"type": "string"},
                        "exchange": {"type": "string"},
                        "ipo_date": {"type": "string"},
                        "size_or_rank": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["company", "ticker", "exchange", "ipo_date", "confidence"],
                },
            }
        },
        "required": ["listings"],
    },
}


def _now_iso(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_ticker(value: object) -> str:
    ticker = str(value or "").strip().upper()
    ticker = ticker.removeprefix("$")
    if ":" in ticker:
        ticker = ticker.rsplit(":", 1)[-1]
    ticker = re.sub(r"[^A-Z0-9.\-]", "", ticker)
    return ticker


def _valid_us_ticker(ticker: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker or ""))


def _confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _web_search(query: str) -> list[dict[str, Any]]:
    """Reuse the Claude web-search pattern from geopolitical_monitor."""
    from geopolitical_monitor import _web_search as geopolitical_web_search

    return geopolitical_web_search(query)


def _read_json_strict(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"malformed JSON: {path}: {exc}") from exc


def fetch_search_results(
    *,
    queries: Iterable[str] = SEARCH_QUERIES,
    web_search_fn: SearchFn = _web_search,
    max_workers: int = 3,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(web_search_fn, query): query for query in queries}
        for future in as_completed(futures):
            query = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                log.error("web search failed (%s): %s", query[:60], exc)
                continue
            if not isinstance(rows, list):
                log.warning("web search returned non-list for query=%s", query[:60])
                continue
            for row in rows:
                if isinstance(row, dict):
                    items.append({**row, "query": query})
    return items


def extract_listings_with_claude(search_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract structured IPO candidates from public snippets only."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required for ipo_watch LLM extraction")
    compact = [
        {
            "headline": str(row.get("headline") or row.get("title") or "")[:240],
            "snippet": str(row.get("snippet") or row.get("text") or "")[:500],
            "url": row.get("url"),
            "source": row.get("source"),
            "published_at": row.get("published_at") or row.get("date"),
            "query": row.get("query"),
        }
        for row in search_results[:30]
    ]
    prompt = (
        "以下は公開web検索結果です。直近の米国大型IPOまたはNYSE/NASDAQ新規上場だけを抽出してください。\n"
        "book、保有、口座、発注情報は含まれていません。ticker が不明なもの、未上場・噂だけのもの、"
        "米国外の上場は捨ててください。confidence は 0.0-1.0。\n\n"
        f"{json.dumps(compact, ensure_ascii=False)}"
    )
    client = anthropic.Anthropic(api_key=api_key)
    started = time.monotonic()
    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=1200,
            tools=[_IPO_EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "extract_ipo_listings"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        _append_llm_call_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "role": "ipo_watch_extractor",
            "model": MODEL_ID,
            "use_tool": True,
            "max_tokens": 1200,
            "elapsed_sec": round(time.monotonic() - started, 2),
            "prompt_chars": len(prompt),
            "search_result_count": len(compact),
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        })
        raise
    usage = getattr(response, "usage", None)
    _append_llm_call_log({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": "ipo_watch_extractor",
        "model": MODEL_ID,
        "use_tool": True,
        "max_tokens": 1200,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": len(prompt),
        "search_result_count": len(compact),
        "status": "ok",
        "stop_reason": getattr(response, "stop_reason", None),
        "content_types": [getattr(block, "type", None) for block in getattr(response, "content", [])],
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    })
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "extract_ipo_listings":
            listings = block.input.get("listings", []) if isinstance(block.input, dict) else []
            return listings if isinstance(listings, list) else []
    raise RuntimeError("Claude did not return extract_ipo_listings tool_use")


def _load_universe(tickers_path: Path | str = TICKERS_FILE) -> set[str]:
    payload = _read_json_strict(Path(tickers_path), {})
    if not isinstance(payload, dict) or not isinstance(payload.get("all"), list):
        raise RuntimeError(f"{tickers_path} must contain tickers.json['all'] list")
    return {_normalize_ticker(ticker) for ticker in payload["all"] if _normalize_ticker(ticker)}


def _load_state(path: Path | str = STATE_FILE) -> dict[str, Any]:
    state = _read_json_strict(Path(path), {})
    if not state:
        return {"schema_version": "1.0.0", "candidates": []}
    if not isinstance(state, dict) or not isinstance(state.get("candidates", []), list):
        raise RuntimeError(f"malformed ipo watch state: {path}")
    state.setdefault("schema_version", "1.0.0")
    state.setdefault("candidates", [])
    return state


def _candidate_from_listing(listing: dict[str, Any], *, detected_at: str) -> dict[str, Any] | None:
    ticker = _normalize_ticker(listing.get("ticker"))
    if not _valid_us_ticker(ticker):
        return None
    company = str(listing.get("company") or "").strip()
    if not company:
        return None
    return {
        "ticker": ticker,
        "company": company[:160],
        "exchange": str(listing.get("exchange") or "").strip().upper()[:40],
        "ipo_date": str(listing.get("ipo_date") or "").strip()[:40],
        "size_or_rank": str(listing.get("size_or_rank") or listing.get("size") or "").strip()[:160],
        "confidence": _confidence(listing.get("confidence")),
        "detected_at": detected_at,
        "status": "universe_missing",
        "onboarding_path": "download_tickers.py:NEW_LISTINGS",
    }


def _telegram_message(candidate: dict[str, Any]) -> str:
    return (
        f"新規上場検知: {candidate['ticker']} ({candidate['company']})— ユニバース外。"
        "download_tickers.py の NEW_LISTINGS に追加検討"
    )


def run_watch(
    *,
    base_dir: Path | str = BASE_DIR,
    state_path: Path | str | None = None,
    tickers_path: Path | str | None = None,
    web_search_fn: SearchFn = _web_search,
    extract_fn: ListingExtractor = extract_listings_with_claude,
    telegram_sender: TelegramSender = send_telegram,
    notify: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one alert-only IPO watch pass."""
    root = Path(base_dir)
    state_file = Path(state_path) if state_path is not None else root / "data" / "ipo_watch_state.json"
    tickers_file = Path(tickers_path) if tickers_path is not None else root / "tickers.json"
    detected_at = _now_iso(now)

    universe = _load_universe(tickers_file)
    state = _load_state(state_file)
    existing_state_tickers = {
        _normalize_ticker(row.get("ticker"))
        for row in state.get("candidates", [])
        if isinstance(row, dict)
    }

    search_results = fetch_search_results(web_search_fn=web_search_fn)
    listings = extract_fn(search_results)
    if not isinstance(listings, list):
        raise RuntimeError("ipo listing extractor must return a list")

    new_candidates: list[dict[str, Any]] = []
    skipped_existing: list[str] = []
    skipped_dedup: list[str] = []
    telegram_messages: list[str] = []

    for raw in listings:
        if not isinstance(raw, dict):
            continue
        candidate = _candidate_from_listing(raw, detected_at=detected_at)
        if candidate is None:
            continue
        ticker = candidate["ticker"]
        if ticker in universe:
            skipped_existing.append(ticker)
            continue
        if ticker in existing_state_tickers:
            skipped_dedup.append(ticker)
            continue
        state["candidates"].append(candidate)
        existing_state_tickers.add(ticker)
        new_candidates.append(candidate)
        if notify and candidate["confidence"] >= HIGH_CONFIDENCE:
            msg = _telegram_message(candidate)
            # ALMANAC: telegram disabled — ai_analysis only
            # telegram_sender(msg)
            candidate["notified_at"] = detected_at
            telegram_messages.append(msg)

    state["updated_at"] = detected_at
    state["last_scan"] = {
        "searched_items": len(search_results),
        "extracted_listings": len(listings),
        "new_candidates": len(new_candidates),
        "skipped_existing": sorted(set(skipped_existing)),
        "skipped_dedup": sorted(set(skipped_dedup)),
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_file, state)
    return {
        "ok": True,
        "state_path": str(state_file),
        "new_candidates": new_candidates,
        "telegram_messages": telegram_messages,
        "skipped_existing": sorted(set(skipped_existing)),
        "skipped_dedup": sorted(set(skipped_dedup)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alert-only watcher for large recent US IPOs")
    parser.add_argument("--no-notify", action="store_true", help="do not send Telegram alerts")
    parser.add_argument("--state-path", type=Path, default=STATE_FILE)
    parser.add_argument("--tickers-path", type=Path, default=TICKERS_FILE)
    args = parser.parse_args(argv)

    result = run_watch(
        state_path=args.state_path,
        tickers_path=args.tickers_path,
        notify=not args.no_notify,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

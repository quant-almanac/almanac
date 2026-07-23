"""
ingest_disclosures.py — orchestrate public-disclosure ingestion → observe_only features.

For each source-provided disclosure item, extract evidence-backed numeric features
(via DeepSeek through ``almanac.llm_safety``) and store them as observe_only rows.
The "調査員" runner, not a trader.

Two hard safety gates so nothing happens by accident:

  * **Network** — each source fetcher (``edgar_fetcher.fetch_edgar_filings``,
    ``edinet_fetcher.fetch_edinet_documents``) returns ``[]`` unless ``live=True``.
  * **Spend** — :func:`ingest_items` calls the LLM only when a ``transport`` is
    injected (tests/offline) OR ``live_llm=True`` is passed explicitly. The
    default is a dry run that reports what *would* be processed.

Already-extracted disclosures (same ``source_event_id`` + extractor version) are
skipped via :func:`almanac.observability.disclosure_features.feature_exists`, so
re-runs never re-pay the LLM.

Universe: the scan set is PUBLIC (``tickers.json`` / index constituents), NOT
derived from holdings — so even the ticker list reveals nothing about the book.
"""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

from almanac.observability.disclosure_features import feature_exists
from almanac.observability.ids import compute_source_event_id
from disclosure_feature_extractor import PROMPT_VERSION, extract_features
from insider_restrictions import filter_allowed_tickers, is_restricted_ticker
from utils import load_environment_secrets

BASE_DIR = Path(__file__).parent
DEFAULT_MODEL = "deepseek-chat"
# Pre-registered fixed slice (curated ~30 liquid large-caps, one per GICS sector).
# This is the DEFAULT scan set so a stray --us cannot fan out over the full ~894
# tickers.json and rack up DeepSeek spend; the full universe is opt-in behind
# --full-universe. See disclosure_universe.json for the pre-registration rationale.
DISCLOSURE_UNIVERSE_PATH = BASE_DIR / "disclosure_universe.json"
DISCLOSURE_UNIVERSE_JP_PATH = BASE_DIR / "disclosure_universe_jp.json"

__all__ = [
    "ingest_items",
    "collect_us_items",
    "collect_jp_items",
    "collect_news_items",
    "collect_tdnet_items",
    "load_scan_universe",
    "resolve_scan_universe",
    "main",
]


@contextmanager
def _ingest_lock(store_path: Path | str | None):
    """Exclusive lock over a disclosure's check+extract+store critical section.

    Uses a sidecar ``<store>.ingest.lock`` — distinct from the feature store's own
    append lock so the two never nest (which would deadlock) — so two concurrent
    ingest processes cannot both pass feature_exists() and double-charge the LLM
    for the same disclosure (R-round P2). flock self-releases on process exit.
    """
    from almanac.observability.disclosure_features import default_store_path
    p = Path(store_path) if store_path is not None else default_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".ingest.lock")
    with open(lock_path, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def ingest_items(
    items: list[dict],
    *,
    model_id: str = DEFAULT_MODEL,
    prompt_version: str = PROMPT_VERSION,
    store_path: Path | str | None = None,
    transport: Optional[Callable[..., tuple[str, dict[str, Any]]]] = None,
    live_llm: bool = False,
    api_key: Optional[str] = None,
    skip_existing: bool = True,
    log_path: Path | str | None = None,
    fsync: bool = True,
) -> dict:
    """Extract + store observe_only features for each disclosure ``item``.

    Spend gate: the LLM is invoked only if ``transport`` is provided (offline /
    test) or ``live_llm=True``. Otherwise items are counted as ``skipped_no_llm``.

    Returns a report dict with counts and any per-item errors.
    """
    report = {
        "seen": 0, "skipped_existing": 0, "skipped_no_llm": 0,
        "skipped_restricted": 0,
        "extracted": 0, "deterministic_written": 0,
        "deterministic_duplicate": 0, "failed": 0, "errors": [],
    }
    for item in items:
        report["seen"] += 1
        if is_restricted_ticker(item.get("ticker")):
            report["skipped_restricted"] += 1
            continue
        try:
            sid = compute_source_event_id(
                item.get("source", ""),
                native_doc_id=item.get("native_doc_id"),
                source_url=item.get("source_url"),
            )
        except ValueError as e:
            report["failed"] += 1
            report["errors"].append(f"no stable anchor: {e}")
            continue

        # Reservation lock held across the dedup check + extract + store, so two
        # concurrent ingest processes can't both pass feature_exists() and
        # double-charge the LLM for the same disclosure (R-round P2).
        with _ingest_lock(store_path):
            from deterministic_disclosure_features import append_deterministic_feature
            # A single malformed item (e.g. an unresolved-ticker stake that slipped
            # past the fetchers' guards, or a bad publish_time) must skip itself, not
            # abort the whole batch. The deterministic lane's contract is "value or
            # None, never crash" — make_feature raises ValueError on bad input, so we
            # contain it here rather than let it kill every subsequent disclosure.
            try:
                deterministic = append_deterministic_feature(
                    item, store_path=store_path, fsync=fsync
                )
            except (ValueError, KeyError) as e:
                report["failed"] += 1
                report["errors"].append(f"deterministic: {e}")
                deterministic = None
            if deterministic:
                key = "deterministic_duplicate" if deterministic.get("duplicate") else "deterministic_written"
                report[key] += 1

            if item.get("deterministic_only"):
                continue

            if skip_existing and feature_exists(
                sid, model_id=model_id, prompt_version=prompt_version, path=store_path
            ):
                report["skipped_existing"] += 1
                continue

            # Spend gate — never call the LLM implicitly.
            if transport is None and not live_llm:
                report["skipped_no_llm"] += 1
                continue

            res = extract_features(
                item,
                api_key=api_key or os.environ.get("DEEPSEEK_API_KEY", ""),
                model_id=model_id,
                prompt_version=prompt_version,
                transport=transport,
                store_path=store_path,
                log_path=log_path,
                fsync=fsync,
            )
            if res.get("ok"):
                report["extracted"] += 1
            else:
                report["failed"] += 1
                report["errors"].append(res.get("error"))
    return report


def collect_us_items(tickers: list[str], *, live: bool = False,
                     limit_per_ticker: int = 20,
                     include_insider_clusters: bool = True) -> list[dict]:
    """Recent SEC filing events for each ticker (network gated by ``live``)."""
    from edgar_fetcher import fetch_edgar_filings
    items: list[dict] = []
    for t in filter_allowed_tickers(tickers):
        items.extend(fetch_edgar_filings(t, live=live, limit=limit_per_ticker))
        if include_insider_clusters:
            from insider_cluster import fetch_insider_cluster
            items.extend(fetch_insider_cluster(t, live=live))
    return items


def _apply_universe_filter(items: list[dict], tickers: Optional[list[str]]) -> list[dict]:
    """Keep items within the fixed universe, but never drop a known-activist stake.

    Activist large-shareholding filings are a rare, high-signal, *event-defined*
    lane — the activist chooses the target, not us — so a fixed pre-registered
    universe can't bound them. Those (``activist_flag is True``) are universe-
    exempt; everything else stays bounded to the pre-registered names.
    """
    allowed = set(filter_allowed_tickers(tickers or []))
    items = [item for item in items if not is_restricted_ticker(item.get("ticker"))]
    if not allowed:
        return items
    return [
        item for item in items
        if item.get("ticker") in allowed or item.get("activist_flag") is True
    ]


def collect_jp_items(
    dates: list[str], *, live: bool = False, tickers: Optional[list[str]] = None
) -> list[dict]:
    """EDINET disclosure events for each business day (network gated by ``live``)."""
    from edinet_fetcher import fetch_edinet_documents
    items: list[dict] = []
    for d in dates:
        items.extend(fetch_edinet_documents(d, live=live))
    return _apply_universe_filter(items, tickers)


def collect_news_items(tickers: list[str], *, live: bool = False,
                       limit_per_ticker: int = 10) -> list[dict]:
    """Per-ticker news headlines as disclosure items (network gated by ``live``)."""
    from news_fetcher import fetch_news_items
    items: list[dict] = []
    for t in tickers:
        items.extend(fetch_news_items(t, live=live, limit=limit_per_ticker))
    return items


def collect_tdnet_items(
    queries: list[str], *, live: bool = False, tickers: Optional[list[str]] = None
) -> list[dict]:
    """TDnet disclosures for each day-query as items (network gated by ``live``)."""
    from tdnet_fetcher import fetch_tdnet_items
    items: list[dict] = []
    for q in queries:
        items.extend(fetch_tdnet_items(q, live=live))
    return _apply_universe_filter(items, tickers)


def load_scan_universe(path: "Path | str | None" = None) -> list[str]:
    """Fixed PUBLIC scan universe from ``tickers.json`` — NOT holdings-derived.

    Accepts: a JSON list; ``{"tickers": [...]}``; a **categorized** dict whose
    values are ticker lists (``{"sp500_major": [...], "etf_list": [...]}``) →
    flattened + de-duped; or a flat ``{ticker: meta}`` dict → keys.
    Returns ``[]`` if the file is absent or unparseable.
    """
    p = Path(path) if path is not None else BASE_DIR / "tickers.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if isinstance(data, list):
        return filter_allowed_tickers(data)
    if isinstance(data, dict):
        if isinstance(data.get("tickers"), list):
            return filter_allowed_tickers(data["tickers"])
        # Categorized dict: flatten all list values, dedup preserving order.
        # (Bug fix: previously returned the CATEGORY NAMES as if they were tickers,
        #  so --us tried to fetch EDGAR for "sp500_major" etc.)
        flat: list[str] = []
        for v in data.values():
            if isinstance(v, list):
                flat.extend(str(t) for t in v)
        if flat:
            seen: set[str] = set()
            out: list[str] = []
            for t in flat:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
            return filter_allowed_tickers(out)
        # No list values → keys are tickers (flat {ticker: meta} dict).
        return filter_allowed_tickers(data.keys())
    return []


def resolve_scan_universe(
    *, universe_path: "Path | str | None" = None, full: bool = False,
    market: str = "US",
) -> list[str]:
    """Pick the US scan universe (precedence: explicit path > full > curated default).

    * ``universe_path`` — load exactly this file (override).
    * ``full=True`` — the full ``tickers.json`` (~894 names; large DeepSeek spend).
    * default — the pre-registered curated slice (:data:`DISCLOSURE_UNIVERSE_PATH`),
      falling back to ``tickers.json`` only if the curated file is missing.

    Keeping the curated slice as the default is the spend guard: a bare ``--us``
    can never silently fan out over the full universe.
    """
    if universe_path is not None:
        return load_scan_universe(universe_path)
    if market.upper() == "JP":
        return load_scan_universe(DISCLOSURE_UNIVERSE_JP_PATH)
    if full:
        return load_scan_universe(BASE_DIR / "tickers.json")
    if DISCLOSURE_UNIVERSE_PATH.exists():
        return load_scan_universe(DISCLOSURE_UNIVERSE_PATH)
    return load_scan_universe()


def _try_load_secrets() -> None:
    """Load local ALMANAC secrets if DEEPSEEK_API_KEY is absent from env.

    Cron jobs use run_with_secrets.sh; this makes direct ``python ingest_disclosures.py``
    invocations work without needing to manually source the secrets file first.
    """
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    load_environment_secrets()


def main(argv: list[str] | None = None) -> dict:
    """CLI. ``--live`` is the single explicit opt-in for network AND spend."""
    _try_load_secrets()
    import argparse

    ap = argparse.ArgumentParser(description="Ingest public disclosures → observe_only features")
    ap.add_argument("--live", action="store_true",
                    help="fetch from SEC/EDINET AND call DeepSeek (incurs spend)")
    ap.add_argument("--us", action="store_true", help="ingest US (EDGAR) filings")
    ap.add_argument("--jp-date", help="ingest EDINET filings for YYYY-MM-DD (JST)")
    ap.add_argument("--limit", type=int, default=20, help="max filings per US ticker")
    ap.add_argument("--news", action="store_true", help="ingest per-ticker news (Yahoo RSS)")
    ap.add_argument("--tdnet", nargs="?", const="today",
                    help="ingest TDnet for a day (today|yesterday|YYYYMMDD)")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip fetching full filing text (use metadata body only)")
    ap.add_argument("--universe", help="path to a scan-universe JSON (overrides default)")
    ap.add_argument("--full-universe", action="store_true",
                    help="scan the full tickers.json (~894 names; large spend) "
                         "instead of the curated pre-registered slice")
    ap.add_argument("--push", action="store_true",
                    help="push newly stored high-signal rows to Telegram (observe_only label)")
    args = ap.parse_args(argv)

    us_universe = resolve_scan_universe(
        universe_path=args.universe if args.us else None,
        full=args.full_universe,
        market="US",
    )
    jp_universe = resolve_scan_universe(
        universe_path=args.universe if (not args.us and (args.jp_date or args.tdnet)) else None,
        market="JP",
    )

    items: list[dict] = []
    if args.us:
        items += collect_us_items(us_universe, live=args.live,
                                  limit_per_ticker=args.limit)
    if args.jp_date:
        items += collect_jp_items([args.jp_date], live=args.live, tickers=jp_universe)
    if args.news:
        items += collect_news_items(us_universe, live=args.live,
                                    limit_per_ticker=args.limit)
    if args.tdnet:
        items += collect_tdnet_items([args.tdnet], live=args.live, tickers=jp_universe)

    # Replace metadata bodies with real filing text (gated; on by default in live).
    if args.live and not args.no_enrich:
        from disclosure_enrich import enrich_items
        items = enrich_items(items, live=True)

    report = ingest_items(items, live_llm=args.live)
    if args.push:
        from disclosure_push import push_new_disclosure_features
        report["push"] = push_new_disclosure_features()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    main()

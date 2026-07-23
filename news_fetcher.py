"""
ALMANAC Phase 0.5 — ニュース開示イベントフェッチャー（disclosure feature pipeline 用）

Yahoo Finance の per-ticker RSS を feedparser で取得し、各見出しを
``disclosure_feature_extractor`` が消費する正規化アイテムに変換する。

- ``normalize_news_entries()`` は pure（fixture でテスト可能・ネットワーク非依存）。
- ``fetch_news_items()`` はネットワークを **gate** する（live=True のときのみ実取得）。
- news は native id が無いため ``source_url``（URL hash）で dedup する。本文は RSS の
  要約のみ（本文取得は別途）。
"""

from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Optional

__all__ = ["normalize_news_entries", "fetch_news_items"]

_YF_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"


def _to_iso(published: str) -> Optional[str]:
    """RFC-822 RSS date → UTC ISO; None on failure."""
    if not published:
        return None
    try:
        dt = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def normalize_news_entries(entries, ticker: str, *, market: str = "US",
                           language: str = "en", limit: int = 10) -> list[dict]:
    """Convert feed entries (dict-like) to disclosure items for ``ticker`` (pure).

    Each entry needs a title, a link (the stable dedup anchor — news has no native
    id), and a parseable published date; entries missing any are skipped.
    """
    items: list[dict] = []
    for e in entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        publish_time = _to_iso(e.get("published") or "")
        if not publish_time:
            continue
        summary = (e.get("summary") or "")[:500]
        items.append({
            "source": "news",
            "ticker": ticker,
            "native_doc_id": None,            # no native id → source_url hash dedup
            "source_url": link,
            "publish_time": publish_time,
            "market": market,
            "language": language,
            "disclosure_type": "other",
            "title": title,
            "body": summary or title,
            "ticker_resolution_method": "ticker_rss",
            "ticker_resolution_confidence": 0.9,
        })
        if len(items) >= limit:
            break
    return items


def fetch_news_items(ticker: str, *, live: bool = False, limit: int = 10) -> list[dict]:
    """Fetch recent per-ticker news as disclosure items. Network is **gated**.

    Returns ``[]`` unless ``live=True`` so importing / dry-running never hits the
    network. Live mode parses the Yahoo Finance per-ticker RSS via feedparser.
    """
    if not live:
        return []
    try:
        import feedparser
        feed = feedparser.parse(_YF_RSS.format(ticker=ticker))
        return normalize_news_entries(feed.entries, ticker, limit=limit)
    except Exception as e:  # noqa: BLE001
        print(f"[news] {ticker} 取得失敗: {type(e).__name__}: {e}")
        return []

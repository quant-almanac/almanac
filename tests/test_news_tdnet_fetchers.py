"""Tests for the news + TDnet disclosure adapters (offline, no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ingest_disclosures import ingest_items  # noqa: E402
from almanac.observability.disclosure_features import read_features  # noqa: E402
from news_fetcher import fetch_news_items, normalize_news_entries  # noqa: E402
from tdnet_fetcher import fetch_tdnet_items, normalize_tdnet_items  # noqa: E402

_NEWS_ENTRIES = [
    {"title": "Apple raises guidance", "link": "https://news/aapl1",
     "summary": "FY outlook above consensus", "published": "Wed, 04 Jun 2026 12:00:00 GMT"},
    {"title": "No link", "link": "", "summary": "x",
     "published": "Wed, 04 Jun 2026 12:00:00 GMT"},          # skipped: no link
    {"title": "Bad date", "link": "https://news/x", "summary": "y",
     "published": "not-a-date"},                              # skipped: unparseable date
]

_TDNET = {"items": [
    {"Tdnet": {"id": "20260604001", "company_code": "72030",
               "title": "2026年3月期 第1四半期決算短信",
               "document_url": "https://release.tdnet.info/doc1",
               "pubdate": "2026-06-04 15:00:00"}},
    {"Tdnet": {"id": "20260604002", "company_code": None, "title": "ファンド",
               "pubdate": "2026-06-04 15:00:00"}},            # skipped: no company_code
]}

_OK = json.dumps({"directional_score": 0.5, "directional_confidence": 0.7,
                  "catalyst_specificity": 0.6, "contradiction_count": 0,
                  "summary": "x", "evidence": []})


def _transport(content: str):
    def _tx(**kwargs):
        return content, {"input_tokens": 10, "output_tokens": 5}
    return _tx


# ---------------------------------------------------------------------------
# news normalizer + gate
# ---------------------------------------------------------------------------


def test_news_normalizer_shapes_and_skips() -> None:
    items = normalize_news_entries(_NEWS_ENTRIES, "AAPL")
    assert len(items) == 1                       # no-link + bad-date skipped
    it = items[0]
    assert it["source"] == "news" and it["ticker"] == "AAPL"
    assert it["native_doc_id"] is None and it["source_url"] == "https://news/aapl1"
    assert it["publish_time"].startswith("2026-06-04T")
    assert "consensus" in it["body"]


def test_news_fetch_gated() -> None:
    assert fetch_news_items("AAPL", live=False) == []


# ---------------------------------------------------------------------------
# tdnet normalizer + gate
# ---------------------------------------------------------------------------


def test_tdnet_normalizer_maps_and_skips() -> None:
    items = normalize_tdnet_items(_TDNET)
    assert len(items) == 1                        # fund (no code) skipped
    it = items[0]
    assert it["ticker"] == "7203.T" and it["source"] == "tdnet"
    assert it["native_doc_id"] == "20260604001"
    assert it["disclosure_type"] == "earnings"    # 決算短信
    assert it["publish_time"] == "2026-06-04T15:00:00+09:00"
    assert it["market"] == "JP" and it["language"] == "ja"


def test_tdnet_fetch_gated() -> None:
    assert fetch_tdnet_items("today", live=False) == []


# ---------------------------------------------------------------------------
# orchestrator integration (offline transport)
# ---------------------------------------------------------------------------


def test_news_and_tdnet_flow_through_ingest(tmp_path: Path) -> None:
    items = normalize_news_entries(_NEWS_ENTRIES, "AAPL") + normalize_tdnet_items(_TDNET)
    rep = ingest_items(items, store_path=tmp_path / "f.jsonl",
                       log_path=tmp_path / "c.jsonl",
                       transport=_transport(_OK), fsync=False)
    assert rep["extracted"] == 2 and rep["failed"] == 0
    stored = read_features(tmp_path / "f.jsonl")
    assert {r["source"] for r in stored} == {"news", "tdnet"}

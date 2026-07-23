"""Tests for disclosure_enrich — filing-text enrichment (offline, no network)."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from disclosure_enrich import enrich_item, enrich_items, html_to_text  # noqa: E402

_HTML = """
<html><head><style>.x{color:red}</style><script>var a=1;</script></head>
<body><h1>Apple&nbsp;raises&nbsp;guidance</h1>
<p>FY operating profit outlook is now <b>above</b> consensus.</p></body></html>
"""

_EDGAR_ITEM = {
    "source": "edgar", "ticker": "AAPL", "native_doc_id": "acc-1",
    "source_url": "https://sec.gov/aapl-10q.htm", "body": "10-Q: Quarterly report",
}


def test_html_to_text_strips_tags_scripts_and_unescapes() -> None:
    txt = html_to_text(_HTML)
    assert "Apple raises guidance" in txt          # &nbsp; → space
    assert "above consensus" in txt
    assert "var a=1" not in txt and "color:red" not in txt   # script/style dropped
    assert "<" not in txt and ">" not in txt        # tags stripped
    assert "  " not in txt                           # whitespace collapsed


def test_html_to_text_empty() -> None:
    assert html_to_text("") == ""


def test_enrich_noop_when_not_live() -> None:
    out = enrich_item(_EDGAR_ITEM, live=False, fetch=lambda u: _HTML)
    assert out["body"] == "10-Q: Quarterly report"   # unchanged, no fetch


def test_enrich_skips_non_edgar() -> None:
    item = {**_EDGAR_ITEM, "source": "edinet"}
    out = enrich_item(item, live=True, fetch=lambda u: _HTML)
    assert out["body"] == "10-Q: Quarterly report"   # only EDGAR enriched


def test_enrich_replaces_body_with_filing_text() -> None:
    out = enrich_item(_EDGAR_ITEM, live=True, fetch=lambda u: _HTML)
    assert "above consensus" in out["body"]
    assert out["ticker"] == "AAPL"                   # other fields preserved


def test_enrich_truncates_to_max_chars() -> None:
    out = enrich_item(_EDGAR_ITEM, live=True, max_chars=10, fetch=lambda u: _HTML)
    assert len(out["body"]) <= 10


def test_enrich_skips_non_document_url() -> None:
    item = {**_EDGAR_ITEM, "source_url": "https://sec.gov/cgi-bin/browse-edgar?CIK=320193"}
    out = enrich_item(item, live=True, fetch=lambda u: _HTML)
    assert out["body"] == "10-Q: Quarterly report"   # index page → unchanged


def test_enrich_best_effort_on_fetch_error() -> None:
    def _boom(url):
        raise RuntimeError("network down")
    out = enrich_item(_EDGAR_ITEM, live=True, fetch=_boom)
    assert out["body"] == "10-Q: Quarterly report"   # error → keep metadata body


def test_enrich_items_noop_when_not_live() -> None:
    items = [_EDGAR_ITEM]
    assert enrich_items(items, live=False, fetch=lambda u: _HTML) is items


def test_enrich_prefers_ex99_press_release():
    """index.json から EX-99.1 (プレスリリース) を表紙より優先して取得する。"""
    import json
    index = json.dumps({"directory": {"item": [
        {"name": "aapl-8k.htm", "type": "8-K", "size": "3000"},
        {"name": "ex99-1.htm", "type": "EX-99.1", "size": "20000"},
        {"name": "R1.htm", "type": "", "size": "5000"},
        {"name": "aapl_htm.xml", "type": "XML", "size": "9000"},
    ]}})
    press = "<html><body>Apple reported record revenue and raised guidance.</body></html>"
    cover = "<html><body>Item 2.02 Results of Operations. See Exhibit 99.1.</body></html>"

    def f(u):
        if u.endswith("index.json"):
            return index
        return press if u.endswith("ex99-1.htm") else cover

    item = {"source": "edgar", "ticker": "AAPL", "native_doc_id": "a",
            "source_url": "https://www.sec.gov/Archives/edgar/data/320193/000x/aapl-8k.htm",
            "body": "8-K"}
    out = enrich_item(item, live=True, fetch=f)
    assert "record revenue" in out["body"]
    assert out["enriched_doc_url"].endswith("ex99-1.htm")


def test_enrich_excludes_xbrl_report_files():
    """R\\d+.htm / MetaLinks / xml を除外し、本則フォーム文書を選ぶ。"""
    import json
    index = json.dumps({"directory": {"item": [
        {"name": "R1.htm", "type": "", "size": "9000"},
        {"name": "MetaLinks.json", "type": "", "size": "100"},
        {"name": "aapl-10q.htm", "type": "10-Q", "size": "50000"},
    ]}})

    def f(u):
        if u.endswith("index.json"):
            return index
        return "<html><body>Management discussion: revenue grew.</body></html>"

    item = {"source": "edgar", "source_url": "https://sec.gov/x/aapl-10q.htm", "body": "10-Q"}
    out = enrich_item(item, live=True, fetch=f)
    assert "Management discussion" in out["body"]
    assert out["enriched_doc_url"].endswith("aapl-10q.htm")


def test_enrich_falls_back_to_source_url_without_index():
    """index.json が取れない場合は source_url を直取得 (従来動作)。"""
    def f(u):
        if u.endswith("index.json"):
            raise RuntimeError("404")
        return "<html><body>fallback body text</body></html>"

    item = {"source": "edgar", "source_url": "https://sec.gov/x/doc.htm", "body": "10-Q"}
    out = enrich_item(item, live=True, fetch=f)
    assert "fallback body text" in out["body"]

"""
disclosure_enrich.py — fetch the actual filing text for disclosure items.

Phase 0 ingestion emits items whose ``body`` is only filing *metadata* (form type
+ description). Feeding that to the extractor yields weak features. This module
replaces ``body`` with the real document text so live features carry signal.

Scope:
  - EDGAR: fetch the primary-document HTML at ``source_url`` and convert to text.
    This is the clean, high-volume case.
  - TDnet: fetch the disclosure PDF at ``source_url`` and convert to text.
  - EDINET (攻めバックログ2026-07 項目4 Phase B, docs/design_jp_event_drift_2026_07.md):
    fetch the submission ZIP (書類取得API type=1) and extract the 本文
    (inline-XBRL HTML, e.g. ``0101010_honbun_..._ixbrl.htm``) via ``html_to_text``.
    Needs ``EDINET_API_KEY`` (same as edinet_fetcher.py); without it the item is
    left unchanged (best-effort, matches the other sources' failure mode).

Network is **gated** (``live=False`` → item unchanged) and the fetch is injectable
so ``html_to_text`` and the gating are fully testable offline.
"""

import html as _html
import io
import json as _json
import os
import re
import zipfile
from typing import Any, Callable, Optional

__all__ = ["html_to_text", "pdf_to_text", "extract_edinet_honbun_text", "enrich_item", "enrich_items"]

_HEADERS = {"User-Agent": "ALMANAC research@almanac.local"}
_DEFAULT_MAX_CHARS = 8000

_SCRIPT_STYLE = re.compile(r"(?is)<(script|style)\b.*?>.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"\s+")


def html_to_text(source: str) -> str:
    """Convert filing HTML to readable plain text (pure stdlib).

    Drops ``<script>``/``<style>`` blocks, strips remaining tags, unescapes HTML
    entities, and collapses whitespace. Crude but sufficient to feed an LLM —
    we want readable content, not a faithful DOM.
    """
    if not source:
        return ""
    text = _SCRIPT_STYLE.sub(" ", source)
    text = _TAG.sub(" ", text)
    text = _html.unescape(text)
    return _WS.sub(" ", text).strip()


def pdf_to_text(source: bytes, *, max_pages: int = 3) -> str:
    """Extract the first pages of a TDnet PDF using pdfplumber."""

    if not source:
        return ""
    import pdfplumber

    chunks: list[str] = []
    with pdfplumber.open(io.BytesIO(source)) as pdf:
        for page in pdf.pages[:max(0, max_pages)]:
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text.strip())
    lines = []
    for line in "\n".join(chunks).splitlines():
        normalized = _WS.sub(" ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _default_fetch(url: str) -> Any:
    import requests

    r = requests.get(url, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    return r.content if url.lower().endswith(".pdf") else r.text


_EDINET_DOC_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"


def _default_edinet_fetch(doc_id: str, *, api_key: str) -> bytes:
    import requests

    r = requests.get(
        _EDINET_DOC_URL.format(doc_id=doc_id),
        params={"type": 1, "Subscription-Key": api_key},
        timeout=30,
    )
    r.raise_for_status()
    return r.content


def extract_edinet_honbun_text(zip_bytes: bytes) -> str:
    """Extract 本文 (main text) from an EDINET submission ZIP as plain text.

    The submission is inline-XBRL HTML, not a PDF: ``XBRL/PublicDoc/`` holds a
    ``..._header_..._ixbrl.htm`` (cover) and a ``..._honbun_..._ixbrl.htm`` (the
    actual filing body, e.g. purpose-of-holding for a 大量保有報告書). Prefers
    the "honbun" file; falls back to the largest non-header ``.htm`` so a
    filename-convention drift degrades gracefully instead of returning nothing.
    """
    if not zip_bytes:
        return ""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        htm_names = [
            n for n in zf.namelist()
            if n.lower().endswith(".htm") and "publicdoc" in n.lower()
        ]
        honbun = [n for n in htm_names if "honbun" in n.lower()]
        if honbun:
            name = honbun[0]
        else:
            candidates = [n for n in htm_names if "header" not in n.lower()]
            if not candidates:
                return ""
            name = max(candidates, key=lambda n: zf.getinfo(n).file_size)
        raw = zf.read(name).decode("utf-8", errors="replace")
    return html_to_text(raw)


# EDGAR の主文書は 8-K=表紙 / 10-Q=inline-XBRL で signal が薄い。filing ディレクトリの
# index.json から「読める文書」を選ぶ: 決算プレスリリース等の EX-99* を最優先し、無ければ
# 本則フォーム文書。XBRL ビューア (R\d+.htm) / .xml / .xsd / metalink は除外する。
_PRIMARY_FORM_TYPES = {
    "8-K", "10-Q", "10-K", "6-K", "20-F", "40-F", "S-1", "424B5", "DEF 14A", "8-K/A",
}
_R_REPORT = re.compile(r"(?i)^r\d+\.htm")


def _is_readable_doc(name: str) -> bool:
    n = (name or "").lower()
    if not n.endswith((".htm", ".html", ".txt")):
        return False
    if _R_REPORT.match(n) or "metalink" in n:
        return False
    return True


def _pick_readable_doc(docs: list) -> Optional[str]:
    """index.json の directory.item[] から最も signal の高い読める文書名を返す。"""
    cands = [d for d in docs if isinstance(d, dict) and _is_readable_doc(str(d.get("name", "")))]
    if not cands:
        return None

    def _score(d: dict) -> tuple:
        t = str(d.get("type", "")).upper()
        try:
            sz = int(d.get("size") or 0)
        except (TypeError, ValueError):
            sz = 0
        if t.startswith("EX-99"):     # 決算/IR プレスリリース = 最も読みやすく signal 高
            tier = 3
        elif t in _PRIMARY_FORM_TYPES:
            tier = 2
        elif t.startswith("EX-"):
            tier = 1
        else:
            tier = 0
        return (tier, sz)

    return str(max(cands, key=_score).get("name"))


def _select_readable_doc_url(source_url: str, *, fetch: Callable[[str], Any]) -> Optional[str]:
    """filing ディレクトリの index.json を読み、読める文書の絶対 URL を返す (失敗時 None)。"""
    if "/" not in source_url:
        return None
    base = source_url.rsplit("/", 1)[0] + "/"
    try:
        idx = _json.loads(fetch(base + "index.json"))
    except Exception:
        return None
    docs = (((idx or {}).get("directory") or {}).get("item")) or []
    if not isinstance(docs, list):
        return None
    name = _pick_readable_doc(docs)
    return (base + name) if name else None


def enrich_item(
    item: dict,
    *,
    live: bool = False,
    max_chars: int = _DEFAULT_MAX_CHARS,
    fetch: Optional[Callable[[str], Any]] = None,
    edinet_fetch: Optional[Callable[[str], bytes]] = None,
    edinet_api_key: Optional[str] = None,
) -> dict:
    """Return ``item`` with ``body`` replaced by real filing text when possible.

    Gated: returns the item unchanged unless ``live=True``. EDGAR HTML, TDnet
    PDF, and EDINET submission ZIP (本文 inline-XBRL HTML) are supported.
    ``fetch``/``edinet_fetch`` are injectable for tests. Any fetch or parse
    error leaves the item unchanged (best-effort).
    """
    if not live:
        return item
    source = item.get("source")
    if source not in {"edgar", "tdnet", "edinet"}:
        return item
    fetcher = fetch or _default_fetch

    if source == "edinet":
        doc_id = item.get("native_doc_id")
        if not doc_id:
            return item
        key = edinet_api_key or os.environ.get("EDINET_API_KEY", "")
        if not key:
            return item
        edinet_fetcher = edinet_fetch or (lambda d: _default_edinet_fetch(d, api_key=key))
        try:
            zip_bytes = edinet_fetcher(doc_id)
        except Exception as e:  # noqa: BLE001 — best-effort; keep metadata body
            print(f"[enrich] EDINET {doc_id} 取得失敗: {type(e).__name__}: {e}")
            return item
        try:
            text = extract_edinet_honbun_text(zip_bytes)
        except Exception as e:  # noqa: BLE001
            print(f"[enrich] EDINET {doc_id} 解析失敗: {type(e).__name__}: {e}")
            return item
        text = text[:max_chars]
        if not text:
            return item
        return {**item, "body": text, "enriched_doc_url": f"edinet:{doc_id}"}

    url = item.get("source_url") or ""
    if not url:
        return item

    if source == "tdnet":
        doc_url = url
    else:
        doc_url = _select_readable_doc_url(url, fetch=fetcher)
        if doc_url is None:
            # fallback: use source_url directly only if it is itself a document
            if not url.lower().endswith((".htm", ".html", ".txt")):
                return item
            doc_url = url

    try:
        raw = fetcher(doc_url)
    except Exception as e:  # noqa: BLE001 — best-effort; keep metadata body
        print(f"[enrich] {doc_url} 取得失敗: {type(e).__name__}: {e}")
        return item
    try:
        if source == "tdnet":
            if isinstance(raw, str):
                raw = raw.encode("latin-1")
            text = pdf_to_text(raw)
        else:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            text = html_to_text(raw)
    except Exception as e:  # noqa: BLE001
        print(f"[enrich] {doc_url} 解析失敗: {type(e).__name__}: {e}")
        return item
    text = text[:max_chars]
    if not text:
        return item
    return {**item, "body": text, "enriched_doc_url": doc_url}


def enrich_items(
    items: list[dict],
    *,
    live: bool = False,
    max_chars: int = _DEFAULT_MAX_CHARS,
    fetch: Optional[Callable[[str], Any]] = None,
    edinet_fetch: Optional[Callable[[str], bytes]] = None,
    edinet_api_key: Optional[str] = None,
) -> list[dict]:
    """Enrich each item's body (see :func:`enrich_item`). No-op when ``live=False``."""
    if not live:
        return items
    return [
        enrich_item(
            it, live=live, max_chars=max_chars, fetch=fetch,
            edinet_fetch=edinet_fetch, edinet_api_key=edinet_api_key,
        )
        for it in items
    ]

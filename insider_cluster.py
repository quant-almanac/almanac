"""Deterministic SEC Form 4 open-market purchase cluster detector."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Callable, Optional

MIN_CLUSTER_BUYERS = 3
LOOKBACK_DAYS = 90


def _text(root: ET.Element, suffix: str) -> str:
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == suffix and node.text:
            return node.text.strip()
    return ""


def _value(root: ET.Element, suffix: str) -> str:
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != suffix:
            continue
        value = _text(node, "value")
        if value:
            return value
        if node.text and node.text.strip():
            return node.text.strip()
    return ""


def parse_form4_xml(xml: str | bytes, *, accession: str = "") -> list[dict[str, Any]]:
    """Return code-P acquisitions from one Form 4 XML document."""

    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError, ValueError):
        return []
    owner = _text(root, "rptOwnerName")
    out: list[dict[str, Any]] = []
    for tx in root.iter():
        if tx.tag.rsplit("}", 1)[-1] != "nonDerivativeTransaction":
            continue
        code = _text(tx, "transactionCode")
        acquired = _value(tx, "transactionAcquiredDisposedCode")
        if code != "P" or acquired != "A":
            continue
        tx_date = _value(tx, "transactionDate")
        shares = _value(tx, "transactionShares")
        price = _value(tx, "transactionPricePerShare")
        try:
            out.append({
                "owner": owner,
                "transaction_date": tx_date,
                "shares": float(shares),
                "price": float(price) if price else None,
                "accession": accession,
            })
        except ValueError:
            continue
    return out


def detect_insider_cluster(
    documents: list[dict[str, Any]],
    ticker: str,
    *,
    as_of: date | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    min_buyers: int = MIN_CLUSTER_BUYERS,
) -> Optional[dict[str, Any]]:
    """Emit one deterministic observe-only item when distinct buyers >= threshold."""

    as_of = as_of or date.today()
    cutoff = as_of - timedelta(days=lookback_days)
    purchases: list[dict[str, Any]] = []
    for doc in documents:
        for purchase in parse_form4_xml(doc.get("xml", b""), accession=str(doc.get("accession", ""))):
            try:
                tx_date = date.fromisoformat(str(purchase["transaction_date"])[:10])
            except ValueError:
                continue
            if cutoff <= tx_date <= as_of:
                purchases.append(purchase)
    owners = {p["owner"].strip().lower() for p in purchases if p.get("owner")}
    if len(owners) < min_buyers:
        return None
    accessions = sorted({p["accession"] for p in purchases if p.get("accession")})
    anchor = hashlib.sha256("|".join(accessions).encode("utf-8")).hexdigest()[:16]
    publish_date = max(str(p["transaction_date"])[:10] for p in purchases)
    return {
        "source": "edgar",
        "ticker": ticker.upper(),
        "native_doc_id": f"form4-cluster:{ticker.upper()}:{anchor}",
        "source_url": f"https://www.sec.gov/edgar/browse/?CIK={ticker.upper()}",
        "publish_time": publish_date,
        "market": "US",
        "language": "en",
        "disclosure_type": "insider",
        "title": f"{ticker.upper()} insider open-market purchase cluster ({len(owners)} buyers)",
        "body": f"{len(owners)} distinct insiders reported code-P purchases within {lookback_days} days.",
        "ticker_resolution_method": "form4_issuer",
        "ticker_resolution_confidence": 1.0,
        "insider_cluster_score": float(len(owners)),
        "deterministic_only": True,
    }


def fetch_insider_cluster(
    ticker: str,
    *,
    live: bool = False,
    submissions: Optional[dict] = None,
    fetch: Optional[Callable[[str], str | bytes]] = None,
) -> list[dict[str, Any]]:
    """Fetch recent Form 4 XML documents and return zero or one cluster item."""

    if not live:
        return []
    from edgar_fetcher import _HEADERS, get_cik

    import requests

    cik = get_cik(ticker)
    if not cik:
        return []
    fetcher = fetch
    if submissions is None:
        try:
            response = requests.get(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                headers=_HEADERS,
                timeout=30,
            )
            response.raise_for_status()
            submissions = response.json()
        except Exception:
            return []
    recent = (submissions or {}).get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    primary_docs = recent.get("primaryDocument") or []
    dates = recent.get("filingDate") or []
    documents: list[dict[str, Any]] = []
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    for index, form in enumerate(forms):
        if form != "4":
            continue
        try:
            filing_date = date.fromisoformat(str(dates[index])[:10])
        except (IndexError, ValueError):
            continue
        if filing_date < cutoff:
            continue
        accession = str(accessions[index])
        primary = str(primary_docs[index])
        acc_nodash = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{primary}"
        try:
            if fetcher:
                xml = fetcher(url)
            else:
                response = requests.get(url, headers=_HEADERS, timeout=30)
                response.raise_for_status()
                xml = response.content
        except Exception:
            continue
        documents.append({"xml": xml, "accession": accession})
    item = detect_insider_cluster(documents, ticker)
    return [item] if item else []

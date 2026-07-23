"""Tests for the disclosure ingestion pipeline (offline, no network, no spend).

Covers the pure normalizers (EDGAR/EDINET against fixtures), the network gates,
and the orchestrator's spend gate + dedup-skip.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from edgar_fetcher import fetch_edgar_filings, normalize_edgar_submissions  # noqa: E402
from edinet_fetcher import fetch_edinet_documents, normalize_edinet_documents  # noqa: E402
from ingest_disclosures import ingest_items  # noqa: E402
from almanac.observability.disclosure_features import read_features  # noqa: E402

_EDGAR = {
    "cik": "320193",
    "filings": {"recent": {
        "accessionNumber": ["0000320193-26-000010", "0000320193-26-000011",
                            "0000320193-26-000099"],
        "form": ["10-Q", "8-K", "4"],
        "filingDate": ["2026-05-01", "2026-05-15", "2026-05-20"],
        "primaryDocument": ["aapl-10q.htm", "aapl-8k.htm", "form4.xml"],
        "primaryDocDescription": ["Quarterly report", "Current report", "Insider"],
    }},
}

_EDINET = {"results": [
    {"docID": "S100ABCD", "secCode": "72030", "filerName": "トヨタ自動車",
     "docDescription": "四半期報告書", "docTypeCode": "140",
     "submitDateTime": "2026-06-01 09:00"},
    {"docID": "S100ZZZZ", "secCode": None, "filerName": "某ファンド",
     "docDescription": "大量保有報告書", "docTypeCode": "350",
     "submitDateTime": "2026-06-01 10:00"},   # no secCode → non-listed → skipped
]}

_OK = json.dumps({
    "directional_score": 0.5, "directional_confidence": 0.7,
    "catalyst_specificity": 0.6, "contradiction_count": 0,
    "summary": "x", "evidence": [],
})


def _transport(content: str):
    def _tx(**kwargs):
        return content, {"input_tokens": 10, "output_tokens": 5}
    return _tx


# ---------------------------------------------------------------------------
# Pure normalizers
# ---------------------------------------------------------------------------


def test_edgar_normalizer_filters_forms_and_maps_types() -> None:
    items = normalize_edgar_submissions(_EDGAR, "AAPL")
    # Form "4" is not in the default form set → excluded; 10-Q + 8-K kept.
    assert len(items) == 2
    by_id = {it["native_doc_id"]: it for it in items}
    q = by_id["0000320193-26-000010"]
    assert q["disclosure_type"] == "earnings" and q["source"] == "edgar"
    assert q["market"] == "US" and q["ticker"] == "AAPL"
    assert "0000320193260000" in q["source_url"].replace("-", "")  # accession in URL
    assert by_id["0000320193-26-000011"]["disclosure_type"] == "other"  # 8-K


def test_edinet_normalizer_skips_non_listed_and_maps_ticker() -> None:
    items = normalize_edinet_documents(_EDINET)
    assert len(items) == 1                      # fund (no secCode) dropped
    it = items[0]
    assert it["ticker"] == "7203.T"             # secCode 72030 → 7203.T
    assert it["disclosure_type"] == "earnings"  # docTypeCode 140
    assert it["publish_time"] == "2026-06-01T09:00:00+09:00"
    assert it["market"] == "JP" and it["language"] == "ja"


# ---------------------------------------------------------------------------
# Network gates
# ---------------------------------------------------------------------------


def test_fetchers_return_empty_when_not_live() -> None:
    assert fetch_edgar_filings("AAPL", live=False) == []
    assert fetch_edinet_documents("2026-06-01", live=False) == []


def test_edinet_surfaces_embedded_auth_error(monkeypatch, capsys) -> None:
    """EDINET v2 wraps auth errors in an HTTP-200 body; must not look like 0 docs.

    {"StatusCode": 401, ...} has no "results" key, so the old code returned []
    silently — an invalid/expired key would freeze the JP large-holding lane
    while reporting "no disclosures". The fetcher must now log the real status.
    """
    import edinet_fetcher

    class _Resp:
        def raise_for_status(self) -> None:  # HTTP 200 — passes
            return None

        def json(self) -> dict:
            return {"StatusCode": 401, "message": "Access denied due to invalid subscription key."}

    monkeypatch.setattr(edinet_fetcher.requests, "get", lambda *a, **k: _Resp())
    out = fetch_edinet_documents("2026-06-12", live=True, api_key="dummy")
    assert out == []
    captured = capsys.readouterr().out
    assert "401" in captured and "取得失敗" in captured


# ---------------------------------------------------------------------------
# Orchestrator: spend gate + dedup skip (offline)
# ---------------------------------------------------------------------------


def test_ingest_extracts_with_injected_transport(tmp_path: Path) -> None:
    items = normalize_edinet_documents(_EDINET)
    rep = ingest_items(items, store_path=tmp_path / "f.jsonl",
                       log_path=tmp_path / "c.jsonl",
                       transport=_transport(_OK), fsync=False)
    assert rep["seen"] == 1 and rep["extracted"] == 1 and rep["failed"] == 0
    assert len(read_features(tmp_path / "f.jsonl")) == 1


def test_ingest_dry_run_makes_no_calls_and_stores_nothing(tmp_path: Path) -> None:
    items = normalize_edinet_documents(_EDINET)
    rep = ingest_items(items, store_path=tmp_path / "f.jsonl")  # no transport, no live
    assert rep["skipped_no_llm"] == 1 and rep["extracted"] == 0
    assert read_features(tmp_path / "f.jsonl") == []


def test_ingest_skips_already_extracted(tmp_path: Path) -> None:
    items = normalize_edinet_documents(_EDINET)
    store, log = tmp_path / "f.jsonl", tmp_path / "c.jsonl"
    ingest_items(items, store_path=store, log_path=log,
                 transport=_transport(_OK), fsync=False)
    rep2 = ingest_items(items, store_path=store, log_path=log,
                        transport=_transport(_OK), fsync=False)
    assert rep2["skipped_existing"] == 1 and rep2["extracted"] == 0
    assert len(read_features(store)) == 1  # no duplicate row, no re-spend


def test_ingest_custom_prompt_version_dedups_no_recharge(tmp_path: Path) -> None:
    """With a custom prompt_version, the stored value matches the pre-check so a
    re-run skips instead of re-charging the LLM (R2 P2 fix)."""
    items = normalize_edinet_documents(_EDINET)
    store, log = tmp_path / "f.jsonl", tmp_path / "c.jsonl"
    r1 = ingest_items(items, store_path=store, log_path=log,
                      transport=_transport(_OK), fsync=False, prompt_version="pv9")
    r2 = ingest_items(items, store_path=store, log_path=log,
                      transport=_transport(_OK), fsync=False, prompt_version="pv9")
    assert r1["extracted"] == 1
    assert r2["skipped_existing"] == 1 and r2["extracted"] == 0


def test_ingest_uses_reservation_lock(tmp_path: Path) -> None:
    """The check+extract critical section is guarded by a sidecar reservation
    lock (R-round P2: prevents concurrent double-charge)."""
    items = normalize_edinet_documents(_EDINET)
    store = tmp_path / "f.jsonl"
    ingest_items(items, store_path=store, log_path=tmp_path / "c.jsonl",
                 transport=_transport(_OK), fsync=False)
    assert (tmp_path / "f.jsonl.ingest.lock").exists()


def test_load_scan_universe_flattens_categorized_dict(tmp_path):
    """カテゴリ別 dict ({cat: [tickers]}) はカテゴリ名でなく全ティッカーを flatten + dedup する。"""
    import json
    import ingest_disclosures as ing
    p = tmp_path / "tickers.json"
    p.write_text(json.dumps({
        "sp500_major": ["MMM", "ABT", "AAPL"],
        "etf_list": ["QQQ", "SPY"],
        "dup_cat": ["AAPL"],  # 重複は1つに
    }), encoding="utf-8")
    out = ing.load_scan_universe(p)
    assert out == ["MMM", "ABT", "AAPL", "QQQ", "SPY"]
    assert "sp500_major" not in out and "etf_list" not in out


# ---------------------------------------------------------------------------
# Scan-universe resolution (spend guard: curated default, full opt-in)
# ---------------------------------------------------------------------------


def test_resolve_universe_default_is_curated_pre_registered_slice():
    """Bare --us resolves to the pinned curated slice (~30 sector-diverse names),
    NOT the full tickers.json — the spend guard against accidental fan-out."""
    import ingest_disclosures as ing
    u = ing.resolve_scan_universe()
    assert ing.DISCLOSURE_UNIVERSE_PATH.exists()
    assert 20 <= len(u) <= 40                      # small, curated
    assert "AAPL" in u and "JPM" in u and "XOM" in u   # multi-sector
    if not (Path(__file__).resolve().parents[1] / "tickers.json").exists():
        pytest.skip("private full-universe ticker state is intentionally excluded")
    full = ing.resolve_scan_universe(full=True)
    assert len(full) > len(u) * 5                  # full universe is much larger
    assert len(u) < len(full)


def test_resolve_universe_explicit_path_overrides(tmp_path):
    """--universe PATH takes precedence over both curated default and --full-universe."""
    import json
    import ingest_disclosures as ing
    p = tmp_path / "custom.json"
    p.write_text(json.dumps({"tickers": ["XYZ", "ABC"]}), encoding="utf-8")
    assert ing.resolve_scan_universe(universe_path=p) == ["XYZ", "ABC"]
    # explicit path wins even if full is also requested
    assert ing.resolve_scan_universe(universe_path=p, full=True) == ["XYZ", "ABC"]


def test_curated_universe_is_not_derived_from_holdings(tmp_path):
    """The pinned slice carries the pre-registration metadata (book-independent
    selection rule) so the file documents *why* it's safe to scan publicly."""
    import json
    import ingest_disclosures as ing
    meta = json.loads(ing.DISCLOSURE_UNIVERSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(meta.get("tickers"), list) and meta["tickers"]
    assert meta.get("_criterion")                  # selection rule documented
    assert meta.get("_pinned_at")                  # pre-registration timestamp
    # flat list and by_sector agree (no silent drift)
    flat = sorted(meta["tickers"])
    sectored = sorted(t for v in meta["by_sector"].values() for t in v)
    assert flat == sectored

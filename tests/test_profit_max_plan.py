"""Offline regression tests for docs/plan_profit_max_2026.md."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from brief_disclosures import format_brief_section, yesterday_disclosure_signals
from disclosure_push import push_new_disclosure_features, qualifies_for_push
from disclosure_shadow_book import estimate_round_trip_cost_pct, signal_from_feature, simulate_shadow_book
from edgar_fetcher import normalize_edgar_submissions
from edinet_fetcher import normalize_edinet_documents
from ingest_disclosures import ingest_items, resolve_scan_universe
from insider_cluster import detect_insider_cluster, parse_form4_xml
from jp_buyback_parser import buyback_directional_score, parse_buyback_ratio_pct
from jp_guidance_parser import parse_guidance_revision_pct
from jp_monthly_sales_parser import parse_monthly_yoy_pct
from almanac.observability.catalyst_layer import synthesize_from_disclosure_features
from nisa_allocator import score_nisa_placement
from tax_harvest_scanner import REPURCHASE_WARNING, scan_tax_harvest
from tdnet_fetcher import normalize_tdnet_items


def _minimal_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def test_jp_universe_is_pinned_and_exactly_50() -> None:
    universe = resolve_scan_universe(market="JP")
    assert len(universe) == 50
    assert len(set(universe)) == 50
    meta = json.loads(open("disclosure_universe_jp.json", encoding="utf-8").read())
    assert meta["_pinned_at"] == "2026-06-11"
    assert sorted(universe) == sorted(t for values in meta["by_sector"].values() for t in values)


def test_tdnet_pdf_enrichment_extracts_bytes_and_is_best_effort() -> None:
    from disclosure_enrich import enrich_item

    item = {
        "source": "tdnet",
        "source_url": "https://example.test/revision.pdf",
        "body": "metadata",
    }
    out = enrich_item(item, live=True, fetch=lambda _: _minimal_pdf("Operating profit revised"))
    assert "Operating profit revised" in out["body"]
    failed = enrich_item(item, live=True, fetch=lambda _: b"not-a-pdf")
    assert failed["body"] == "metadata"
    assert enrich_item(item, live=False, fetch=lambda _: b"") is item


def _minimal_edinet_zip(honbun_text: str) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "XBRL/PublicDoc/0000000_header_x_ixbrl.htm",
            "<html><body>表紙</body></html>",
        )
        zf.writestr(
            "XBRL/PublicDoc/0101010_honbun_x_ixbrl.htm",
            f"<html><body>{honbun_text}</body></html>",
        )
    return buf.getvalue()


def test_edinet_zip_enrichment_extracts_honbun_and_is_best_effort() -> None:
    from disclosure_enrich import enrich_item

    item = {
        "source": "edinet", "native_doc_id": "S100TEST", "body": "metadata",
    }
    out = enrich_item(
        item, live=True, edinet_api_key="dummy",
        edinet_fetch=lambda _doc_id: _minimal_edinet_zip("経営に参加するため保有"),
    )
    assert "経営に参加するため保有" in out["body"]
    assert out["enriched_doc_url"] == "edinet:S100TEST"

    failed = enrich_item(
        item, live=True, edinet_api_key="dummy",
        edinet_fetch=lambda _doc_id: b"not-a-zip",
    )
    assert failed["body"] == "metadata"

    assert enrich_item(item, live=False, edinet_api_key="dummy") is item
    no_key = enrich_item({**item}, live=True, edinet_api_key="")
    assert no_key["body"] == "metadata"
    no_doc_id = enrich_item({"source": "edinet", "body": "metadata"}, live=True, edinet_api_key="dummy")
    assert no_doc_id["body"] == "metadata"


def test_edinet_honbun_extraction_prefers_honbun_over_header() -> None:
    from disclosure_enrich import extract_edinet_honbun_text

    zip_bytes = _minimal_edinet_zip("対象の本文テキスト")
    text = extract_edinet_honbun_text(zip_bytes)
    assert "対象の本文テキスト" in text
    assert "表紙" not in text


def test_guidance_revision_parser_matches_hand_calculation() -> None:
    text = """
    業績予想の修正
    売上高 営業利益 経常利益
    前回発表予想(A) 10,000 1,000 900
    今回修正予想(B) 11,000 1,300 1,100
    """
    assert parse_guidance_revision_pct(text) == 0.3
    explicit = "営業利益 前回予想: 2,000 今回予想: 1,500"
    assert parse_guidance_revision_pct(explicit) == -0.25
    assert parse_guidance_revision_pct("配当予想のみ") is None


def test_monthly_sales_parser_classifies_index_and_signed_change() -> None:
    assert round(parse_monthly_yoy_pct("既存店売上高 前年同月比 112.3%"), 3) == 0.123
    assert parse_monthly_yoy_pct("前年比 8.5%減") == -0.085
    assert parse_monthly_yoy_pct("月次概況（数値なし）") is None
    payload = {"items": [{"Tdnet": {
        "id": "m1",
        "company_code": "12340",
        "pubdate": "2026-06-10 15:00:00",
        "title": "2026年5月度 月次売上高のお知らせ",
    }}]}
    assert normalize_tdnet_items(payload)[0]["disclosure_type"] == "monthly_sales"


def test_edinet_stake_uses_target_not_filer() -> None:
    payload = {"results": [{
        "docID": "S100STAKE",
        "secCode": None,
        "filerName": "Oasis Management",
        "docDescription": "大量保有報告書 対象会社 証券コード 4321",
        "docTypeCode": "350",
        "submitDateTime": "2026-06-10 15:00",
    }]}
    item = normalize_edinet_documents(payload, activist_names=["oasis management"])[0]
    assert item["ticker"] == "4321.T"
    assert item["ticker_resolution_method"] == "target_resolution"
    assert item["activist_flag"] is True
    assert item["disclosure_type"] == "stake"


def test_edgar_13d_and_13g_are_in_default_lane() -> None:
    payload = {"cik": "1", "filings": {"recent": {
        "accessionNumber": ["a", "b"],
        "form": ["SC 13D", "SC 13G/A"],
        "filingDate": ["2026-06-01", "2026-06-02"],
        "primaryDocument": ["a.htm", "b.htm"],
        "primaryDocDescription": ["13D", "13G amendment"],
    }}}
    items = normalize_edgar_submissions(payload, "TEST")
    assert [item["disclosure_type"] for item in items] == ["stake", "stake"]


_FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner><reportingOwnerId><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId></reportingOwner>
  <nonDerivativeTable><nonDerivativeTransaction>
    <transactionDate><value>2026-06-01</value></transactionDate>
    <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
    <transactionAmounts>
      <transactionShares><value>100</value></transactionShares>
      <transactionPricePerShare><value>25</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
    </transactionAmounts>
  </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>"""


def test_form4_cluster_requires_three_distinct_open_market_buyers() -> None:
    docs = [
        {"xml": _FORM4.format(owner=owner), "accession": f"a-{index}"}
        for index, owner in enumerate(("A", "B", "C"), 1)
    ]
    assert parse_form4_xml(docs[0]["xml"])[0]["shares"] == 100
    item = detect_insider_cluster(docs, "TEST", as_of=datetime(2026, 6, 11).date())
    assert item and item["insider_cluster_score"] == 3
    assert item["deterministic_only"] is True
    assert detect_insider_cluster(docs[:2], "TEST", as_of=datetime(2026, 6, 11).date()) is None


def test_deterministic_ingest_writes_without_llm(tmp_path) -> None:
    item = {
        "source": "tdnet",
        "ticker": "1234.T",
        "native_doc_id": "g-1",
        "source_url": "https://example/g.pdf",
        "publish_time": "2026-06-01T15:00:00+09:00",
        "market": "JP",
        "language": "ja",
        "disclosure_type": "guidance",
        "title": "業績予想の修正",
        "body": "売上高 営業利益\n前回発表予想(A) 1000 100\n今回修正予想(B) 1100 140",
    }
    store = tmp_path / "features.jsonl"
    report = ingest_items([item], store_path=store, fsync=False)
    rows = [json.loads(line) for line in store.read_text().splitlines()]
    assert report["deterministic_written"] == 1
    assert report["skipped_no_llm"] == 1
    assert rows[0]["observe_only"] is True
    assert rows[0]["guidance_revision_pct"] == 0.4
    hypotheses = synthesize_from_disclosure_features(
        rows, analysis_id="a", analysis_date="2026-06-01"
    )
    assert hypotheses and hypotheses[0].observe_only is True
    assert hypotheses[0].action_type == "buy"
    assert hypotheses[0].event_at == rows[0]["compute_time"]


def test_disclosure_push_threshold_dedup_and_label(tmp_path) -> None:
    now = datetime(2026, 6, 12, tzinfo=timezone.utc)
    row = {
        "feature_id": "f1",
        "ticker": "1234.T",
        "source": "tdnet",
        "disclosure_type": "guidance",
        "publish_time": "2026-06-11T15:00:00+09:00",  # recent → passes freshness gate
        "guidance_revision_pct": 0.2,
        "summary": "上方修正",
        "observe_only": True,
    }
    sent = []
    assert qualifies_for_push(row)
    first = push_new_disclosure_features(rows=[row], state_path=tmp_path / "state.json", send=sent.append, now=now)
    second = push_new_disclosure_features(rows=[row], state_path=tmp_path / "state.json", send=sent.append, now=now)
    assert first["sent_count"] == 1 and second["sent_count"] == 0
    assert "未検証・観測のみ" in sent[0] and "売買推奨ではありません" in sent[0]


def test_push_freshness_suppresses_stale_flood_but_seeds_state(tmp_path) -> None:
    """空 state での初回 push が、qualifies でも publish_time の古い行を送らない。

    backfill 済みストアに対する初回 cron で 6ヶ月前の開示が一斉送信される事故
    (dedup 状態が空 = 全履歴が「新規」) を鮮度ガードで防ぐ。古い行は再評価しない
    よう seen に記録するが send はしない。
    """
    now = datetime(2026, 6, 12, tzinfo=timezone.utc)
    stale = {
        "feature_id": "old1", "ticker": "AAPL", "source": "edgar",
        "disclosure_type": "earnings", "publish_time": "2025-12-19T12:00:00+00:00",
        "directional_score": 0.9, "directional_confidence": 0.9, "summary": "old",
    }
    recent = {
        "feature_id": "new1", "ticker": "1377.T", "source": "tdnet",
        "disclosure_type": "guidance", "publish_time": "2026-06-11T15:00:00+09:00",
        "guidance_revision_pct": 0.25, "summary": "上方修正",
    }
    sent = []
    res = push_new_disclosure_features(
        rows=[stale, recent], state_path=tmp_path / "s.json", send=sent.append, now=now
    )
    assert res["sent_count"] == 1 and res["skipped_stale"] == 1
    assert any("1377.T" in m for m in sent) and not any("AAPL" in m for m in sent)
    # Re-run: stale was seeded as seen, recent already sent → nothing fires again.
    sent.clear()
    res2 = push_new_disclosure_features(
        rows=[stale, recent], state_path=tmp_path / "s.json", send=sent.append, now=now
    )
    assert res2["sent_count"] == 0 and res2["skipped_stale"] == 0 and sent == []


def test_ingest_deterministic_bad_item_does_not_abort_batch(tmp_path) -> None:
    """1件の不正アイテム (ticker 空の activist stake) でバッチ全体を落とさない。

    決定論レーンの契約は「値か None、決して crash しない」。make_feature は ticker
    空で ValueError を投げるため、ingest_items 側で封じ込めて後続を処理し続ける。
    """
    bad = {
        "source": "edinet", "ticker": "", "publish_time": "2026-06-11T06:00:00+00:00",
        "activist_flag": True, "native_doc_id": "X1", "title": "stake",
    }
    good = {
        "source": "tdnet", "ticker": "1377.T", "publish_time": "2026-06-11T06:00:00+00:00",
        "disclosure_type": "guidance", "title": "業績予想の修正",
        "body": "営業利益 前回予想 100 今回予想 150", "native_doc_id": "X2",
    }
    report = ingest_items([bad, good], store_path=tmp_path / "feat.jsonl", live_llm=False)
    assert report["seen"] == 2
    assert report["failed"] == 1
    assert report["deterministic_written"] == 1  # the good item still landed
    assert any("ticker must be non-empty" in str(e) for e in report["errors"])


def test_shadow_book_costs_and_pnl_are_deterministic() -> None:
    dates = pd.date_range("2026-06-02", periods=10, freq="B")
    prices = pd.DataFrame({"Open": [100] * 10, "Close": [100, 101, 102, 103, 104, 110, 111, 112, 113, 114]}, index=dates)
    feature = {
        "feature_id": "f1",
        "source_event_id": "tdnet:x",
        "ticker": "1234.T",
        "market": "JP",
        "publish_time": "2026-06-01T15:00:00+09:00",
        "guidance_revision_pct": 0.2,
    }
    config = {
        "horizons": [5],
        "notional_jpy": 100_000,
        "thresholds": {
            "directional_score": 0.6,
            "directional_confidence": 0.7,
            "guidance_revision_pct": 0.1,
            "monthly_yoy_pct": 0.1,
            "insider_cluster_score": 3,
        },
        "cost_model": {
            "jp_spread_bps_each_side": {"notional_lte_100k": 20, "notional_lte_500k": 10, "larger": 5},
            "us_commission_rate_each_side": 0.00495,
            "us_commission_cap_usd_each_side": 22,
            "us_spread_bps_each_side": 5,
            "rakuten_fx_spread_jpy_per_usd_each_side": 0.25,
        },
    }
    result = simulate_shadow_book([feature], {"1234.T": prices}, config=config)
    trade = result["trades"][0]
    assert trade["theoretical_return"] == 0.1
    assert trade["cost_return"] == 0.004
    assert trade["pnl_jpy"] == 9600
    assert estimate_round_trip_cost_pct(market="US", notional_jpy=100_000, config=config) > 0.01


def test_nisa_score_prefers_growth_and_keeps_swing_taxable() -> None:
    growth = score_nisa_placement({
        "ticker": "GROW",
        "currency": "JPY",
        "investment_type": "long",
        "expected_return_pct": 0.12,
        "dividend_yield": 0.005,
    })
    swing = score_nisa_placement({
        "ticker": "SWING",
        "currency": "JPY",
        "investment_type": "swing",
        "expected_return_pct": 0.12,
    })
    assert growth["recommended_account"] == "NISA成長投資枠"
    assert swing["recommended_account"] == "課税口座"


def test_tax_harvest_scanner_excludes_nisa_and_warns_same_day(tmp_path, monkeypatch) -> None:
    import action_state_tracker
    # scan_tax_harvest() calls action_state_tracker.record_recommendations() as a
    # side effect (2026-07-12 recommendation_id integration) — without this, the
    # synthetic "AAA" candidate below leaks into the real production
    # action_state.json on every test run (discovered 2026-07-13, see
    # feedback_financial_ledger_confirmation memory).
    monkeypatch.setattr(action_state_tracker, "STATE_FILE", tmp_path / "action_state.json")

    lots = {"lots": {
        "AAA": [
            {"remaining_qty": 10, "cost_per_share_jpy": 20_000, "currency": "JPY", "account": "特定"},
            {"remaining_qty": 10, "cost_per_share_jpy": 20_000, "currency": "JPY", "account": "NISA成長投資枠"},
        ]
    }}
    recommended = []

    def recommend(*args, **kwargs):
        recommended.append(kwargs)
        return {"plan": [{"quantity": 10}]}

    report = scan_tax_harvest(
        lots_snapshot=lots,
        price_provider=lambda ticker, currency: (10_000, None),
        recommend_func=recommend,
    )
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["account"] == "特定"
    assert recommended[0]["mode"] == "loss_harvest"
    assert "翌営業日以降" in REPURCHASE_WARNING


def test_edinet_code_map_resolves_stake_target_via_issuer_code() -> None:
    """大量保有: 提出者(ファンド)の secCode ではなく issuerEdinetCode→証券コードで対象解決。"""
    import io
    import zipfile

    import edinet_fetcher as ef

    csv_text = (
        "ダウンロード,2026/06/11\n"
        "ＥＤＩＮＥＴコード,提出者名,証券コード,提出者法人番号\n"
        "E01777,トヨタ自動車,72030,123\n"
        "E99999,Some Fund,,456\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("EdinetcodeDlInfo.csv", csv_text.encode("cp932"))
    code_map = ef._build_code_map(buf.getvalue())
    assert code_map == {"E01777": "7203.T"}  # missing secCode row dropped

    payload = {"results": [{
        "docID": "S1", "secCode": None, "filerName": "Oasis Management",
        "docDescription": "大量保有報告書", "docTypeCode": "350",
        "submitDateTime": "2026-06-10 15:00", "issuerEdinetCode": "E01777",
    }]}
    item = ef.normalize_edinet_documents(
        payload, target_map=code_map, activist_names=["oasis management"]
    )[0]
    assert item["ticker"] == "7203.T"
    assert item["ticker_resolution_method"] == "target_resolution"
    assert item["ticker_resolution_confidence"] == 1.0
    assert item["activist_flag"] is True


def test_edinet_bare_code_label_does_not_misresolve() -> None:
    """"EDINETコード E12345" の5桁を証券コードと誤解決しない (P2: 偽陽性)。"""
    from edinet_fetcher import normalize_edinet_documents

    payload = {"results": [{
        "docID": "S2", "secCode": None, "filerName": "X",
        "docDescription": "EDINETコード E12345 提出", "docTypeCode": "350",
        "submitDateTime": "2026-06-10 15:00",
    }]}
    assert normalize_edinet_documents(payload) == []  # unresolved → dropped, not guessed


def test_guidance_parser_ignores_dates_in_label_row() -> None:
    """前回/今回の見出しに紛れる (2026年5月14日) を金額と誤読しない (P1)。"""
    text = """
    業績予想の修正
    売上高 営業利益 経常利益
    前回発表予想 (2026年5月14日公表) 10,000 1,000 900
    今回修正予想 (2026年6月11日公表) 11,000 1,300 1,100
    """
    assert parse_guidance_revision_pct(text) == 0.3


def test_monthly_parser_bare_percent_is_positive_delta() -> None:
    """符号も増減語も無い "前年比 8.5%" は +0.085 (指数の -91.5% と誤読しない、P2)。"""
    assert parse_monthly_yoy_pct("前年比 8.5%") == 0.085


def test_universe_filter_exempts_activist_stake() -> None:
    """固定ユニバース外でも known-activist の大量保有は落とさない (P2)。"""
    from ingest_disclosures import _apply_universe_filter

    items = [
        {"ticker": "1234.T", "disclosure_type": "earnings"},          # in universe
        {"ticker": "9999.T", "disclosure_type": "earnings"},          # out → dropped
        {"ticker": "8888.T", "disclosure_type": "stake", "activist_flag": True},  # exempt
    ]
    kept = {it["ticker"] for it in _apply_universe_filter(items, ["1234.T"])}
    assert kept == {"1234.T", "8888.T"}


def test_shadow_book_reports_missing_prices() -> None:
    """価格欠落は missing_price_tickers として表面化し、無言の0件にならない (P1)。"""
    feature = {
        "feature_id": "f1", "ticker": "7777.T", "market": "JP",
        "publish_time": "2026-06-01T15:00:00+09:00", "guidance_revision_pct": 0.2,
    }
    result = simulate_shadow_book([feature], {}, config={
        "horizons": [5], "notional_jpy": 100_000,
        "thresholds": {"directional_score": 0.6, "directional_confidence": 0.7,
                       "guidance_revision_pct": 0.1, "monthly_yoy_pct": 0.1,
                       "insider_cluster_score": 3},
        "cost_model": {"jp_spread_bps_each_side": {"notional_lte_100k": 20,
                       "notional_lte_500k": 10, "larger": 5},
                       "us_commission_rate_each_side": 0.00495,
                       "us_commission_cap_usd_each_side": 22,
                       "us_spread_bps_each_side": 5,
                       "rakuten_fx_spread_jpy_per_usd_each_side": 0.25}})
    assert result["trade_count"] == 0
    assert result["signal_ticker_count"] == 1
    assert result["missing_price_tickers"] == ["7777.T"]


def test_morning_brief_section_is_observe_only() -> None:
    rows = [{
        "ticker": "1234.T",
        "publish_time": "2026-06-10T15:00:00+09:00",
        "disclosure_type": "guidance",
        "guidance_revision_pct": 0.2,
        "summary": "上方修正",
    }]
    selected = yesterday_disclosure_signals(
        rows=rows, now=datetime(2026, 6, 11, 7, tzinfo=timezone.utc)
    )
    text = format_brief_section(selected)
    assert selected and "未検証・観測のみ" in text and "売買推奨ではなく" in text


def test_morning_brief_ranks_jp_dilution_and_going_concern_signals() -> None:
    rows = [
        {
            "ticker": "NOISE.T",
            "publish_time": "2026-06-10T15:00:00+09:00",
            "disclosure_type": "other",
            "directional_score": 0.1,
            "directional_confidence": 0.5,
            "summary": "軽微な役員人事",
        },
        {
            "ticker": "DILUTE.T",
            "publish_time": "2026-06-10T15:01:00+09:00",
            "disclosure_type": "other",
            "dilution_flag": True,
            "summary": "公募増資",
        },
        {
            "ticker": "GC.T",
            "publish_time": "2026-06-10T15:02:00+09:00",
            "disclosure_type": "other",
            "going_concern_flag": True,
            "summary": "継続企業の前提に関する注記",
        },
    ]

    selected = yesterday_disclosure_signals(
        rows=rows,
        now=datetime(2026, 6, 11, 7),
        limit=3,
    )

    assert {row["ticker"] for row in selected[:2]} == {"DILUTE.T", "GC.T"}
    assert selected[-1]["ticker"] == "NOISE.T"


def test_buyback_ratio_parser_matches_hand_calculation() -> None:
    text = "自己株式の取得に係る事項の決定に関するお知らせ\n発行済株式総数に対する割合 3.10％"
    assert parse_buyback_ratio_pct(text) == 3.1
    assert parse_buyback_ratio_pct("配当予想のみ") is None


def test_buyback_ratio_parser_ignores_disposal_direction() -> None:
    # 自己株式の「処分」は取得と逆方向 (希薄化寄り) のため誤って正シグナル化しない。
    text = "自己株式の処分に関するお知らせ\n発行済株式総数に対する割合 2.00％"
    assert parse_buyback_ratio_pct(text) is None


def test_buyback_ratio_parser_rejects_implausible_ratio() -> None:
    text = "自己株式の取得\n発行済株式総数に対する割合 45.0％"
    assert parse_buyback_ratio_pct(text) is None


def test_buyback_directional_score_calibration() -> None:
    assert buyback_directional_score(5.0) == 1.0
    assert buyback_directional_score(10.0) == 1.0  # 上限クリップ
    assert buyback_directional_score(2.5) == 0.5
    assert buyback_directional_score(0.1) == 0.2  # フロア


def test_tdnet_classifies_buyback_and_excludes_disposal() -> None:
    payload = {"items": [
        {"Tdnet": {
            "id": "b1", "company_code": "12340", "pubdate": "2026-06-10 15:00:00",
            "title": "自己株式取得に係る事項の決定に関するお知らせ",
        }},
        {"Tdnet": {
            "id": "b2", "company_code": "12340", "pubdate": "2026-06-10 15:01:00",
            "title": "自己株式の処分に関するお知らせ",
        }},
    ]}
    items = normalize_tdnet_items(payload)
    assert items[0]["disclosure_type"] == "buyback"
    assert items[1]["disclosure_type"] == "other"


def test_deterministic_ingest_writes_buyback_fields(tmp_path) -> None:
    item = {
        "source": "tdnet", "ticker": "1234.T", "native_doc_id": "bb-1",
        "source_url": "https://example/bb.pdf",
        "publish_time": "2026-06-01T15:00:00+09:00", "market": "JP", "language": "ja",
        "disclosure_type": "buyback", "title": "自己株式取得に係る事項の決定に関するお知らせ",
        "body": "発行済株式総数に対する割合 4.00％",
    }
    store = tmp_path / "features.jsonl"
    report = ingest_items([item], store_path=store, fsync=False)
    rows = [json.loads(line) for line in store.read_text().splitlines()]
    assert report["deterministic_written"] == 1
    assert rows[0]["buyback_flag"] is True
    assert rows[0]["buyback_ratio_pct"] == 4.0


def test_shadow_book_signal_from_buyback_feature_is_positive_direction() -> None:
    signal = signal_from_feature(
        {"buyback_flag": True, "buyback_ratio_pct": 5.0},
        {
            "directional_score": 0.6, "directional_confidence": 0.7,
            "guidance_revision_pct": 0.1, "monthly_yoy_pct": 0.1,
            "insider_cluster_score": 3,
        },
    )
    assert signal == {"feature_name": "buyback_flag", "strength": 1.0, "direction": 1}

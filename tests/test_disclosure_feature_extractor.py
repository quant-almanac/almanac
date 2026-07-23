"""Tests for disclosure_feature_extractor — parse → validate → store.

Uses an injected transport so the whole path runs with no network and no spend.
Covers: happy path + token capture, public_news vs public_disclosure kind,
parse-failure handling, range clamping, null preservation, required-field
guarding, and store idempotency.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from disclosure_feature_extractor import PROMPT_VERSION, extract_features  # noqa: E402
from almanac.observability.disclosure_features import read_features  # noqa: E402

_ITEM = {
    "source": "edgar",
    "ticker": "AAPL",
    "native_doc_id": "0000320193-26-000010",
    "publish_time": "2026-06-01T05:00:00+00:00",
    "market": "US",
    "disclosure_type": "earnings",
    "title": "Apple raises full-year guidance",
    "body": "The company raised its FY operating profit outlook above consensus.",
}


def _transport(content: str):
    def _tx(**kwargs):
        return content, {"input_tokens": 100, "output_tokens": 50}

    return _tx


def _ok_json(**over) -> str:
    payload = {
        "directional_score": 0.6,
        "directional_confidence": 0.8,
        "catalyst_specificity": 0.7,
        "contradiction_count": 1,
        "expectation_gap": None,
        "summary": "raised FY guidance above consensus",
        "evidence": [{"quote": "raised its FY operating profit outlook", "claim": "guidance up"}],
    }
    payload.update(over)
    return json.dumps(payload)


def test_happy_path_builds_and_stores(tmp_path: Path) -> None:
    store = tmp_path / "f.jsonl"
    log = tmp_path / "calls.jsonl"
    res = extract_features(
        _ITEM,
        transport=_transport(_ok_json()),
        store_path=store,
        log_path=log,
        fsync=False,
    )
    assert res["ok"] is True
    f = res["feature"]
    assert f.source_event_id == "edgar:0000320193-26-000010"
    assert f.directional_score == 0.6
    assert f.observe_only is True
    assert f.prompt_version == PROMPT_VERSION
    assert (f.input_tokens, f.output_tokens) == (100, 50)
    assert f.cost_usd == pytest.approx(0.000082)
    assert f.raw_text_sha256 and len(f.raw_text_sha256) == 64
    assert res["append"]["written"] is True
    assert len(read_features(store)) == 1
    call_row = json.loads(log.read_text(encoding="utf-8").strip())
    assert call_row["cost_usd"] == pytest.approx(0.000082)


def test_news_uses_public_news_kind(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    item = {**_ITEM, "source": "news", "native_doc_id": None,
            "source_url": "https://news.example/x", "disclosure_type": "other"}
    res = extract_features(
        item, transport=_transport(_ok_json()),
        store_path=tmp_path / "f.jsonl", log_path=log, fsync=False,
    )
    assert res["ok"] is True
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["kind"] == "public_news"


def test_parse_failure_is_handled(tmp_path: Path) -> None:
    debug_log = tmp_path / "disclosure_extract_debug.jsonl"
    res = extract_features(
        _ITEM, transport=_transport("the model rambled with no json"),
        store_path=tmp_path / "f.jsonl", log_path=tmp_path / "c.jsonl", fsync=False,
    )
    assert res["ok"] is False
    assert "parse" in res["error"].lower()
    row = json.loads(debug_log.read_text(encoding="utf-8").strip())
    assert row["reason"] == "parse_json_none"
    assert row["ticker"] == "AAPL"
    assert row["disclosure_type"] == "earnings"
    assert row["feature_id"]
    assert row["source_event_id"] == "edgar:0000320193-26-000010"
    assert row["raw_response_excerpt"] == "the model rambled with no json"
    assert "body" not in row


def test_missing_directional_score_is_logged_for_next_cycle_diagnosis(tmp_path: Path) -> None:
    content = _ok_json()
    payload = json.loads(content)
    payload.pop("directional_score")
    res = extract_features(
        _ITEM, transport=_transport(json.dumps(payload)),
        store_path=tmp_path / "f.jsonl", log_path=tmp_path / "c.jsonl", fsync=False,
    )

    assert res["ok"] is True
    assert res["feature"].directional_score is None
    row = json.loads((tmp_path / "disclosure_extract_debug.jsonl").read_text(encoding="utf-8").strip())
    assert row["reason"] == "directional_score_missing_or_null"
    assert row["feature_id"] == res["feature"].feature_id
    assert "directional_score" not in row["parsed_keys"]
    assert len(row["raw_response_excerpt"]) <= 2000


def test_out_of_range_values_are_clamped(tmp_path: Path) -> None:
    content = _ok_json(directional_score=1.5, catalyst_specificity=2.0, contradiction_count=-3)
    res = extract_features(
        _ITEM, transport=_transport(content),
        store_path=tmp_path / "f.jsonl", log_path=tmp_path / "c.jsonl", fsync=False,
    )
    assert res["ok"] is True
    f = res["feature"]
    assert f.directional_score == 1.0
    assert f.catalyst_specificity == 1.0
    assert f.contradiction_count == 0


def test_null_features_preserved(tmp_path: Path) -> None:
    content = _ok_json(expectation_gap=None, narrative_delta=None)
    res = extract_features(
        _ITEM, transport=_transport(content),
        store_path=tmp_path / "f.jsonl", log_path=tmp_path / "c.jsonl", fsync=False,
    )
    f = res["feature"]
    assert f.expectation_gap is None
    assert f.narrative_delta is None


def test_missing_required_fields_rejected(tmp_path: Path) -> None:
    bad = {k: v for k, v in _ITEM.items() if k != "ticker"}
    res = extract_features(
        bad, transport=_transport(_ok_json()),
        store_path=tmp_path / "f.jsonl", log_path=tmp_path / "c.jsonl", fsync=False,
    )
    assert res["ok"] is False


def test_second_order_impact_sign_clamped_end_to_end(tmp_path: Path) -> None:
    """Model may emit sign=99; extractor must collapse to {-1,0,1} so the store
    (which enforces sign in {-1,0,1}) accepts it."""
    content = _ok_json(second_order_impact=[
        {"ticker": "TSM", "sign": 99},
        {"ticker": "AMD", "sign": -5},
    ])
    res = extract_features(
        _ITEM, transport=_transport(content),
        store_path=tmp_path / "f.jsonl", log_path=tmp_path / "c.jsonl", fsync=False,
    )
    assert res["ok"] is True, res["error"]
    assert res["feature"].second_order_impact == [
        {"ticker": "TSM", "sign": 1},
        {"ticker": "AMD", "sign": -1},
    ]


def test_prompt_version_is_threaded_to_stored_row(tmp_path: Path) -> None:
    """A non-default prompt_version must be stored (so the pre-check matches and
    re-runs don't re-charge the LLM) — R2 P2 fix."""
    res = extract_features(
        _ITEM, transport=_transport(_ok_json()), store_path=tmp_path / "f.jsonl",
        log_path=tmp_path / "c.jsonl", fsync=False, prompt_version="custom-v2")
    assert res["ok"] is True
    assert res["feature"].prompt_version == "custom-v2"


def test_idempotent_store(tmp_path: Path) -> None:
    store = tmp_path / "f.jsonl"
    log = tmp_path / "c.jsonl"
    extract_features(_ITEM, transport=_transport(_ok_json()), store_path=store,
                     log_path=log, fsync=False)
    r2 = extract_features(_ITEM, transport=_transport(_ok_json()), store_path=store,
                          log_path=log, fsync=False)
    assert r2["append"]["duplicate"] is True
    assert len(read_features(store)) == 1

from __future__ import annotations

from datetime import datetime, timezone
import json

import analyst
import disclosure_push
import ingest_disclosures
from insider_restrictions import (
    filter_allowed_tickers,
    is_restricted_ticker,
    signal_record_is_restricted,
)
from almanac.observability.candidate_extractor import extract_all
from almanac.observability.catalyst_layer import run as run_catalyst
import long_term_screener
import short_screener


RESTRICTED = "9999.T"


def test_minimum_restriction_survives_missing_config(tmp_path):
    assert is_restricted_ticker(RESTRICTED, path=tmp_path / "missing.json")
    assert is_restricted_ticker("9999", path=tmp_path / "missing.json")
    assert filter_allowed_tickers(["AAPL", RESTRICTED]) == ["AAPL"]


def test_pair_schema_detects_restricted_leg():
    assert signal_record_is_restricted({"long": "AAPL", "short": RESTRICTED})
    assert signal_record_is_restricted({"pair": f"AAPL/{RESTRICTED}"})


def test_explicit_universe_filters_restricted_ticker(tmp_path):
    path = tmp_path / "universe.json"
    path.write_text(json.dumps({"tickers": [RESTRICTED, "1377.T"]}))

    assert ingest_disclosures.resolve_scan_universe(universe_path=path) == ["1377.T"]


def test_ingest_restricted_item_never_reaches_store_or_llm(tmp_path):
    called = []

    def transport(**kwargs):
        called.append(kwargs)
        raise AssertionError("restricted item reached LLM")

    report = ingest_disclosures.ingest_items(
        [{
            "ticker": RESTRICTED,
            "source": "tdnet",
            "native_doc_id": "restricted-1",
            "publish_time": "2026-06-12T15:00:00+09:00",
            "title": "test",
            "body": "test",
        }],
        store_path=tmp_path / "features.jsonl",
        transport=transport,
    )

    assert report["skipped_restricted"] == 1
    assert called == []
    assert not (tmp_path / "features.jsonl").exists()


def test_restricted_feature_never_pushes(tmp_path):
    sent = []
    row = {
        "feature_id": "restricted-feature",
        "ticker": RESTRICTED,
        "publish_time": datetime.now(timezone.utc).isoformat(),
        "directional_score": 0.9,
        "directional_confidence": 0.9,
    }

    report = disclosure_push.push_new_disclosure_features(
        rows=[row],
        state_path=tmp_path / "push.json",
        send=sent.append,
    )

    assert report["sent_count"] == 0
    assert sent == []


def test_candidate_extractor_drops_restricted_from_all_legacy_surfaces():
    action = {"ticker": RESTRICTED, "type": "buy", "reason": "must not escape"}
    packets = extract_all(
        analysis_id="a1",
        analysis_date="2026-06-12",
        long_tier={"priority_actions": [action]},
        synthesis={"priority_actions": [action]},
        margin_deepseek={"candidates": [{"ticker": RESTRICTED}]},
        short_deepseek={"candidates": [{"ticker": RESTRICTED}]},
    )

    assert packets == []


def test_catalyst_log_excludes_restricted_from_revision_lane(tmp_path):
    revision = tmp_path / "revision_state.json"
    revision.write_text(json.dumps({
        "tickers": {
            RESTRICTED: {
                "ticker": RESTRICTED,
                "direction": "up",
                "magnitude_pct": 20,
                "confidence": 0.9,
                "evidence": ["public guidance revision"],
                "source_event_id": "revision:restricted",
            }
        }
    }))
    log = tmp_path / "catalyst.jsonl"

    output = run_catalyst(
        revision_state_path=revision,
        catalyst_log_path=log,
        analysis_id="a1",
        analysis_date="2026-06-12",
        write_log=True,
    )

    assert output.n_hypotheses_total == 0
    assert not log.exists() or RESTRICTED not in log.read_text()


def test_screener_serializers_remove_restricted(tmp_path, monkeypatch):
    short_path = tmp_path / "short.json"
    short_screener._save_candidates(
        {"candidates": [{"ticker": RESTRICTED}, {"ticker": "AAPL"}]},
        short_path,
    )
    assert [row["ticker"] for row in json.loads(short_path.read_text())["candidates"]] == ["AAPL"]

    long_path = tmp_path / "long.json"
    monkeypatch.setattr(long_term_screener, "RESULTS_FILE", long_path)
    long_term_screener.save_results({
        "passed": [{"ticker": RESTRICTED}, {"ticker": "MSFT"}],
        "rejected": [],
        "watchlist_by_sector": {"X": [{"ticker": RESTRICTED}, {"ticker": "MSFT"}]},
    })
    saved = json.loads(long_path.read_text())
    assert [row["ticker"] for row in saved["passed"]] == ["MSFT"]
    assert [row["ticker"] for row in saved["watchlist_by_sector"]["X"]] == ["MSFT"]


def test_phase1_post_filter_blocks_restricted_priority_action(monkeypatch):
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [])
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: set())
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: set())
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: set())
    monkeypatch.setattr("behavioral_guard.is_rebalance_in_cooldown", lambda vix=None: (False, ""))
    synthesis = {
        "priority_actions": [{
            "ticker": RESTRICTED,
            "type": "trim",
            "action": "restricted employer trim",
        }]
    }

    result = analyst._phase1_post_filter(synthesis, 10_000_000)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("insider_restricted:")

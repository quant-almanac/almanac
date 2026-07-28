"""Frozen decision prices and the two recommendation-evaluation axes."""
from __future__ import annotations

import json

import action_state_tracker
import analyst


def _no_op_record_recommendations(actions, source=None):
    return 0


def test_uses_decision_price_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(action_state_tracker, "record_recommendations", _no_op_record_recommendations)
    synthesis = {
        "priority_actions": [{
            "ticker": "AAPL", "type": "buy",
            "decision_price": 210.5, "limit_price": 211.0,
        }],
        "analysis_id": "test-analysis-1",
    }
    analyst._log_recommendations(synthesis, market_meta={})
    log = json.loads((tmp_path / "ai_recommendation_log.json").read_text(encoding="utf-8"))
    assert log[0]["price_at_rec"] == 210.5
    assert log[0]["price_at_rec_source"] == "decision_price"


def test_falls_back_to_limit_price_when_decision_price_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(action_state_tracker, "record_recommendations", _no_op_record_recommendations)
    synthesis = {
        "priority_actions": [{"ticker": "AAPL", "type": "buy", "limit_price": 211.0}],
        "analysis_id": "test-analysis-2",
    }
    analyst._log_recommendations(synthesis, market_meta={})
    log = json.loads((tmp_path / "ai_recommendation_log.json").read_text(encoding="utf-8"))
    assert log[0]["price_at_rec"] == 211.0
    assert log[0]["price_at_rec_source"] == "limit_price"


def test_missing_frozen_price_is_not_refetched_and_signal_is_unevaluable(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(action_state_tracker, "record_recommendations", _no_op_record_recommendations)
    synthesis = {
        "priority_actions": [{"ticker": "AAPL", "type": "buy"}],
        "analysis_id": "test-analysis-3",
    }
    analyst._log_recommendations(synthesis, market_meta={})
    log = json.loads((tmp_path / "ai_recommendation_log.json").read_text(encoding="utf-8"))
    assert log[0]["price_at_rec"] is None
    assert log[0]["price_at_rec_source"] is None
    assert log[0]["signal_evaluable"] is False


def test_invalid_decision_price_falls_through_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(action_state_tracker, "record_recommendations", _no_op_record_recommendations)
    synthesis = {
        "priority_actions": [{
            "ticker": "AAPL", "type": "buy",
            "decision_price": "not-a-number", "limit_price": 205.25,
        }],
        "analysis_id": "test-analysis-4",
    }
    analyst._log_recommendations(synthesis, market_meta={})
    log = json.loads((tmp_path / "ai_recommendation_log.json").read_text(encoding="utf-8"))
    assert log[0]["price_at_rec"] == 205.25
    assert log[0]["price_at_rec_source"] == "limit_price"


def test_readiness_axes_are_recorded_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(action_state_tracker, "record_recommendations", _no_op_record_recommendations)
    synthesis = {
        "priority_actions": [{
            "ticker": "AAPL",
            "type": "buy",
            "decision_price": 210.5,
            "execution_readiness": "review",
            "execution_block_reasons": [{
                "code": "cash_resource_stale",
                "message": "cash needs refresh",
            }],
        }],
        "analysis_id": "test-analysis-5",
    }
    analyst._log_recommendations(synthesis, market_meta={})
    log = json.loads((tmp_path / "ai_recommendation_log.json").read_text(encoding="utf-8"))
    assert log[0]["signal_evaluable"] is True
    assert log[0]["execution_eligible"] is False

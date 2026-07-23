"""Tests for almanac.observability.candidate_extractor.

The extractor is the **only** module that bridges the legacy producer
schemas (Sonnet 4 tiers, DeepSeek margin/short, Opus synthesis,
catalyst_layer) to the unified candidate_packet schema. Coverage focuses
on:

- Action-type mapping (including the realistic ``take_profit`` /
  ``rebalance`` / ``cover`` variants that show up in production).
- Direction mapping aligns with the action_type contract from plan §6.9.
- ``hypothesis_id`` is stable across days for the same logical
  ``(ticker, action_type, horizon)`` tuple from the same source agent —
  the Round 8 #1 multi-day join property.
- Producer-specific extractors return ``[]`` for missing / malformed
  inputs rather than raising — the analyzer must complete even when one
  tier emits noise.
- Opus synthesis packets land with ``candidate_status=adopted`` and
  ``source_agents=[opus_final]``.
- catalyst_layer extractor refuses to fabricate a ``source_event_id``
  (a key invariant — see plan Round 11 #C-1).
- Real ``ai_portfolio_analysis.json`` in the worktree extracts the
  expected number of rows when present.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.candidate_extractor import (  # noqa: E402
    AGENT_CATALYST_LAYER,
    AGENT_LONG_SONNET,
    AGENT_MARGIN_DEEPSEEK,
    AGENT_MEDIUM_SONNET,
    AGENT_OPUS_FINAL,
    AGENT_SHORT_DEEPSEEK,
    AGENT_SHORT_SONNET,
    AGENT_SWING_SONNET,
    DEFAULT_HORIZON_DAYS,
    extract_all,
    extract_from_catalyst_layer,
    extract_from_deepseek_margin,
    extract_from_deepseek_short,
    extract_from_sonnet_tier,
    extract_from_synthesis,
    infer_action_type,
    infer_direction,
)
from almanac.observability.status import CandidateStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Action type mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("buy", "buy"),
        ("BUY", "buy"),
        ("Buy", "buy"),
        ("dca", "buy"),
        ("rebuy", "buy"),
        ("add", "buy"),
        ("trim", "trim"),
        ("take_profit", "trim"),   # observed in real ai_portfolio_analysis.json
        ("reduce", "trim"),
        ("rebalance", "trim"),     # observed in real ai_portfolio_analysis.json
        ("sell", "sell"),
        ("exit", "sell"),
        ("close", "sell"),
        ("cover", "buy"),          # short cover = buy to close
        ("short", "short_sell"),
        ("short_sell", "short_sell"),
        ("margin_buy", "margin_buy"),
        ("hold", "hold"),
        ("watch", "hold"),
        ("  buy  ", "buy"),        # whitespace normalized
    ],
)
def test_infer_action_type_maps_known_strings(raw: str, expected: str) -> None:
    assert infer_action_type(raw) == expected


def test_infer_action_type_returns_none_for_no_action() -> None:
    """Round 11 #4 — ``no_action`` is portfolio_decision_state, not a
    candidate action."""
    assert infer_action_type("no_action") is None


def test_infer_action_type_returns_none_for_unknown() -> None:
    assert infer_action_type("yolo") is None
    assert infer_action_type("") is None
    assert infer_action_type(None) is None


@pytest.mark.parametrize(
    "action_type,expected_direction",
    [
        ("buy", "long"),
        ("margin_buy", "long"),
        ("short_sell", "short"),
        ("trim", "reduce"),
        ("sell", "reduce"),
        ("hold", "neutral"),
        ("unknown_garbage", "neutral"),  # fallback
    ],
)
def test_infer_direction(action_type: str, expected_direction: str) -> None:
    assert infer_direction(action_type) == expected_direction


# ---------------------------------------------------------------------------
# Sonnet tier extractor
# ---------------------------------------------------------------------------


def _sonnet_payload(actions: list[dict]) -> dict:
    """Build a realistic tier output shape (matches ai_portfolio_analysis)."""
    return {
        "overall_stance": "balanced",
        "priority_actions": actions,
        "morning_brief_headline": "...",
        "risk_warnings": [],
        "hold_notes": [],
    }


def test_sonnet_returns_empty_for_none_input() -> None:
    assert extract_from_sonnet_tier(None, source_agent=AGENT_LONG_SONNET,
                                    analysis_id="a", analysis_date="2026-05-24") == []


def test_sonnet_returns_empty_for_missing_priority_actions() -> None:
    assert extract_from_sonnet_tier({}, source_agent=AGENT_LONG_SONNET,
                                    analysis_id="a", analysis_date="2026-05-24") == []


def test_sonnet_returns_empty_for_priority_actions_not_a_list() -> None:
    payload = {"priority_actions": {"oops": "dict"}}
    assert extract_from_sonnet_tier(payload, source_agent=AGENT_LONG_SONNET,
                                    analysis_id="a", analysis_date="2026-05-24") == []


def test_sonnet_extracts_one_packet_per_action() -> None:
    payload = _sonnet_payload([
        {"rank": 1, "urgency": "high", "type": "buy", "ticker": "NVDA",
         "action": "buy NVDA", "reason": "AI tailwind", "amount_hint": "10株",
         "confidence_pct": 72, "tier": "Long"},
        {"rank": 2, "urgency": "low", "type": "hold", "ticker": "AVGO",
         "action": "hold", "reason": "wait", "confidence_pct": 50, "tier": "Long"},
    ])
    out = extract_from_sonnet_tier(payload, source_agent=AGENT_LONG_SONNET,
                                   analysis_id="a", analysis_date="2026-05-24")
    assert len(out) == 2
    tickers = {p["ticker"] for p in out}
    assert tickers == {"NVDA", "AVGO"}


def test_sonnet_skips_entries_with_missing_ticker() -> None:
    payload = _sonnet_payload([
        {"type": "buy", "action": "buy something", "confidence_pct": 50},
        {"type": "buy", "ticker": "NVDA", "action": "buy NVDA", "confidence_pct": 70},
    ])
    out = extract_from_sonnet_tier(payload, source_agent=AGENT_LONG_SONNET,
                                   analysis_id="a", analysis_date="2026-05-24")
    assert len(out) == 1
    assert out[0]["ticker"] == "NVDA"


def test_sonnet_skips_entries_with_unmappable_type() -> None:
    """An unknown ``type`` is logged and dropped — not an error."""
    payload = _sonnet_payload([
        {"type": "yolo", "ticker": "NVDA"},
        {"type": "buy", "ticker": "AVGO", "confidence_pct": 60},
    ])
    out = extract_from_sonnet_tier(payload, source_agent=AGENT_LONG_SONNET,
                                   analysis_id="a", analysis_date="2026-05-24")
    assert len(out) == 1
    assert out[0]["ticker"] == "AVGO"


def test_sonnet_packet_carries_canonical_fields() -> None:
    payload = _sonnet_payload([
        {"rank": 1, "urgency": "high", "type": "buy", "ticker": "NVDA",
         "action": "buy NVDA aggressively", "reason": "AI tailwind",
         "amount_hint": "10株", "confidence_pct": 72, "tier": "Long"},
    ])
    out = extract_from_sonnet_tier(payload, source_agent=AGENT_LONG_SONNET,
                                   analysis_id="aid", analysis_date="2026-05-24")
    p = out[0]
    assert p["ticker"] == "NVDA"
    assert p["action_type"] == "buy"
    assert p["direction"] == "long"
    assert p["source_agents"] == [AGENT_LONG_SONNET]
    assert p["analysis_id"] == "aid"
    assert p["analysis_date"] == "2026-05-24"
    assert p["confidence_pct"] == 72
    assert p["candidate_status"] == CandidateStatus.generated.value
    assert p["time_horizon_days"] == DEFAULT_HORIZON_DAYS[AGENT_LONG_SONNET]
    assert "urgency:high" in p["risk_flags"]
    assert "tier_rank:1" in p["risk_flags"]
    assert p["suggested_size"] == "10株"
    assert p["evidence_summary"] == "buy NVDA aggressively | AI tailwind"


def test_sonnet_default_horizon_per_tier() -> None:
    payload = _sonnet_payload([
        {"type": "buy", "ticker": "NVDA", "confidence_pct": 50},
    ])
    for agent in (AGENT_LONG_SONNET, AGENT_MEDIUM_SONNET, AGENT_SWING_SONNET,
                  AGENT_SHORT_SONNET):
        out = extract_from_sonnet_tier(payload, source_agent=agent,
                                       analysis_id="a", analysis_date="2026-05-24")
        assert out[0]["time_horizon_days"] == DEFAULT_HORIZON_DAYS[agent], agent


def test_sonnet_caller_horizon_overrides_default() -> None:
    payload = _sonnet_payload([
        {"type": "buy", "ticker": "NVDA", "confidence_pct": 50},
    ])
    out = extract_from_sonnet_tier(payload, source_agent=AGENT_LONG_SONNET,
                                   analysis_id="a", analysis_date="2026-05-24",
                                   horizon_days=7)
    assert out[0]["time_horizon_days"] == 7


def test_sonnet_coerces_float_or_string_confidence() -> None:
    payload = _sonnet_payload([
        {"type": "buy", "ticker": "NVDA", "confidence_pct": 72.6},
        {"type": "buy", "ticker": "AVGO", "confidence_pct": "60"},
        {"type": "buy", "ticker": "AMD",  "confidence_pct": "garbage"},
        {"type": "buy", "ticker": "INTC", "confidence_pct": 150},   # clamp
    ])
    out = extract_from_sonnet_tier(payload, source_agent=AGENT_LONG_SONNET,
                                   analysis_id="a", analysis_date="2026-05-24")
    confidence_by_ticker = {p["ticker"]: p["confidence_pct"] for p in out}
    assert confidence_by_ticker["NVDA"] == 73
    assert confidence_by_ticker["AVGO"] == 60
    assert confidence_by_ticker["AMD"] is None
    assert confidence_by_ticker["INTC"] == 100


# ---------------------------------------------------------------------------
# hypothesis_id stability (Round 8 #1 / Round 9 #1)
# ---------------------------------------------------------------------------


def _one_packet(ticker="NVDA", source_agent=AGENT_LONG_SONNET,
                action_type="buy", confidence=70) -> dict:
    payload = _sonnet_payload([
        {"type": action_type, "ticker": ticker, "confidence_pct": confidence},
    ])
    return extract_from_sonnet_tier(payload, source_agent=source_agent,
                                    analysis_id="a", analysis_date="2026-05-24")[0]


def test_hypothesis_id_is_stable_across_analysis_dates() -> None:
    """Same logical hypothesis on day 1 and day 2 → same hypothesis_id."""
    a = _one_packet()
    payload = _sonnet_payload([{"type": "buy", "ticker": "NVDA", "confidence_pct": 60}])
    b = extract_from_sonnet_tier(payload, source_agent=AGENT_LONG_SONNET,
                                 analysis_id="other-aid", analysis_date="2026-06-15")[0]
    assert a["hypothesis_id"] == b["hypothesis_id"]
    # row_ids must differ (each call gets a fresh UUID).
    assert a["row_id"] != b["row_id"]


def test_hypothesis_id_changes_with_ticker() -> None:
    assert _one_packet("NVDA")["hypothesis_id"] != _one_packet("AVGO")["hypothesis_id"]


def test_hypothesis_id_changes_with_action_type() -> None:
    assert (
        _one_packet(action_type="buy")["hypothesis_id"]
        != _one_packet(action_type="sell")["hypothesis_id"]
    )


def test_hypothesis_id_changes_with_source_agent() -> None:
    """The Long tier's NVDA buy is a different hypothesis from the
    Medium tier's NVDA buy — the source_event_id encodes the producer."""
    assert (
        _one_packet(source_agent=AGENT_LONG_SONNET)["hypothesis_id"]
        != _one_packet(source_agent=AGENT_MEDIUM_SONNET)["hypothesis_id"]
    )


def test_candidate_id_is_alias_of_hypothesis_id() -> None:
    p = _one_packet()
    assert p["candidate_id"] == p["hypothesis_id"]


# ---------------------------------------------------------------------------
# Synthesis (Opus final) extractor
# ---------------------------------------------------------------------------


def test_synthesis_packets_carry_adopted_status() -> None:
    synthesis = {
        "priority_actions": [
            {"rank": 1, "urgency": "high", "type": "buy", "ticker": "NVDA",
             "action": "buy NVDA", "reason": "AI demand", "confidence_pct": 80},
            {"rank": 2, "urgency": "medium", "type": "trim", "ticker": "META",
             "action": "trim META", "reason": "valuation", "confidence_pct": 60},
        ]
    }
    out = extract_from_synthesis(synthesis, analysis_id="a", analysis_date="2026-05-24")
    assert len(out) == 2
    assert all(p["candidate_status"] == CandidateStatus.adopted.value for p in out)
    assert all(p["source_agents"] == [AGENT_OPUS_FINAL] for p in out)


def test_synthesis_returns_empty_for_none_or_missing_actions() -> None:
    assert extract_from_synthesis(None, analysis_id="a", analysis_date="d") == []
    assert extract_from_synthesis({}, analysis_id="a", analysis_date="d") == []
    assert extract_from_synthesis({"priority_actions": "oops"}, analysis_id="a",
                                  analysis_date="d") == []


# ---------------------------------------------------------------------------
# DeepSeek margin / short extractors
# ---------------------------------------------------------------------------


def test_deepseek_margin_extracts_with_default_action_type() -> None:
    payload = {
        "candidates": [
            {"ticker": "9984.T", "confidence": 65, "reason": "信用拡大余地"},
            {"ticker": "AAPL", "confidence_pct": 70, "reason": "trend"},
        ]
    }
    out = extract_from_deepseek_margin(payload, analysis_id="a", analysis_date="2026-05-24")
    assert len(out) == 2
    assert all(p["action_type"] == "margin_buy" for p in out)
    assert all(p["direction"] == "long" for p in out)
    assert all(p["source_agents"] == [AGENT_MARGIN_DEEPSEEK] for p in out)


def test_deepseek_short_extracts_with_short_sell_action() -> None:
    payload = {
        "candidates": [
            {"ticker": "TSLA", "confidence": 55, "reason": "borrow easy"},
        ]
    }
    out = extract_from_deepseek_short(payload, analysis_id="a", analysis_date="2026-05-24")
    assert len(out) == 1
    assert out[0]["action_type"] == "short_sell"
    assert out[0]["direction"] == "short"


def test_deepseek_explicit_action_overrides_default() -> None:
    payload = {
        "candidates": [
            {"ticker": "9984.T", "action": "hold", "confidence": 40},
        ]
    }
    out = extract_from_deepseek_margin(payload, analysis_id="a", analysis_date="2026-05-24")
    assert out[0]["action_type"] == "hold"


def test_deepseek_returns_empty_for_missing_candidates() -> None:
    assert extract_from_deepseek_margin(None, analysis_id="a", analysis_date="d") == []
    assert extract_from_deepseek_margin({}, analysis_id="a", analysis_date="d") == []
    assert extract_from_deepseek_short({"candidates": "oops"}, analysis_id="a",
                                       analysis_date="d") == []


# ---------------------------------------------------------------------------
# catalyst_layer extractor — strict required fields (R11 #C-1)
# ---------------------------------------------------------------------------


def _catalyst_hypothesis(**overrides) -> dict:
    base = {
        "primary_ticker": "9984.T",
        "action_type": "buy",
        "hypothesis_type": "ipo_proxy",
        "source_event_id": "news:openai_ipo_2026_05_22",
        "time_horizon_days": 20,
        "confidence_pct": 72,
        "evidence_summary": "OpenAI IPO via SoftBank stake",
        "invalidation_summary": "IPO delay / ARM < -5%",
        "expected_alpha_bps": 250,
    }
    base.update(overrides)
    return base


def test_catalyst_layer_extracts_full_hypothesis() -> None:
    out = extract_from_catalyst_layer(
        {"hypotheses": [_catalyst_hypothesis()]},
        analysis_id="a", analysis_date="2026-05-24",
    )
    assert len(out) == 1
    p = out[0]
    assert p["ticker"] == "9984.T"
    assert p["hypothesis_type"] == "ipo_proxy"
    assert p["time_horizon_days"] == 20
    assert p["confidence_pct"] == 72
    assert p["expected_alpha_bps"] == 250
    assert p["invalidation_summary"] == "IPO delay / ARM < -5%"
    assert p["source_agents"] == [AGENT_CATALYST_LAYER]


def test_catalyst_layer_skips_entry_missing_source_event_id() -> None:
    """R11 #C-1 — never fabricate a source_event_id."""
    bad = _catalyst_hypothesis()
    del bad["source_event_id"]
    out = extract_from_catalyst_layer(
        {"hypotheses": [bad]},
        analysis_id="a", analysis_date="2026-05-24",
    )
    assert out == []


def test_catalyst_layer_skips_entry_missing_hypothesis_type() -> None:
    bad = _catalyst_hypothesis()
    del bad["hypothesis_type"]
    out = extract_from_catalyst_layer({"hypotheses": [bad]},
                                      analysis_id="a", analysis_date="2026-05-24")
    assert out == []


def test_catalyst_layer_accepts_candidates_key_for_backward_compat() -> None:
    """Some early prototypes used ``candidates`` instead of ``hypotheses``."""
    out = extract_from_catalyst_layer(
        {"candidates": [_catalyst_hypothesis()]},
        analysis_id="a", analysis_date="2026-05-24",
    )
    assert len(out) == 1


def test_catalyst_layer_returns_empty_for_none() -> None:
    assert extract_from_catalyst_layer(None, analysis_id="a", analysis_date="d") == []


def test_catalyst_layer_hypothesis_id_uses_explicit_source_event_id() -> None:
    """Two catalyst entries with different source_event_id but same
    (ticker, action_type, hypothesis_type, horizon) must still produce
    distinct hypothesis_ids — source_event_id is part of the join key."""
    h1 = _catalyst_hypothesis(source_event_id="news:openai_2026_05_22")
    h2 = _catalyst_hypothesis(source_event_id="news:softbank_2026_05_25")
    out = extract_from_catalyst_layer(
        {"hypotheses": [h1, h2]},
        analysis_id="a", analysis_date="2026-05-26",
    )
    assert len(out) == 2
    assert out[0]["hypothesis_id"] != out[1]["hypothesis_id"]


# ---------------------------------------------------------------------------
# extract_all orchestrator
# ---------------------------------------------------------------------------


def test_extract_all_concatenates_every_producer() -> None:
    out = extract_all(
        analysis_id="aid",
        analysis_date="2026-05-24",
        long_tier=_sonnet_payload([
            {"type": "buy", "ticker": "NVDA", "confidence_pct": 70},
        ]),
        medium_tier=_sonnet_payload([
            {"type": "trim", "ticker": "META", "confidence_pct": 50},
        ]),
        synthesis={
            "priority_actions": [
                {"type": "buy", "ticker": "9984.T", "confidence_pct": 80},
            ],
        },
        margin_deepseek={
            "candidates": [{"ticker": "1570.T", "confidence": 60}],
        },
        catalyst_layer={"hypotheses": [_catalyst_hypothesis()]},
    )
    agents = [p["source_agents"][0] for p in out]
    assert agents == [
        AGENT_LONG_SONNET,
        AGENT_MEDIUM_SONNET,
        AGENT_OPUS_FINAL,
        AGENT_MARGIN_DEEPSEEK,
        AGENT_CATALYST_LAYER,
    ]


def test_extract_all_with_all_inputs_none_returns_empty() -> None:
    """The harness must be able to call extract_all on a freshly-booted
    system where no producer has emitted yet."""
    assert extract_all(analysis_id="a", analysis_date="2026-05-24") == []


# ---------------------------------------------------------------------------
# Real production file compatibility (Codex R11-D-style check)
# ---------------------------------------------------------------------------


_PROD_FILE = _REPO_ROOT / "ai_portfolio_analysis.json"


@pytest.mark.skipif(
    not _PROD_FILE.exists(),
    reason="ai_portfolio_analysis.json not in checkout",
)
def test_real_production_file_extracts_all_priority_actions() -> None:
    """The real file's action count changes with the daily market decision.

    Keep this as a schema compatibility smoke test rather than pinning one
    day's 21-action count; a valid no-trade / low-action day should not fail
    the suite.
    """
    with _PROD_FILE.open("r", encoding="utf-8") as fh:
        d = json.load(fh)
    out = extract_all(
        analysis_id="prod-smoke",
        analysis_date="2026-05-24",
        long_tier=d.get("long_analysis"),
        medium_tier=d.get("medium_analysis"),
        swing_tier=d.get("short_positions_analysis"),
        short_tier=d.get("short_selling_analysis"),
        synthesis=d.get("synthesis"),
    )
    synthesis = d.get("synthesis", {}) or {}
    from insider_restrictions import is_restricted_ticker
    expected_count = sum(
        1
        for action in (synthesis.get("priority_actions", []) or [])
        if not is_restricted_ticker(action.get("ticker"))
    )
    opus_packets = [
        p for p in out
        if AGENT_OPUS_FINAL in (p.get("source_agents") or [])
    ]
    assert len(opus_packets) == expected_count
    if expected_count == 0:
        assert (
            synthesis.get("no_action_rationale")
            or synthesis.get("headline")
            or synthesis.get("stance_reason")
        ), "production no-action days should explain why actions are empty"
        return
    expected_tickers = {
        str(a.get("ticker") or "").upper()
        for a in synthesis.get("priority_actions", [])
        if a.get("ticker") and not is_restricted_ticker(a.get("ticker"))
    }
    actual_tickers = {str(p.get("ticker") or "").upper() for p in opus_packets}
    assert expected_tickers <= actual_tickers
    # Spot-check: every packet has the canonical fields downstream needs.
    for p in out:
        assert p["hypothesis_id"]
        assert p["row_id"]
        assert p["analysis_id"] == "prod-smoke"
        assert p["analysis_date"] == "2026-05-24"
        assert p["ticker"]
        assert p["action_type"] in {"buy", "margin_buy", "short_sell",
                                    "sell", "trim", "hold"}
        assert p["candidate_status"] in {
            CandidateStatus.generated.value,
            CandidateStatus.adopted.value,
        }

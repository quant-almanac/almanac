"""Tests for almanac.observability.invalidation_rules.

The rules drive every belief conviction adjustment in production. Coverage
focuses on:

- Each pure rule function — positive case, near-threshold negative, and
  missing-indicator graceful return.
- The orchestrator handles beliefs without a market snapshot.
- Malformed beliefs are skipped, not crashed-on.
- ``apply_invalidations`` appends well-formed rows to
  ``belief_adjustments.jsonl`` joined via ``belief_id``.
- The rule registry :data:`RULES` is pinned so adding a fourth rule
  requires updating the test deliberately.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.invalidation_rules import (  # noqa: E402
    EXPIRY_DELTA,
    MA20_BREAK_DELTA,
    RSI_OVERHEAT_DELTA,
    RULE_VERSION,
    RULES,
    InvalidationAdjustment,
    MarketIndicators,
    apply_invalidations,
    check_expired,
    check_ma20_break,
    check_rsi_overheat,
    evaluate_belief,
    evaluate_beliefs,
    make_apply,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _belief(**overrides) -> dict:
    base = {
        "id": "abcd1234",
        "ticker": "NVDA",
        "theme": "opportunity",
        "conviction_score": 60,
        "base_conviction": 60,
        "adjusted_conviction": 60,
        "adjustment_log": [],
        "rationale": "earnings revision pullback",
        "source_agent": "opus_synthesis",
        "evidence": "",
        "created_at": "2026-05-01T10:00:00",
        "last_updated": "2026-05-01T10:00:00",
        "expires_at": "2026-06-01T10:00:00",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# check_expired
# ---------------------------------------------------------------------------


def test_check_expired_returns_none_before_expiry() -> None:
    assert check_expired(_belief(), today=date(2026, 5, 24)) is None


def test_check_expired_returns_none_on_exact_expiry_date() -> None:
    """Boundary: ``today <= expires`` → not expired."""
    b = _belief(expires_at="2026-05-24T00:00:00")
    assert check_expired(b, today=date(2026, 5, 24)) is None


def test_check_expired_fires_after_expiry() -> None:
    adj = check_expired(_belief(), today=date(2026, 6, 15))
    assert adj is not None
    assert adj.belief_id == "abcd1234"
    assert adj.delta == EXPIRY_DELTA
    assert adj.reason == "invalidation:expired"
    assert adj.evidence["days_past_expiry"] == 14


def test_check_expired_handles_date_only_format() -> None:
    """``fromisoformat`` should accept plain YYYY-MM-DD too."""
    adj = check_expired(_belief(expires_at="2026-05-01"), today=date(2026, 5, 2))
    assert adj is not None


def test_check_expired_swallows_malformed_timestamp() -> None:
    """Bad data must not crash the cron."""
    assert check_expired(_belief(expires_at="garbage"), today=date(2026, 6, 1)) is None
    assert check_expired(_belief(expires_at=None), today=date(2026, 6, 1)) is None
    assert check_expired(_belief(expires_at=""), today=date(2026, 6, 1)) is None


# ---------------------------------------------------------------------------
# check_rsi_overheat
# ---------------------------------------------------------------------------


def test_rsi_below_threshold_returns_none() -> None:
    assert check_rsi_overheat(_belief(), MarketIndicators("NVDA", rsi_14=70)) is None


def test_rsi_at_exact_threshold_does_not_fire() -> None:
    """Boundary: >75 strict."""
    assert check_rsi_overheat(_belief(), MarketIndicators("NVDA", rsi_14=75)) is None


def test_rsi_above_threshold_fires() -> None:
    adj = check_rsi_overheat(_belief(), MarketIndicators("NVDA", rsi_14=80))
    assert adj is not None
    assert adj.delta == RSI_OVERHEAT_DELTA
    assert adj.reason == "invalidation:rsi_overheat"
    assert adj.evidence == {"rsi_14": 80, "threshold": 75}


def test_rsi_missing_returns_none() -> None:
    """Never invalidate on missing data — plan R8 false-positive guard."""
    assert check_rsi_overheat(_belief(), MarketIndicators("NVDA")) is None


# ---------------------------------------------------------------------------
# check_ma20_break
# ---------------------------------------------------------------------------


def test_ma20_above_returns_none() -> None:
    m = MarketIndicators("NVDA", price=125.0, ma20=120.0)
    assert check_ma20_break(_belief(), m) is None


def test_ma20_at_boundary_does_not_fire() -> None:
    """Boundary: price < ma20 strict (closing at the line is not a break)."""
    m = MarketIndicators("NVDA", price=120.0, ma20=120.0)
    assert check_ma20_break(_belief(), m) is None


def test_ma20_break_fires() -> None:
    m = MarketIndicators("NVDA", price=118.0, ma20=120.0)
    adj = check_ma20_break(_belief(), m)
    assert adj is not None
    assert adj.delta == MA20_BREAK_DELTA
    assert adj.reason == "invalidation:ma20_break"
    assert adj.evidence["price"] == 118.0
    assert adj.evidence["ma20"] == 120.0
    # ~-1.67% below MA20
    assert adj.evidence["below_ma20_pct"] == pytest.approx(-0.01666, abs=1e-4)


def test_ma20_partial_data_returns_none() -> None:
    assert check_ma20_break(_belief(), MarketIndicators("NVDA", price=120.0)) is None
    assert check_ma20_break(_belief(), MarketIndicators("NVDA", ma20=120.0)) is None
    assert check_ma20_break(_belief(), MarketIndicators("NVDA")) is None


# ---------------------------------------------------------------------------
# evaluate_belief — composition
# ---------------------------------------------------------------------------


def test_evaluate_belief_emits_multiple_adjustments() -> None:
    """A belief can violate multiple rules simultaneously."""
    m = MarketIndicators("NVDA", price=100.0, ma20=120.0, rsi_14=80)
    adjs = evaluate_belief(_belief(), today=date(2026, 7, 1), market=m)
    reasons = {a.reason for a in adjs}
    assert reasons == {
        "invalidation:expired",
        "invalidation:rsi_overheat",
        "invalidation:ma20_break",
    }


def test_evaluate_belief_no_market_only_runs_expiry() -> None:
    adjs = evaluate_belief(_belief(), today=date(2026, 7, 1), market=None)
    assert len(adjs) == 1
    assert adjs[0].reason == "invalidation:expired"


def test_evaluate_belief_no_violations_returns_empty_list() -> None:
    m = MarketIndicators("NVDA", price=125.0, ma20=120.0, rsi_14=60)
    assert evaluate_belief(_belief(), today=date(2026, 5, 24), market=m) == []


# ---------------------------------------------------------------------------
# evaluate_beliefs — orchestrator
# ---------------------------------------------------------------------------


def test_orchestrator_skips_beliefs_without_ticker() -> None:
    """MARKET-themed beliefs have no ticker — only expiry can apply."""
    market_only = _belief(ticker=None, expires_at="2026-05-01T00:00:00")
    out = evaluate_beliefs([market_only], today=date(2026, 6, 1), market_snapshot={})
    assert len(out) == 1
    assert out[0].reason == "invalidation:expired"


def test_orchestrator_skips_malformed_belief_without_crashing() -> None:
    good = _belief()
    bad = {"no_id_here": True}
    out = evaluate_beliefs(
        [good, bad],
        today=date(2026, 7, 1),
        market_snapshot={},
    )
    # Only the good belief produced an adjustment (expiry).
    assert len(out) == 1
    assert out[0].belief_id == good["id"]


def test_orchestrator_uses_per_ticker_snapshot() -> None:
    a = _belief(id="aaa", ticker="NVDA")
    b = _belief(id="bbb", ticker="9984.T")
    snap = {
        "NVDA": MarketIndicators("NVDA", price=100.0, ma20=120.0, rsi_14=50),
        "9984.T": MarketIndicators("9984.T", price=150.0, ma20=120.0, rsi_14=80),
    }
    out = evaluate_beliefs([a, b], today=date(2026, 5, 24), market_snapshot=snap)
    by_belief = {(adj.belief_id, adj.reason) for adj in out}
    assert ("aaa", "invalidation:ma20_break") in by_belief
    assert ("bbb", "invalidation:rsi_overheat") in by_belief
    # NVDA RSI=50 must NOT fire overheat.
    assert ("aaa", "invalidation:rsi_overheat") not in by_belief
    # 9984.T price>MA20 must NOT fire MA break.
    assert ("bbb", "invalidation:ma20_break") not in by_belief


def test_orchestrator_handles_missing_snapshot_ticker() -> None:
    """No snapshot for a ticker → only expiry can fire."""
    out = evaluate_beliefs(
        [_belief(expires_at="2026-05-01")],
        today=date(2026, 6, 1),
        market_snapshot={},
    )
    assert len(out) == 1
    assert out[0].reason == "invalidation:expired"


# ---------------------------------------------------------------------------
# apply_invalidations — disk I/O
# ---------------------------------------------------------------------------


def _v2_beliefs_file(tmp_path: Path, beliefs: list[dict]) -> Path:
    p = tmp_path / "agent_beliefs.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "version": "1.0",
                "last_updated": "2026-05-24T00:00:00Z",
                "beliefs": beliefs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return p


def test_apply_writes_well_formed_adjustment_rows(tmp_path: Path) -> None:
    beliefs_path = _v2_beliefs_file(tmp_path, [_belief()])
    adjustments_path = tmp_path / "belief_adjustments.jsonl"
    row_ids = apply_invalidations(
        beliefs_path,
        adjustments_path,
        today=date(2026, 7, 1),
        market_snapshot={
            "NVDA": MarketIndicators("NVDA", price=118.0, ma20=120.0, rsi_14=80),
        },
        fsync=False,
    )
    assert len(row_ids) == 3  # expiry + rsi + ma
    lines = adjustments_path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    assert {r["row_id"] for r in rows} == set(row_ids)
    assert all(r["belief_id"] == "abcd1234" for r in rows)
    assert all(r["rule_version"] == RULE_VERSION for r in rows)
    assert all(r["adjustment_id"] == r["row_id"] for r in rows)  # alias
    reasons = {r["reason"] for r in rows}
    assert reasons == {
        "invalidation:expired",
        "invalidation:rsi_overheat",
        "invalidation:ma20_break",
    }
    deltas = {r["delta"] for r in rows}
    assert deltas == {EXPIRY_DELTA, RSI_OVERHEAT_DELTA, MA20_BREAK_DELTA}


def test_apply_no_violations_writes_nothing(tmp_path: Path) -> None:
    beliefs_path = _v2_beliefs_file(tmp_path, [_belief()])
    adjustments_path = tmp_path / "belief_adjustments.jsonl"
    row_ids = apply_invalidations(
        beliefs_path,
        adjustments_path,
        today=date(2026, 5, 24),
        market_snapshot={
            "NVDA": MarketIndicators("NVDA", price=130.0, ma20=120.0, rsi_14=55),
        },
        fsync=False,
    )
    assert row_ids == []
    assert not adjustments_path.exists()


def test_apply_rejects_non_v2_shape(tmp_path: Path) -> None:
    """Wrong top-level shape suggests migration was not run; refuse."""
    p = tmp_path / "agent_beliefs.json"
    p.write_text(json.dumps([_belief()]))  # bare list instead of {beliefs: [...]}
    with pytest.raises(ValueError, match="is the v1→v2 migration done"):
        apply_invalidations(
            p,
            tmp_path / "out.jsonl",
            today=date(2026, 7, 1),
            market_snapshot={},
            fsync=False,
        )


def test_apply_is_idempotent_within_same_day(tmp_path: Path) -> None:
    """Codex Round 12 P1 #1 — re-running the same day must NOT stack
    duplicate ``-10/-15/-15`` deltas onto the same belief."""
    beliefs_path = _v2_beliefs_file(tmp_path, [_belief()])
    adjustments_path = tmp_path / "belief_adjustments.jsonl"
    market = {"NVDA": MarketIndicators("NVDA", price=118.0, ma20=120.0, rsi_14=80)}
    today = date(2026, 7, 1)

    first = apply_invalidations(beliefs_path, adjustments_path,
                                today=today, market_snapshot=market, fsync=False)
    second = apply_invalidations(beliefs_path, adjustments_path,
                                 today=today, market_snapshot=market, fsync=False)

    # First run writes 3 adjustments; second writes zero (idempotent).
    assert len(first) == 3
    assert len(second) == 0

    # On disk: exactly 3 rows, not 6.
    rows = [
        json.loads(line)
        for line in adjustments_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 3
    # And the 3 reasons are exactly the rules that fired.
    assert {r["reason"] for r in rows} == {
        "invalidation:expired",
        "invalidation:rsi_overheat",
        "invalidation:ma20_break",
    }


def test_apply_writes_new_adjustments_on_a_later_day(tmp_path: Path) -> None:
    """Idempotency is per-day — a true cron tomorrow must still write."""
    beliefs_path = _v2_beliefs_file(tmp_path, [_belief()])
    adjustments_path = tmp_path / "belief_adjustments.jsonl"
    market = {"NVDA": MarketIndicators("NVDA", price=118.0, ma20=120.0, rsi_14=80)}

    apply_invalidations(beliefs_path, adjustments_path,
                        today=date(2026, 7, 1), market_snapshot=market, fsync=False)
    next_day = apply_invalidations(beliefs_path, adjustments_path,
                                   today=date(2026, 7, 2), market_snapshot=market, fsync=False)
    assert len(next_day) == 3
    # Disk now holds two days' worth: 6 rows total.
    rows = [
        json.loads(line)
        for line in adjustments_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 6


def test_apply_writes_only_missing_rules_on_partial_re_run(tmp_path: Path) -> None:
    """If only RSI is already recorded today, MA20 should still write on
    a subsequent call. Use a far-future expiry so the expired rule
    cannot interfere with the partial-rerun semantics under test."""
    far_future = _belief(expires_at="2099-01-01T00:00:00")
    beliefs_path = _v2_beliefs_file(tmp_path, [far_future])
    adjustments_path = tmp_path / "belief_adjustments.jsonl"
    today = date(2026, 7, 1)

    # First call: only RSI fires (no expiry, no MA20 data).
    rsi_only = apply_invalidations(
        beliefs_path, adjustments_path,
        today=today,
        market_snapshot={"NVDA": MarketIndicators("NVDA", rsi_14=80)},
        fsync=False,
    )
    assert len(rsi_only) == 1

    # Second call adds MA20 data: should write ma20_break only (RSI is
    # already recorded today, expiry still inert).
    second = apply_invalidations(
        beliefs_path, adjustments_path,
        today=today,
        market_snapshot={"NVDA": MarketIndicators("NVDA", price=118.0, ma20=120.0, rsi_14=80)},
        fsync=False,
    )
    assert len(second) == 1

    rows = [
        json.loads(line)
        for line in adjustments_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    reasons_by_count: dict[str, int] = {}
    for r in rows:
        reasons_by_count[r["reason"]] = reasons_by_count.get(r["reason"], 0) + 1
    assert reasons_by_count == {
        "invalidation:rsi_overheat": 1,
        "invalidation:ma20_break": 1,
    }


def test_apply_does_not_mutate_beliefs_file(tmp_path: Path) -> None:
    """agent_beliefs.json is read-only from this module's POV (plan R3)."""
    beliefs_path = _v2_beliefs_file(tmp_path, [_belief()])
    snapshot = beliefs_path.read_text(encoding="utf-8")
    apply_invalidations(
        beliefs_path,
        tmp_path / "out.jsonl",
        today=date(2026, 7, 1),
        market_snapshot={},
        fsync=False,
    )
    assert beliefs_path.read_text(encoding="utf-8") == snapshot


# ---------------------------------------------------------------------------
# make_apply — DI wrapper for production wiring
# ---------------------------------------------------------------------------


def test_make_apply_injects_date_and_snapshot(tmp_path: Path) -> None:
    seen_tickers: list[set[str]] = []

    def fixed_today() -> date:
        return date(2026, 7, 1)

    def snapshot(tickers):
        seen = set(tickers)
        seen_tickers.append(seen)
        return {
            "NVDA": MarketIndicators("NVDA", price=80.0, ma20=120.0),
        }

    runner = make_apply(today_fn=fixed_today, snapshot_fn=snapshot)
    beliefs_path = _v2_beliefs_file(
        tmp_path,
        [_belief(id="aaa", ticker="NVDA"), _belief(id="bbb", ticker="9984.T")],
    )
    row_ids = runner(beliefs_path, tmp_path / "out.jsonl", fsync=False)
    # snapshot was asked for both tickers from the beliefs file.
    assert seen_tickers == [{"NVDA", "9984.T"}]
    # Both beliefs expired by fixed_today; NVDA also broke MA20.
    assert len(row_ids) == 3


# ---------------------------------------------------------------------------
# Registry stability
# ---------------------------------------------------------------------------


def test_rules_registry_is_locked() -> None:
    """Adding a fourth rule should be a deliberate change; this test
    forces the author to update both the registry and the test."""
    assert RULES == ("expired", "rsi_overheat", "ma20_break")


def test_dataclass_invalidationadjustment_is_frozen() -> None:
    """Audit-friendly: an adjustment object cannot be silently rewritten."""
    adj = InvalidationAdjustment(
        belief_id="x", ticker="y", delta=-1, reason="r", evidence={}
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        adj.delta = 5  # type: ignore[misc]

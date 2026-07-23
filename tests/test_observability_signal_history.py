"""Tests for almanac.observability.signal_history.

The extension is purely additive (plan §5 step 3, R18 legacy compat).
Coverage pins down:

- ``apply_legacy_defaults`` is non-destructive and idempotent.
- All five v2 fields land with the correct defaults.
- ``read_history`` returns ``[]`` for missing files and rejects non-list
  payloads (defensive — surface the corruption rather than silently
  yielding garbage).
- ``make_record`` validates enums, rejects ``legacy`` status (legacy is
  for back-fill, never for new emission).
- Unicode strategy names round-trip (real production uses Japanese
  strings such as ``"モメンタム"`` / ``"逆張り"``).
- Real ``signal_history.json`` in the worktree is parseable end-to-end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.signal_history import (  # noqa: E402
    EXTENDED_FIELDS,
    LEGACY_HYPOTHESIS_TYPE,
    apply_legacy_defaults,
    make_record,
    read_history,
)
from almanac.observability.status import CandidateStatus, ExecutionState  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _legacy_record() -> dict:
    """A realistic v1 record taken from the production schema."""
    return {
        "date": "2026-05-20",
        "ticker": "NVDA",
        "strategy": "モメンタム",  # Japanese is the norm in production
        "signal": "buy",
        "confidence": 0.72,
        "reason": "RSI<30 + volume>1.5x",
        "price_at_signal": 118.4,
        "rsi": 28.0,
        "volume_ratio": 1.65,
        "mom_5d": -0.04,
        "debate": None,
        "regime": "bull",
        "outcome_5d": None,
        "outcome_10d": None,
    }


# ---------------------------------------------------------------------------
# apply_legacy_defaults
# ---------------------------------------------------------------------------


def test_legacy_defaults_inject_all_five_fields() -> None:
    out = apply_legacy_defaults(_legacy_record())
    for field in EXTENDED_FIELDS:
        assert field in out, f"v2 field {field!r} missing after defaults"


def test_legacy_defaults_use_legacy_sentinels() -> None:
    out = apply_legacy_defaults(_legacy_record())
    assert out["hypothesis_id"] is None
    assert out["hypothesis_type"] == LEGACY_HYPOTHESIS_TYPE
    assert out["horizon_days"] is None
    assert out["candidate_status"] == CandidateStatus.legacy.value
    assert out["execution_state"] == ExecutionState.legacy.value


def test_legacy_defaults_does_not_mutate_input() -> None:
    rec = _legacy_record()
    snapshot = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    apply_legacy_defaults(rec)
    assert json.dumps(rec, sort_keys=True, ensure_ascii=False) == snapshot


def test_legacy_defaults_preserves_existing_extended_fields() -> None:
    """A record that already has extended fields keeps them verbatim."""
    rec = _legacy_record() | {
        "hypothesis_id": "sha256:abc",
        "hypothesis_type": "earnings_revision_pullback",
        "horizon_days": 10,
        "candidate_status": CandidateStatus.adopted.value,
        "execution_state": ExecutionState.executed.value,
    }
    out = apply_legacy_defaults(rec)
    assert out["hypothesis_id"] == "sha256:abc"
    assert out["hypothesis_type"] == "earnings_revision_pullback"
    assert out["horizon_days"] == 10
    assert out["candidate_status"] == "adopted"
    assert out["execution_state"] == "executed"


def test_legacy_defaults_keeps_explicit_none_values() -> None:
    """Caller may have set ``hypothesis_id=None`` deliberately. We must
    not overwrite it with the same default (idempotency)."""
    rec = _legacy_record() | {"hypothesis_id": None}
    out = apply_legacy_defaults(rec)
    assert out["hypothesis_id"] is None
    # setdefault should also have populated the rest.
    assert out["candidate_status"] == CandidateStatus.legacy.value


def test_legacy_defaults_is_idempotent() -> None:
    once = apply_legacy_defaults(_legacy_record())
    twice = apply_legacy_defaults(once)
    assert once == twice


def test_legacy_defaults_preserves_v1_fields_exactly() -> None:
    """Round 8 #5 / R18 legacy compat — no v1 field may change shape."""
    rec = _legacy_record()
    out = apply_legacy_defaults(rec)
    for k, v in rec.items():
        assert out[k] == v


# ---------------------------------------------------------------------------
# read_history
# ---------------------------------------------------------------------------


def test_read_history_returns_empty_for_missing_file(tmp_path: Path) -> None:
    """Mirrors signal_tracker.load_history() behaviour."""
    assert read_history(tmp_path / "nope.json") == []


def test_read_history_decorates_every_record(tmp_path: Path) -> None:
    p = tmp_path / "signal_history.json"
    p.write_text(
        json.dumps([_legacy_record(), _legacy_record()], ensure_ascii=False)
    )
    history = read_history(p)
    assert len(history) == 2
    for rec in history:
        for field in EXTENDED_FIELDS:
            assert field in rec


def test_read_history_rejects_non_list_payload(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"oops": "this is a dict"}))
    with pytest.raises(ValueError, match="expected a JSON list"):
        read_history(p)


def test_read_history_round_trips_japanese_strategy_names(tmp_path: Path) -> None:
    p = tmp_path / "signal_history.json"
    p.write_text(
        json.dumps(
            [
                _legacy_record() | {"strategy": "イベントドリブン後"},
                _legacy_record() | {"strategy": "逆張り"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    history = read_history(p)
    assert {r["strategy"] for r in history} == {"イベントドリブン後", "逆張り"}


# ---------------------------------------------------------------------------
# make_record
# ---------------------------------------------------------------------------


def test_make_record_accepts_enum_or_string_for_status() -> None:
    via_enum = make_record(
        date="2026-05-24",
        ticker="NVDA",
        strategy="モメンタム",
        signal="buy",
        confidence=0.72,
        reason="r",
        price_at_signal=120.0,
        hypothesis_id="sha256:abc",
        hypothesis_type="earnings_revision_pullback",
        horizon_days=10,
        candidate_status=CandidateStatus.adopted,
        execution_state=ExecutionState.executed,
    )
    via_string = make_record(
        date="2026-05-24",
        ticker="NVDA",
        strategy="モメンタム",
        signal="buy",
        confidence=0.72,
        reason="r",
        price_at_signal=120.0,
        hypothesis_id="sha256:abc",
        hypothesis_type="earnings_revision_pullback",
        horizon_days=10,
        candidate_status="adopted",
        execution_state="executed",
    )
    # Both forms must produce identical wire format.
    assert via_enum["candidate_status"] == via_string["candidate_status"] == "adopted"
    assert via_enum["execution_state"] == via_string["execution_state"] == "executed"


def test_make_record_defaults_outcomes_to_none() -> None:
    """signal_tracker.update_outcomes fills these later — must start null."""
    rec = make_record(
        date="2026-05-24",
        ticker="NVDA",
        strategy="モメンタム",
        signal="buy",
        confidence=0.5,
        reason="r",
        price_at_signal=1.0,
        hypothesis_id="sha256:abc",
    )
    assert rec["outcome_5d"] is None
    assert rec["outcome_10d"] is None


def test_make_record_includes_all_v1_fields() -> None:
    rec = make_record(
        date="2026-05-24",
        ticker="NVDA",
        strategy="モメンタム",
        signal="buy",
        confidence=0.5,
        reason="r",
        price_at_signal=1.0,
    )
    for field in (
        "date", "ticker", "strategy", "signal", "confidence", "reason",
        "price_at_signal", "rsi", "volume_ratio", "mom_5d", "debate",
        "regime", "outcome_5d", "outcome_10d",
    ):
        assert field in rec


def test_make_record_rejects_candidate_status_legacy() -> None:
    """Legacy status is for back-fill only, never new emission."""
    with pytest.raises(ValueError, match="reserved for pre-migration"):
        make_record(
            date="2026-05-24",
            ticker="NVDA",
            strategy="モメンタム",
            signal="buy",
            confidence=0.5,
            reason="r",
            price_at_signal=1.0,
            candidate_status=CandidateStatus.legacy,
        )


def test_make_record_rejects_execution_state_legacy() -> None:
    with pytest.raises(ValueError, match="reserved for pre-migration"):
        make_record(
            date="2026-05-24",
            ticker="NVDA",
            strategy="モメンタム",
            signal="buy",
            confidence=0.5,
            reason="r",
            price_at_signal=1.0,
            execution_state=ExecutionState.legacy,
        )


def test_make_record_rejects_invalid_status_string() -> None:
    with pytest.raises(ValueError, match="candidate_status='nonsense'"):
        make_record(
            date="2026-05-24",
            ticker="NVDA",
            strategy="モメンタム",
            signal="buy",
            confidence=0.5,
            reason="r",
            price_at_signal=1.0,
            candidate_status="nonsense",
        )


def test_make_record_rejects_wrong_type_for_status() -> None:
    with pytest.raises(TypeError, match="candidate_status must be"):
        make_record(
            date="2026-05-24",
            ticker="NVDA",
            strategy="モメンタム",
            signal="buy",
            confidence=0.5,
            reason="r",
            price_at_signal=1.0,
            candidate_status=42,  # type: ignore[arg-type]
        )


def test_make_record_default_status_is_generated_not_ordered() -> None:
    """A brand-new record is ``generated`` on the decision axis and
    ``not_ordered`` on the execution axis."""
    rec = make_record(
        date="2026-05-24",
        ticker="NVDA",
        strategy="モメンタム",
        signal="buy",
        confidence=0.5,
        reason="r",
        price_at_signal=1.0,
    )
    assert rec["candidate_status"] == "generated"
    assert rec["execution_state"] == "not_ordered"


# ---------------------------------------------------------------------------
# Production file shape compatibility
# ---------------------------------------------------------------------------


_PRODUCTION_FILE = _REPO_ROOT / "signal_history.json"


@pytest.mark.skipif(
    not _PRODUCTION_FILE.exists(),
    reason="production signal_history.json not in this checkout",
)
def test_real_production_file_decorates_cleanly() -> None:
    """Round-trip the actual file (read-only, never written).

    The production file may be intentionally empty immediately after a
    harness reset; the compatibility invariant is that it remains parseable.
    """
    history = read_history(_PRODUCTION_FILE)
    for rec in history:
        # Every legacy record must now carry the five v2 fields.
        for field in EXTENDED_FIELDS:
            assert field in rec
        # Legacy sentinels must be set since the file pre-dates the migration.
        assert rec["candidate_status"] == CandidateStatus.legacy.value
        assert rec["execution_state"] == ExecutionState.legacy.value
        assert rec["hypothesis_type"] == LEGACY_HYPOTHESIS_TYPE

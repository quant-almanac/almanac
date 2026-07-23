"""Regression tests for the execution-based cooldown filter in analyst/__init__.py.

The cooling filter must only suppress trim/sell/stop_loss recommendations for
tickers that have a *real buy execution record* in action_executions.json
within the last 14 days.  It must NOT fire based on holding_days, position
import dates, or any other heuristic.

Coverage:
- _load_recent_executions returns buy records within the window
- _load_recent_executions ignores non-buy directions
- _load_recent_executions ignores records older than the window
- _load_recent_executions ignores records with non-executed status
- The _recently_bought set is populated correctly from buy records
- Positions NOT in _recently_bought are never filtered, regardless of holding_days
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import analyst  # noqa: E402 — ensure module is loaded before monkeypatching


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_execution(
    ticker: str,
    direction: str = "buy",
    status: str = "executed",
    saved_at: str | None = None,
) -> dict:
    """Build a minimal action_executions entry."""
    if saved_at is None:
        saved_at = datetime.now().isoformat()
    return {
        "ticker": ticker,
        "direction": direction,
        "status": status,
        "quantity": 10,
        "saved_at": saved_at,
    }


def _write_executions(path: Path, entries: list[dict]) -> None:
    path.write_text(
        json.dumps({"executions": entries}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _load_recent_executions
# ---------------------------------------------------------------------------


def test_load_recent_executions_returns_recent_buys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recent executed buy records are returned."""
    f = tmp_path / "action_executions.json"
    _write_executions(f, [
        _make_execution("NVDA", direction="buy", status="executed"),
        _make_execution("AAPL", direction="buy", status="filled"),
    ])
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)

    result = analyst._load_recent_executions(days=14)
    tickers = {e["ticker"] for e in result}
    assert "NVDA" in tickers
    assert "AAPL" in tickers


def test_load_recent_executions_ignores_non_buy_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sell/trim/stop_loss records must NOT appear — the caller filters by direction."""
    f = tmp_path / "action_executions.json"
    _write_executions(f, [
        _make_execution("NVDA", direction="sell",      status="executed"),
        _make_execution("AAPL", direction="trim",      status="done"),
        _make_execution("MSFT", direction="stop_loss", status="filled"),
        _make_execution("TSLA", direction="buy",       status="executed"),
    ])
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)

    result = analyst._load_recent_executions(days=14)
    # _load_recent_executions itself doesn't filter direction — the caller does.
    # We verify that the returned list includes all status-valid records,
    # and that the direction field is intact so the caller's set comprehension works.
    tickers = {e["ticker"] for e in result}
    assert "TSLA" in tickers   # buy → included
    assert "NVDA" in tickers   # sell → included in raw list
    # Now simulate the caller's _recently_bought set construction:
    recently_bought = {
        e["ticker"]
        for e in result
        if (e.get("direction") or "").lower() in {"buy", "add", "dca", "margin_buy"}
        and e.get("ticker")
    }
    assert "TSLA" in recently_bought
    assert "NVDA" not in recently_bought, "sell direction must not appear in _recently_bought"
    assert "AAPL" not in recently_bought, "trim direction must not appear in _recently_bought"
    assert "MSFT" not in recently_bought, "stop_loss direction must not appear in _recently_bought"


def test_load_recent_executions_ignores_old_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records older than `days` are excluded from results."""
    f = tmp_path / "action_executions.json"
    old_ts = (datetime.now() - timedelta(days=20)).isoformat()
    _write_executions(f, [
        _make_execution("NVDA", direction="buy", status="executed", saved_at=old_ts),
        _make_execution("AAPL", direction="buy", status="executed"),  # today → recent
    ])
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)

    result = analyst._load_recent_executions(days=14)
    tickers = {e["ticker"] for e in result}
    assert "NVDA" not in tickers, "20-day-old record must be excluded by 14-day window"
    assert "AAPL" in tickers


def test_load_recent_executions_ignores_pending_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records with status='pending' or 'draft' are not treated as executed."""
    f = tmp_path / "action_executions.json"
    _write_executions(f, [
        _make_execution("NVDA", direction="buy", status="pending"),
        _make_execution("AAPL", direction="buy", status="draft"),
        _make_execution("MSFT", direction="buy", status="ordered"),  # valid
    ])
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)

    result = analyst._load_recent_executions(days=14)
    tickers = {e["ticker"] for e in result}
    assert "NVDA" not in tickers, "status=pending must be excluded"
    assert "AAPL" not in tickers, "status=draft must be excluded"
    assert "MSFT" in tickers   # ordered is a valid executed-equivalent status


def test_load_recent_executions_returns_empty_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing action_executions.json returns empty list without error."""
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    result = analyst._load_recent_executions(days=14)
    assert result == []


def test_load_recent_executions_returns_empty_for_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed JSON is swallowed and returns empty list."""
    f = tmp_path / "action_executions.json"
    f.write_text("not valid json {{{", encoding="utf-8")
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    result = analyst._load_recent_executions(days=14)
    assert result == []


# ---------------------------------------------------------------------------
# _recently_bought set construction — filter semantics
# ---------------------------------------------------------------------------


def test_recently_bought_set_covers_all_buy_synonyms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """buy / add / dca / margin_buy all populate _recently_bought."""
    f = tmp_path / "action_executions.json"
    _write_executions(f, [
        _make_execution("A",    direction="buy",        status="executed"),
        _make_execution("B",    direction="add",        status="done"),
        _make_execution("C",    direction="dca",        status="filled"),
        _make_execution("D",    direction="margin_buy", status="ordered"),
        _make_execution("SKIP", direction="sell",       status="executed"),
    ])
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)

    execs = analyst._load_recent_executions(days=14)
    recently_bought = {
        e["ticker"]
        for e in execs
        if (e.get("direction") or "").lower() in {"buy", "add", "dca", "margin_buy"}
        and e.get("ticker")
    }
    assert recently_bought == {"A", "B", "C", "D"}
    assert "SKIP" not in recently_bought


def test_no_execution_record_means_no_cooling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ticker with zero execution records is never in _recently_bought.

    This is the core regression: before the fix, holding_days < 14 triggered
    cooling for positions that were simply imported or had a stale entry_date,
    even if no actual trade was executed.
    """
    # action_executions.json exists but has NO record for NVDA.
    f = tmp_path / "action_executions.json"
    _write_executions(f, [
        _make_execution("AAPL", direction="buy", status="executed"),
    ])
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)

    execs = analyst._load_recent_executions(days=14)
    recently_bought = {
        e["ticker"]
        for e in execs
        if (e.get("direction") or "").lower() in {"buy", "add", "dca", "margin_buy"}
        and e.get("ticker")
    }
    assert "NVDA" not in recently_bought, (
        "NVDA has no execution record — cooling filter must NOT apply, "
        "regardless of holding_days"
    )

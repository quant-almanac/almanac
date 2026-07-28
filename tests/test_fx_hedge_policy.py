from datetime import datetime
from zoneinfo import ZoneInfo

import fx_hedge_policy as policy

JST = ZoneInfo("Asia/Tokyo")


def _stock():
    return {
        "key": "AAPL_GENERAL",
        "ticker": "AAPL",
        "owner": "husband",
        "broker": "rakuten",
        "account": "一般",
        "asset_type": "stock",
        "currency": "USD",
        "value_jpy": 1_000_000,
    }


def _actual(as_of="2026-07-28T08:00:00+09:00"):
    return {
        "observed_actual_hedge_notional_jpy": 50_000,
        "broker_source": "broker_statement",
        "source_as_of": as_of,
        "reconciliation_snapshot_hash": "sha256:actual",
    }


def test_unknown_actual_notional_fails_closed_without_shadow_write(monkeypatch):
    called = []
    monkeypatch.setattr(policy, "run_hedge_shadow", lambda *a, **k: called.append(True))

    result = policy.evaluate_portfolio_hedge_shadow(
        [_stock()],
        regime="neutral",
        vix=20,
        usdjpy=150,
        actual_state={},
        now=datetime(2026, 7, 28, 9, 0, tzinfo=JST),
    )

    assert result["status"] == "review"
    assert result["state_saved"] is False
    assert called == []


def test_unknown_instrument_kind_fails_closed(monkeypatch):
    position = {**_stock(), "ticker": "UNKNOWN", "asset_type": None}
    called = []
    monkeypatch.setattr(policy, "run_hedge_shadow", lambda *a, **k: called.append(True))

    result = policy.evaluate_portfolio_hedge_shadow(
        [position],
        regime="neutral",
        vix=20,
        usdjpy=150,
        actual_state=_actual(),
        now=datetime(2026, 7, 28, 9, 0, tzinfo=JST),
    )

    assert "economic_exposure_unknown:UNKNOWN" in result["issues"]
    assert result["state_saved"] is False
    assert called == []


def test_fully_evidenced_shadow_records_three_notional_quantities(monkeypatch):
    captured = {}

    def _shadow(*args, **kwargs):
        captured.update(kwargs)
        return {"target_hedge_ratio": 0.20, "mode": "shadow"}

    monkeypatch.setattr(policy, "run_hedge_shadow", _shadow)
    result = policy.evaluate_portfolio_hedge_shadow(
        [_stock()],
        regime="neutral",
        vix=20,
        usdjpy=150,
        actual_state=_actual(),
        now=datetime(2026, 7, 28, 9, 0, tzinfo=JST),
        decision_snapshot_hash="snapshot-123",
    )

    assert result["status"] == "shadow_recorded"
    assert result["state_saved"] is True
    assert result["observed_actual_hedge_notional_jpy"] == 50_000
    assert result["target_hedge_notional_jpy"] == 200_000
    assert result["shadow_proposed_hedge_notional_jpy"] == 200_000
    assert result["proposed_delta_notional_jpy"] == 150_000
    assert captured["snapshot_hash"] == "snapshot-123"

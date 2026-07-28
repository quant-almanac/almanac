from datetime import datetime
from zoneinfo import ZoneInfo

import fx_actual_hedge_state as actual

JST = ZoneInfo("Asia/Tokyo")


def _snapshot(broker, *, complete=True, as_of="2026-07-28T08:00:00+09:00", positions=None):
    return {
        "broker": broker, "complete": complete, "source_as_of": as_of,
        "reconciliation_snapshot_hash": f"sha256:{broker}",
        "positions": positions or [],
    }


def test_confirmed_zero_requires_every_broker_snapshot():
    result = actual.build_actual_hedge_state(
        [_snapshot("rakuten")], required_brokers=["rakuten", "sbi"],
        now=datetime(2026, 7, 28, 9, 0, tzinfo=JST),
    )
    assert result["status"] == "review"
    assert result["observed_actual_hedge_notional_jpy"] is None
    assert "broker_snapshot_missing:sbi" in result["issues"]


def test_fresh_complete_snapshots_can_evidence_zero():
    result = actual.build_actual_hedge_state(
        [_snapshot("rakuten"), _snapshot("sbi")],
        required_brokers=["rakuten", "sbi"],
        now=datetime(2026, 7, 28, 9, 0, tzinfo=JST),
    )
    assert result["status"] == "eligible"
    assert result["observed_actual_hedge_notional_jpy"] == 0


def test_overlay_notional_is_summed_but_embedded_hedge_etf_is_not():
    result = actual.build_actual_hedge_state(
        [_snapshot("rakuten", positions=[
            {"ticker": "2634.T", "vehicle_type": "embedded_hedge_etf",
             "hedge_notional_jpy": 1_000_000},
            {"ticker": "6J", "vehicle_type": "currency_future",
             "hedge_notional_jpy": 300_000},
        ])],
        required_brokers=["rakuten"],
        now=datetime(2026, 7, 28, 9, 0, tzinfo=JST),
    )
    assert result["status"] == "eligible"
    assert result["observed_actual_hedge_notional_jpy"] == 300_000


def test_stale_or_partial_snapshot_cannot_overwrite_state(tmp_path):
    result = actual.build_actual_hedge_state(
        [_snapshot("rakuten", complete=False, as_of="2026-07-01T08:00:00+09:00")],
        required_brokers=["rakuten"],
        now=datetime(2026, 7, 28, 9, 0, tzinfo=JST),
    )
    output = tmp_path / "fx_actual_hedge_state.json"
    written = actual.write_actual_hedge_state(result, path=output)
    assert written["written"] is False
    assert not output.exists()

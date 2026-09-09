"""Part E-6: earnings_proximity_manager のビジネス日計算 & format 出力。"""
from __future__ import annotations

import importlib
import json
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest


def test_business_days_future():
    m = importlib.import_module("earnings_proximity_manager")
    # 来週の同曜日 → 5 営業日
    tgt = date.today() + timedelta(days=7)
    n = m._business_days_until(tgt)
    assert 4 <= n <= 6


def test_business_days_past():
    m = importlib.import_module("earnings_proximity_manager")
    n = m._business_days_until(date.today() - timedelta(days=1))
    assert n == -1


def test_thresholds():
    m = importlib.import_module("earnings_proximity_manager")
    assert 0 < m.DAMAGE_PCT_THRESHOLD < 0.1
    assert 0 < m.IMPL_MOVE_FUDGE < 1.0
    assert 0 < m.BEAT_RATE_FORCE_TRIM <= 1.0


def test_scheduled_scan_rechecks_current_snapshot_inside_shared_lock(monkeypatch):
    m = importlib.import_module("earnings_proximity_manager")
    import utils

    lock_state = {"held": False}
    expected = {"schema_version": m.OUTPUT_SCHEMA_VERSION, "suggestions": []}

    @contextmanager
    def fake_lock(name, *, timeout):
        assert name == "earnings_proximity"
        assert timeout == 300.0
        lock_state["held"] = True
        try:
            yield
        finally:
            lock_state["held"] = False

    def load_current():
        assert lock_state["held"] is True
        return expected

    monkeypatch.setattr(utils, "process_lock", fake_lock)
    monkeypatch.setattr(m, "_load_current_snapshot", load_current)
    monkeypatch.setattr(
        m,
        "_scan_once",
        lambda **_kwargs: pytest.fail("duplicate network scan must be skipped"),
    )

    assert m.scan(dry_run=False, reuse_current=True) is expected
    assert lock_state["held"] is False


def test_scan_never_falls_back_to_non_atomic_output_write(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    import utils

    output = tmp_path / "earnings_hedge_suggestions.json"
    output.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "_load_holdings", lambda: [])
    monkeypatch.setattr(
        m,
        "_portfolio_total_observation",
        lambda: {"value_jpy": 30_000_000.0, "source": "guard_state", "as_of": "2026-09-07T06:00:00"},
    )
    monkeypatch.setattr(
        m,
        "_fx_rate_observation",
        lambda: {"rate": 150.0, "source": "live", "observed_at": time.time()},
    )

    def fail_atomic_write(_path, _payload):
        raise OSError("simulated atomic persistence failure")

    monkeypatch.setattr(utils, "atomic_write_json", fail_atomic_write)

    with pytest.raises(OSError, match="atomic persistence failure"):
        m._scan_once(dry_run=False)
    assert output.read_text(encoding="utf-8") == "sentinel"


def _current_snapshot(m, holdings, *, result_rows=None):
    rows = result_rows if result_rows is not None else [
        {"ticker": row["ticker"], "reason": "no_earnings_date"}
        for row in holdings
    ]
    # 実時計の少し前・かつ必ず today と同じ暦日にする。固定 "06:15:00" を
    # date.today() と組み合わせると、実行時刻が 06:15 より前（深夜0時台）
    # だと未来日時になり S2 の未来拒否チェックに引っかかる。単純な「5分前」も
    # 日境界をまたぐと today と一致しなくなる（いずれも 00:0x JST 実行で
    # 実際に再現・自己レビューで発見）。
    _now = datetime.now()
    _candidate = _now - timedelta(minutes=5)
    if _candidate.date() != _now.date():
        _candidate = datetime.combine(_now.date(), datetime.min.time())
    generated_at = _candidate.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "schema_version": m.OUTPUT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "holdings_scanned": len(holdings),
        "holdings_snapshot_sha256": m._holdings_snapshot_sha256(holdings),
        "portfolio_jpy": 30_000_000.0,
        "portfolio_jpy_source": "guard_state",
        "portfolio_jpy_as_of": time.time(),
        "usd_jpy": 150.0,
        "fx_rate_source": "live",
        "fx_rate_usdjpy_as_of": time.time(),
        "suggestions": [],
        "skipped": rows,
    }


def test_same_day_snapshot_is_invalidated_by_ticker_or_share_change(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    overrides = tmp_path / "earnings_calendar_overrides.json"
    overrides.write_text('{"overrides": {}}', encoding="utf-8")
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "EARNINGS_OVERRIDES", overrides)

    holdings = [{"ticker": "SYNTH_A", "shares": 10.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    output.write_text(json.dumps(_current_snapshot(m, holdings)), encoding="utf-8")
    assert m.snapshot_is_current() is True

    # Same count is not enough: both a replacement holding and a sizing change
    # require a fresh earnings/damage calculation.
    monkeypatch.setattr(
        m, "_load_holdings",
        lambda: [{"ticker": "SYNTH_B", "shares": 10.0, "currency": "USD"}],
    )
    assert m.snapshot_is_current() is False
    monkeypatch.setattr(
        m, "_load_holdings",
        lambda: [{"ticker": "SYNTH_A", "shares": 11.0, "currency": "USD"}],
    )
    assert m.snapshot_is_current() is False


def test_snapshot_rejects_missing_or_duplicate_holding_result_rows(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    overrides = tmp_path / "earnings_calendar_overrides.json"
    overrides.write_text('{"overrides": {}}', encoding="utf-8")
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "EARNINGS_OVERRIDES", overrides)
    holdings = [
        {"ticker": "SYNTH_A", "shares": 10.0, "currency": "USD"},
        {"ticker": "SYNTH_B", "shares": 5.0, "currency": "USD"},
    ]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)

    missing = _current_snapshot(
        m, holdings,
        result_rows=[{"ticker": "SYNTH_A", "reason": "no_earnings_date"}],
    )
    output.write_text(json.dumps(missing), encoding="utf-8")
    assert m.snapshot_is_current() is False

    duplicate = _current_snapshot(
        m, holdings,
        result_rows=[
            {"ticker": "SYNTH_A", "reason": "no_earnings_date"},
            {"ticker": "SYNTH_A", "reason": "no_option_chain"},
        ],
    )
    output.write_text(json.dumps(duplicate), encoding="utf-8")
    assert m.snapshot_is_current() is False


def test_scan_persists_exact_holdings_fingerprint(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    holdings = [
        {"ticker": "SYNTH_A", "shares": 10.0, "currency": "USD"},
        {"ticker": "SYNTH_B", "shares": 5.0, "currency": "USD"},
    ]
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    monkeypatch.setattr(
        m,
        "_portfolio_total_observation",
        lambda: {"value_jpy": 30_000_000.0, "source": "guard_state", "as_of": "2026-09-07T06:00:00"},
    )
    monkeypatch.setattr(
        m,
        "_fx_rate_observation",
        lambda: {"rate": 150.0, "source": "cache", "observed_at": time.time()},
    )
    monkeypatch.setattr(m, "_next_earnings_with_source", lambda _ticker: None)

    out = m._scan_once(dry_run=False)

    assert out["holdings_scanned"] == 2
    assert out["holdings_snapshot_sha256"] == m._holdings_snapshot_sha256(holdings)
    assert out["portfolio_jpy_source"] == "guard_state"
    assert out["fx_rate_source"] == "cache"
    assert {row["ticker"] for row in out["skipped"]} == {"SYNTH_A", "SYNTH_B"}


def test_prompt_suppresses_snapshot_after_holdings_change(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    output.write_text(json.dumps({
        "suggestions": [{
            "ticker": "SYNTH_A",
            "business_days": 2,
            "earnings_date": date.today().isoformat(),
            "implied_move_pct": 5.0,
            "damage_pct": 2.0,
            "recommended_action": "trim_50pct",
        }],
        "skipped": [],
    }), encoding="utf-8")
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "snapshot_is_current", lambda: False)

    assert m.format_for_prompt() == ""


def test_corrupt_holdings_never_publish_an_empty_current_snapshot(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    holdings = tmp_path / "holdings.json"
    output = tmp_path / "earnings_hedge_suggestions.json"
    holdings.write_text("{not-json", encoding="utf-8")
    output.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(m, "HOLDINGS", holdings)
    monkeypatch.setattr(m, "OUTPUT", output)

    with pytest.raises(ValueError, match="holdings source is unreadable"):
        m._scan_once(dry_run=False)

    assert output.read_text(encoding="utf-8") == "sentinel"


def test_portfolio_total_uses_recent_guard_then_recent_formal_analysis(monkeypatch, tmp_path):
    """guard_state の timestamp key は portfolio_value_as_of（last_updated ではない）。

    last_updated は behavioral_guard.save_state が全 save で更新するため、値を
    再評価しない writer でも進み得る。portfolio_value_as_of は実際に再評価した
    writer だけが書く（2026-09 レビュー S1）。より詳細な新旧逆転ケースは
    tests/test_earnings_portfolio_freshness_jst.py を参照。
    """
    m = importlib.import_module("earnings_proximity_manager")
    guard = tmp_path / "guard_state.json"
    analysis = tmp_path / "ai_portfolio_analysis.json"
    now = datetime(2026, 9, 7, 6, 15, tzinfo=timezone.utc)
    guard.write_text(json.dumps({
        "portfolio_value": 31_000_000,
        "portfolio_value_as_of": "2026-09-07T05:15:00+00:00",
    }), encoding="utf-8")
    analysis.write_text(json.dumps({
        "portfolio_total": 30_000_000,
        "as_of": "2026-09-07T04:15:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setattr(m, "GUARD_STATE", guard)
    monkeypatch.setattr(m, "ANALYSIS", analysis)

    assert m._portfolio_total_observation(now=now) == {
        "value_jpy": 31_000_000.0,
        "source": "guard_state",
        "as_of": "2026-09-07T05:15:00+00:00",
    }

    guard.write_text(json.dumps({
        "portfolio_value": 31_000_000,
        "portfolio_value_as_of": "2026-09-05T05:15:00+00:00",
    }), encoding="utf-8")
    assert m._portfolio_total_observation(now=now) == {
        "value_jpy": 30_000_000.0,
        "source": "formal_analysis",
        "as_of": "2026-09-07T04:15:00+00:00",
    }


def test_portfolio_total_never_uses_magic_fallback(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    guard = tmp_path / "guard_state.json"
    analysis = tmp_path / "ai_portfolio_analysis.json"
    guard.write_text('{"portfolio_value": "not-a-number"}', encoding="utf-8")
    analysis.write_text('{"portfolio_total": null}', encoding="utf-8")
    monkeypatch.setattr(m, "GUARD_STATE", guard)
    monkeypatch.setattr(m, "ANALYSIS", analysis)

    with pytest.raises(RuntimeError, match="current portfolio total is unavailable"):
        m._portfolio_total_observation(
            now=datetime(2026, 9, 7, 6, 15, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    ("observation", "message"),
    [
        ({"rate": 150.0, "source": "hardcoded", "observed_at": None}, "source is not usable"),
        ({
            "rate": 150.0,
            "source": "account_stale",
            "observed_at": datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp(),
        }, "FX observation is stale"),
        ({
            "rate": float("nan"),
            "source": "live",
            "observed_at": datetime(2026, 9, 7, 6, tzinfo=timezone.utc).timestamp(),
        }, "must be positive and finite"),
    ],
)
def test_fx_observation_rejects_unverifiable_inputs(monkeypatch, observation, message):
    m = importlib.import_module("earnings_proximity_manager")
    import utils

    monkeypatch.setattr(utils, "get_fx_rate_observation", lambda **_kwargs: observation)
    with pytest.raises(RuntimeError, match=message):
        m._fx_rate_observation(
            now=datetime(2026, 9, 7, 6, 15, tzinfo=timezone.utc),
        )


def test_snapshot_contract_requires_valuation_provenance(monkeypatch, tmp_path):
    m = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    overrides = tmp_path / "earnings_calendar_overrides.json"
    overrides.write_text('{"overrides": {}}', encoding="utf-8")
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    payload = _current_snapshot(m, holdings)
    payload.pop("fx_rate_source")
    output.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(m, "OUTPUT", output)
    monkeypatch.setattr(m, "EARNINGS_OVERRIDES", overrides)
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)

    assert m.snapshot_is_current() is False

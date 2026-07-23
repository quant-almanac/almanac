"""JPY cash contract + scenario_state freshness の回帰テスト。

A. JP 建て buy は JPY available cash(account.balance=cash_breakdown['jpy'])で clip し、
   USD 換算総現金(total_jpy / usd_jpy)を JP 買付余力に混ぜない。USD 建ては usd_jpy を使う。
   不足時は候補を消さず target 縮小 + deferred + currency_cash_sufficient=False を明示。
B. portfolio_analyst.py --force が scenario_playbook/engine より古い scenario_state を
   そのまま使わない(deterministic refresh)。
"""

import os
import json
import sys
import time
from datetime import datetime as real_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import drawdown_dca_engine as dca
import analyst


# ── A. JPY cash contract ─────────────────────────────────

def _by_ticker(buys):
    return {b["ticker"]: b for b in buys}


def test_dca_jp_buy_clips_to_jpy_available_not_total():
    """JP ETF(1489.T)の投入は JPY 残高で clip され、USD換算総現金は使わない。"""
    cb = {"jpy": 100000, "usd": 1000.0, "usd_jpy": 5_000_000, "total_jpy": 5_100_000}
    buys = dca._build_recommended_buys(
        "T1", cash_jpy=300000, target_tickers=["1489.T"],
        deploy_jpy=300000, cash_breakdown=cb,
    )
    bt = _by_ticker(buys)
    assert "1489.T" in bt, "候補を黙って消さない(不足でも残す)"
    b = bt["1489.T"]
    assert b["currency"] == "JPY"
    # JPY available=100000 で clip。total(5.1M)/usd_jpy(5M) を余力にしていない。
    assert b["target_jpy"] <= 100000, f"JPY残高で clip されるべき: {b['target_jpy']}"
    assert b["deferred_jpy"] > 0, "不足分は deferred で明示(silently drop しない)"
    assert b["currency_cash_sufficient"] is False
    assert b["requested_jpy"] == b["target_jpy"] + b["deferred_jpy"], "会計恒等式"


def test_dca_usd_buy_uses_usd_jpy_not_jpy():
    """USD 建て(SPY)の投入は USD換算現金(usd_jpy)を使い、JPY残高で誤 clip しない。"""
    cb = {"jpy": 100, "usd": 50000.0, "usd_jpy": 5_000_000, "total_jpy": 5_000_100}
    buys = dca._build_recommended_buys(
        "T1", cash_jpy=300000, target_tickers=["SPY"],
        deploy_jpy=300000, cash_breakdown=cb,
    )
    bt = _by_ticker(buys)
    assert "SPY" in bt
    b = bt["SPY"]
    assert b["currency"] == "USD"
    # USD avail(usd_jpy=5M) >> 300000 → 充足。jpy=100 で clip されていない証拠。
    assert b["currency_cash_sufficient"] is True
    assert b["target_jpy"] > 100, "JPY残高(100)で誤 clip していない"
    assert b["deferred_jpy"] == 0


# ── B. scenario_state freshness ──────────────────────────

def _touch(path: Path, mtime: float):
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_ensure_scenario_state_fresh_refreshes_when_stale(tmp_path):
    """playbook が state より新しい → evaluator が呼ばれ refresh(True)。"""
    now = time.time()
    _touch(tmp_path / "scenario_state.json", now - 100)      # 古い state
    _touch(tmp_path / "scenario_playbook.json", now)          # 新しい playbook
    called = []
    refreshed = analyst._ensure_scenario_state_fresh(
        base_dir=tmp_path, evaluator=lambda: called.append(1)
    )
    assert refreshed is True
    assert called == [1], "stale 時に evaluate_scenarios を呼ぶ"


def test_ensure_scenario_state_fresh_skips_when_current(tmp_path):
    """state が playbook より新しい → refresh しない(False, evaluator 未呼出)。"""
    now = time.time()
    _touch(tmp_path / "scenario_playbook.json", now - 100)    # 古い playbook
    _touch(tmp_path / "scenario_state.json", now)             # 新しい state
    called = []
    refreshed = analyst._ensure_scenario_state_fresh(
        base_dir=tmp_path, evaluator=lambda: called.append(1)
    )
    assert refreshed is False
    assert called == [], "fresh 時は再生成しない"


def test_ensure_scenario_state_fresh_refreshes_when_missing(tmp_path):
    """state 不在 → refresh(True)。"""
    now = time.time()
    _touch(tmp_path / "scenario_playbook.json", now)
    called = []
    refreshed = analyst._ensure_scenario_state_fresh(
        base_dir=tmp_path, evaluator=lambda: called.append(1)
    )
    assert refreshed is True
    assert called == [1]


# ── C. execution_plan freshness ──────────────────────────

def test_refresh_execution_plan_state_calls_generator_before_analysis(tmp_path, monkeypatch):
    monkeypatch.delenv("ALMANAC_SKIP_EXECUTION_PLAN_REFRESH", raising=False)
    monkeypatch.delenv("KAIROS_SKIP_EXECUTION_PLAN_REFRESH", raising=False)
    fixed_now = real_datetime(2026, 7, 10, 6, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    calls = []

    def _generate(**kwargs):
        calls.append(kwargs)
        return {
            "items": [{"plan_item_id": "p1"}],
            "consumption_summary": {
                "remaining_normal_jpy": 123000,
                "remaining_opportunity_jpy": 45000,
            },
        }

    result = analyst._refresh_execution_plan_state(
        base_dir=tmp_path,
        generator=_generate,
        now=fixed_now,
    )

    assert result == {
        "ok": True,
        "items": 1,
        "remaining_normal_jpy": 123000,
        "remaining_opportunity_jpy": 45000,
    }
    assert calls == [{"base_dir": tmp_path, "now": fixed_now, "write": True}]


def test_refresh_execution_plan_state_disables_stale_plan_on_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("ALMANAC_SKIP_EXECUTION_PLAN_REFRESH", raising=False)
    monkeypatch.delenv("KAIROS_SKIP_EXECUTION_PLAN_REFRESH", raising=False)
    fixed_now = real_datetime(2026, 7, 10, 6, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

    def _raise(**kwargs):
        raise RuntimeError("planner broke")

    result = analyst._refresh_execution_plan_state(
        base_dir=tmp_path,
        generator=_raise,
        now=fixed_now,
    )

    assert result["ok"] is False
    state = json.loads((tmp_path / "execution_plan_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "disabled"
    assert state["items"] == []
    assert state["horizon"] == {
        "month": "2026-07",
        "week_start": "2026-07-06",
        "week_end": "2026-07-12",
    }
    assert "execution_plan_refresh_failed: RuntimeError: planner broke" in state["warnings"]


# ── C. data freshness timezone handling ───────────────────

def test_data_freshness_converts_utc_cached_at_to_jst_age(tmp_path, monkeypatch):
    """UTC cached_at を naive 化せず、JST基準の実経過時間で鮮度判定する。"""
    (tmp_path / "technical_state.json").write_text(
        '{"cached_at": "2026-07-01T11:36:00+00:00"}',
        encoding="utf-8",
    )

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            base = real_datetime(2026, 7, 1, 21, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
            if tz is not None:
                return base.astimezone(tz)
            return base.replace(tzinfo=None)

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(analyst, "datetime", FixedDateTime)

    freshness = analyst._compute_data_freshness()

    assert "✅ FRESH technical_state(RSI/MACD)" in freshness
    assert "VERY_STALE technical_state(RSI/MACD)" not in freshness


def test_data_freshness_uses_morning_screen_results_when_fresher(tmp_path, monkeypatch):
    """朝AIでは前日夕方の通常screen_resultsよりmorning出力を鮮度代表にする。"""
    (tmp_path / "screen_results.json").write_text(
        '{"timestamp": "2026-07-01 18:00"}',
        encoding="utf-8",
    )
    (tmp_path / "screen_results_morning.json").write_text(
        '{"timestamp": "2026-07-02 06:04"}',
        encoding="utf-8",
    )

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            base = real_datetime(2026, 7, 2, 8, 20, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
            if tz is not None:
                return base.astimezone(tz)
            return base.replace(tzinfo=None)

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(analyst, "datetime", FixedDateTime)

    freshness = analyst._compute_data_freshness()

    assert "✅ FRESH screen_results(短期)" in freshness
    assert "screen_results(短期): 14h前" not in freshness


def test_ensure_news_candidates_fresh_refreshes_stale_file(tmp_path, monkeypatch):
    (tmp_path / "news_signal_candidates.json").write_text(
        '{"generated_at": "2026-07-01 18:00", "candidates": []}',
        encoding="utf-8",
    )
    calls = []

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            base = real_datetime(2026, 7, 2, 8, 20, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
            if tz is not None:
                return base.astimezone(tz)
            return base.replace(tzinfo=None)

    monkeypatch.setattr(analyst, "datetime", FixedDateTime)

    refreshed = analyst._ensure_news_candidates_fresh(
        base_dir=tmp_path,
        max_age_hours=6,
        refresher=lambda: calls.append("refresh") or {"candidates": []},
    )

    assert refreshed is True
    assert calls == ["refresh"]


def test_ensure_news_candidates_fresh_skips_fresh_file(tmp_path, monkeypatch):
    (tmp_path / "news_signal_candidates.json").write_text(
        '{"generated_at": "2026-07-02 07:30", "candidates": []}',
        encoding="utf-8",
    )
    calls = []

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            base = real_datetime(2026, 7, 2, 8, 20, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
            if tz is not None:
                return base.astimezone(tz)
            return base.replace(tzinfo=None)

    monkeypatch.setattr(analyst, "datetime", FixedDateTime)

    refreshed = analyst._ensure_news_candidates_fresh(
        base_dir=tmp_path,
        max_age_hours=6,
        refresher=lambda: calls.append("refresh"),
    )

    assert refreshed is False
    assert calls == []


def test_extract_data_freshness_score_reads_japanese_summary_line():
    freshness = "\n".join([
        "【データ鮮度スコア】",
        "  総合スコア: 0.57/1.00",
        "  ⚠️ STALE technical_state(RSI/MACD): 4h前",
    ])

    assert analyst._extract_data_freshness_score(freshness) == 0.57

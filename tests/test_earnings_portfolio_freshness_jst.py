"""earnings_proximity_manager: portfolio_value_as_of resolver の新旧逆転修正・
naive timestamp の JST 固定解釈 (2026-09 レビュー S1・Codex 指摘)。

背景:
  1. _portfolio_total_observation は最初に検証を通った候補をそのまま返していた。
     guard_state.last_updated は評価額を再計算しない writer でも進む
     （behavioral_guard.save_state が全 save で更新する）ため、古い guard_state
     が新しい formal_analysis より優先され得た。→ portfolio_value_as_of という
     「実際に再評価した writer だけが書く」専用フィールドに切替え、かつ
     newest-as-of が勝つように修正する。
  2. _parse_input_timestamp / _aware_now は naive な文字列を
     datetime.now().astimezone().tzinfo（ホスト TZ 環境変数依存）で補完していた。
     このリポジトリの実データ (guard_state.last_updated, ai_portfolio_analysis.as_of)
     はどちらも naive。ホストが JST なら偶然正しいが、TZ=UTC 等で実行すると
     同一文字列が 9 時間ズレて解釈される（再現済み）。naive 入力は明示的に
     JST 固定で解釈する。
"""
from __future__ import annotations

import importlib
import json
from datetime import date, datetime, timedelta, timezone

import pytest


@pytest.fixture
def m(monkeypatch, tmp_path):
    mod = importlib.import_module("earnings_proximity_manager")
    monkeypatch.setattr(mod, "GUARD_STATE", tmp_path / "guard_state.json")
    monkeypatch.setattr(mod, "ANALYSIS", tmp_path / "ai_portfolio_analysis.json")
    return mod


# ── naive timestamp は host TZ ではなく JST 固定で解釈すること ─────────────

def test_naive_timestamp_parses_as_jst_regardless_of_host_tz(m, monkeypatch):
    naive = "2026-09-08T00:13:55.178193"  # T区切り、guard_state.last_updated 形式
    monkeypatch.setenv("TZ", "UTC")
    parsed_under_utc_host = m._parse_input_timestamp(naive)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    parsed_under_jst_host = m._parse_input_timestamp(naive)

    assert parsed_under_utc_host == parsed_under_jst_host, (
        "同一の naive 文字列がホスト TZ で異なる UTC 瞬間に解釈された"
    )
    # 2026-09-08 00:13:55 JST == 2026-09-07 15:13:55 UTC
    assert parsed_under_jst_host == datetime(2026, 9, 7, 15, 13, 55, 178193, tzinfo=timezone.utc)


def test_naive_space_separated_timestamp_parses_as_jst(m, monkeypatch):
    naive = "2026-09-08 06:26"  # ai_portfolio_analysis.json の as_of 形式（秒無し）
    monkeypatch.setenv("TZ", "UTC")
    parsed = m._parse_input_timestamp(naive)
    assert parsed == datetime(2026, 9, 7, 21, 26, 0, tzinfo=timezone.utc)


def test_input_age_hours_is_tz_independent(m, monkeypatch):
    naive = "2026-09-08T06:00:00"
    now = datetime(2026, 9, 8, 7, 0, 0, tzinfo=timezone(timedelta(hours=9)))  # 07:00 JST
    monkeypatch.setenv("TZ", "UTC")
    age_under_utc_host = m._input_age_hours(naive, now=now)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    age_under_jst_host = m._input_age_hours(naive, now=now)
    assert age_under_utc_host == age_under_jst_host == pytest.approx(1.0, abs=0.01)


def test_aware_now_naive_input_is_jst_not_host_tz(m, monkeypatch):
    naive_now = datetime(2026, 9, 8, 12, 0, 0)  # tzinfo 無し
    monkeypatch.setenv("TZ", "UTC")
    result = m._aware_now(naive_now)
    assert result == datetime(2026, 9, 8, 3, 0, 0, tzinfo=timezone.utc)  # 12:00 JST = 03:00 UTC


# ── resolver は「新しい方が勝つ」（返す前に全候補を評価する） ──────────────

def _write_guard(m, *, value, as_of):
    m.GUARD_STATE.write_text(json.dumps({
        "portfolio_value": value,
        "portfolio_value_as_of": as_of,
    }), encoding="utf-8")


def _write_analysis(m, *, value, as_of):
    m.ANALYSIS.write_text(json.dumps({
        "portfolio_total": value,
        "as_of": as_of,
    }), encoding="utf-8")


def test_stale_guard_state_never_outranks_a_newer_formal_analysis(m):
    """再現: 未再評価 writer が古い guard_state を新しい時刻のまま残しても、
    より新しい formal_analysis が優先される（旧実装は first-hit で guard_state
    を無条件に優先していた）。"""
    now = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
    _write_guard(m, value=20_000_000.0, as_of=(now - timedelta(hours=20)).isoformat())
    _write_analysis(m, value=29_638_880.0, as_of=(now - timedelta(hours=1)).isoformat())

    obs = m._portfolio_total_observation(now=now)
    assert obs["source"] == "formal_analysis"
    assert obs["value_jpy"] == 29_638_880.0


def test_fresher_guard_state_outranks_an_older_formal_analysis(m):
    now = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
    _write_guard(m, value=31_000_000.0, as_of=(now - timedelta(hours=1)).isoformat())
    _write_analysis(m, value=30_000_000.0, as_of=(now - timedelta(hours=20)).isoformat())

    obs = m._portfolio_total_observation(now=now)
    assert obs["source"] == "guard_state"
    assert obs["value_jpy"] == 31_000_000.0


def test_tie_prefers_guard_state(m):
    now = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
    same_ts = (now - timedelta(hours=2)).isoformat()
    _write_guard(m, value=31_000_000.0, as_of=same_ts)
    _write_analysis(m, value=30_000_000.0, as_of=same_ts)

    obs = m._portfolio_total_observation(now=now)
    assert obs["source"] == "guard_state"


def test_guard_state_missing_portfolio_value_as_of_falls_back_to_formal_analysis(m):
    """移行直後: 旧 guard_state.json に portfolio_value_as_of が無ければ、
    last_updated から補完せず不採用にする（不在＝不明を潰さない）。"""
    now = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
    m.GUARD_STATE.write_text(json.dumps({
        "portfolio_value": 31_000_000.0,
        "last_updated": now.isoformat(),
        # portfolio_value_as_of は無い（移行前の state）
    }), encoding="utf-8")
    _write_analysis(m, value=30_000_000.0, as_of=(now - timedelta(hours=1)).isoformat())

    obs = m._portfolio_total_observation(now=now)
    assert obs["source"] == "formal_analysis"


def test_both_stale_still_raises_with_both_failure_reasons(m):
    now = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
    _write_guard(m, value=31_000_000.0, as_of=(now - timedelta(hours=30)).isoformat())
    _write_analysis(m, value=30_000_000.0, as_of=(now - timedelta(hours=30)).isoformat())

    with pytest.raises(RuntimeError, match="current portfolio total is unavailable"):
        m._portfolio_total_observation(now=now)

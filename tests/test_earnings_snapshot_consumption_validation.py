"""earnings_proximity_manager: S2 — 同じ snapshot を検証して消費する。

fix_plan_v2.md の S2 節に対応。再現された欠陥（本セッション実装前の現行コード）:

1. `_load_current_snapshot()` は `snapshot_is_current()`（内部で1回読む）と、
   自身の `json.loads(OUTPUT.read_text(...))`（別の1回）で**同じファイルを
   2回別々に読む**。検証した内容と返す内容が同じ瞬間の同じデータである保証が
   無い（TOCTOU）。
2. `format_for_prompt()` は `snapshot_is_current()`・`OUTPUT.stat().st_mtime`・
   自身の `json.loads(...)` の**3回読む**。しかも `st_mtime` を
   `generated_at` とは独立の鮮度権威として使っている
   （v2 の明示的な禁止事項: 「mtime は鮮度の権威にしない」）。
3. NAV/FX は生成時刻の妥当性（パース可能か）は見るが、**消費時点での実年齢**
   （`PORTFOLIO_INPUT_MAX_AGE_HOURS`/`FX_INPUT_MAX_AGE_HOURS`、生成時の
   検証にしか使われていなかった）を再検証しない。生成直後は新鮮でも、
   同じ JST 日のうちに古くなった NAV/FX がそのまま「有効」扱いされ続ける。
4. `generated_at` が未来日時でも拒否しない。
5. `format_for_prompt()` の行レンダリングは try/except の外側にあり、1行でも
   表示用の数値が壊れていると関数全体が例外を投げる
   （呼出元 analyst/__init__.py は捕捉するが、結果として earnings_hedge
   コンテキストが丸ごと消える ―― 1行の欠陥で全件を失う）。
"""
from __future__ import annotations

import importlib
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest


def _current_snapshot(m, holdings, *, result_rows=None, generated_at=None,
                       portfolio_jpy_as_of=None, fx_rate_usdjpy_as_of=None,
                       suggestions=None):
    """result_rows は skipped 側（ticker 網羅チェックを満たす既定値あり）。
    suggestions を明示した場合はそちらを使い、skipped は result_rows が
    無ければ空にする（suggestions 側で ticker 網羅を満たす想定）。"""
    if suggestions is not None:
        sug = suggestions
        rows = result_rows if result_rows is not None else []
    else:
        sug = []
        rows = result_rows if result_rows is not None else [
            {"ticker": row["ticker"], "reason": "no_earnings_date"}
            for row in holdings
        ]
    # 既定の generated_at は「実時計の少し前・かつ必ず today と同じ暦日」に
    # する。固定 "06:15:00" を date.today() と組み合わせると、実行時刻が
    # 06:15 より前（例: 深夜0時台）だと「今日の 06:15」が未来日時になり
    # S2 の未来拒否チェックに引っかかる。単純な「5分前」も日境界をまたぐと
    # （例: 00:02 の5分前は前日）today と一致しなくなる
    # （いずれも 00:0x JST 実行で実際に再現・自己レビューで発見）。
    _now = datetime.now()
    _candidate = _now - timedelta(minutes=5)
    if _candidate.date() != _now.date():
        _candidate = datetime.combine(_now.date(), datetime.min.time())
    default_generated = _candidate.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "schema_version": m.OUTPUT_SCHEMA_VERSION,
        "generated_at": generated_at or default_generated,
        "holdings_scanned": len(holdings),
        "holdings_snapshot_sha256": m._holdings_snapshot_sha256(holdings),
        "portfolio_jpy": 30_000_000.0,
        "portfolio_jpy_source": "guard_state",
        "portfolio_jpy_as_of": portfolio_jpy_as_of if portfolio_jpy_as_of is not None else time.time(),
        "usd_jpy": 150.0,
        "fx_rate_source": "live",
        "fx_rate_usdjpy_as_of": fx_rate_usdjpy_as_of if fx_rate_usdjpy_as_of is not None else time.time(),
        "suggestions": sug,
        "skipped": rows,
    }


@pytest.fixture
def m(monkeypatch, tmp_path):
    mod = importlib.import_module("earnings_proximity_manager")
    output = tmp_path / "earnings_hedge_suggestions.json"
    overrides = tmp_path / "earnings_calendar_overrides.json"
    overrides.write_text('{"overrides": {}}', encoding="utf-8")
    monkeypatch.setattr(mod, "OUTPUT", output)
    monkeypatch.setattr(mod, "EARNINGS_OVERRIDES", overrides)
    return mod


def _write(m, holdings, **kw):
    m.OUTPUT.write_text(json.dumps(_current_snapshot(m, holdings, **kw)), encoding="utf-8")


# ── 再現の核心 1・2: 同じファイルを複数回読まない（TOCTOU） ──────────────

def _patch_output_read_counter(m, monkeypatch):
    """OUTPUT だけを対象にした read_text 呼出しカウンタ。EARNINGS_OVERRIDES
    等の他パスへの正当な読み込みは実体の read_text へ素通しする ―― 全 Path
    共通のクラスメソッドを差し替えるため、対象外のパスまで巻き込むと
    snapshot_is_current() 自身の正当な読み込みまで壊れてしまう。"""
    valid_text = m.OUTPUT.read_text(encoding="utf-8")
    calls = {"n": 0}
    real_read_text = type(m.OUTPUT).read_text

    def counting_read_text(self, *a, **kw):
        if self != m.OUTPUT:
            return real_read_text(self, *a, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            return valid_text
        # 2回目以降の読み込みが発生するなら、それは TOCTOU の兆候 ――
        # 意図的にパース不能な内容を返し、検出できるようにする。
        return "{not-json"

    monkeypatch.setattr(type(m.OUTPUT), "read_text", counting_read_text)
    return calls


def test_load_current_snapshot_reads_the_file_exactly_once(m, monkeypatch):
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    _write(m, holdings)

    calls = _patch_output_read_counter(m, monkeypatch)

    result = m._load_current_snapshot()

    assert calls["n"] == 1, f"OUTPUT を{calls['n']}回読んでいる（1回のみであるべき）"
    assert result is not None
    assert result["holdings_scanned"] == 1


def test_format_for_prompt_reads_the_file_exactly_once(m, monkeypatch):
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    # result_rows は省略（holdings を全件カバーする既定の skipped 行が入る）。
    # 空にすると ticker 網羅チェックで snapshot_is_current() が早期 False
    # になり、以降の読み込みに到達する前にテストの前提が崩れる。
    _write(m, holdings)

    calls = _patch_output_read_counter(m, monkeypatch)

    m.format_for_prompt()

    assert calls["n"] == 1, f"OUTPUT を{calls['n']}回読んでいる（1回のみであるべき）"


# ── 再現の核心 3: mtime を鮮度の権威として使わない ──────────────────────

def test_format_for_prompt_does_not_gate_on_mtime(m, monkeypatch):
    """generated_at・NAV/FX 実年齢が全て新鮮なら、mtime が古くても使う
    （v2 の明示的な禁止事項: mtime は鮮度の権威にしない）。"""
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    _write(m, holdings, result_rows=[{
        "ticker": "SYNTH_A", "earnings": (date.today() + timedelta(days=3)).isoformat(),
        "business_days": 2, "implied_move_pct": 5.0, "damage_pct": 2.0,
        "recommended_action": "hold", "reason": "in_window",
    }])
    # mtime を 25 時間前に見せかける（旧実装の 24h mtime チェックなら弾かれる）。
    old = time.time() - 25 * 3600
    os.utime(m.OUTPUT, (old, old))

    result = m.format_for_prompt()
    assert result != "", "mtime が古いというだけで有効な snapshot を空にしてはいけない"


# ── 再現の核心 4: 未来日時の generated_at を拒否する ───────────────────

def test_snapshot_rejects_future_dated_generation(m, monkeypatch):
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    now = datetime(2026, 9, 8, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    future = now + timedelta(hours=5)  # FUTURE_TOLERANCE_HOURS(1h) を大きく超える
    _write(
        m, holdings,
        generated_at=future.strftime("%Y-%m-%d %H:%M:%S"),
        portfolio_jpy_as_of=future.isoformat(),
        fx_rate_usdjpy_as_of=future.isoformat(),
    )

    assert m.snapshot_is_current(now=now) is False


def test_snapshot_accepts_generation_within_future_tolerance(m, monkeypatch):
    """既存の FUTURE_TOLERANCE_HOURS（他のタイムスタンプ検証と同じ許容幅）
    程度の時計ずれは許容する。"""
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    now = datetime(2026, 9, 8, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    slightly_future = now + timedelta(minutes=10)
    _write(
        m, holdings,
        generated_at=slightly_future.strftime("%Y-%m-%d %H:%M:%S"),
        portfolio_jpy_as_of=now.isoformat(),
        fx_rate_usdjpy_as_of=now.isoformat(),
    )

    assert m.snapshot_is_current(now=now) is True


# ── 再現の核心 5: NAV/FX の消費時点での実年齢を再検証する ───────────────

def test_snapshot_rejects_stale_portfolio_value_age_at_consumption_time(m, monkeypatch):
    """生成時点では新鮮でも、消費時点で PORTFOLIO_INPUT_MAX_AGE_HOURS を
    超えていれば拒否する（生成時の検証だけに頼らない）。"""
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    generated = datetime(2026, 9, 8, 6, 15, tzinfo=timezone(timedelta(hours=9)))
    stale_nav = generated - timedelta(hours=1)  # 生成時点では新鮮
    _write(
        m, holdings,
        generated_at=generated.strftime("%Y-%m-%d %H:%M:%S"),
        portfolio_jpy_as_of=stale_nav.isoformat(),
        fx_rate_usdjpy_as_of=generated.isoformat(),
    )
    # 消費が生成の PORTFOLIO_INPUT_MAX_AGE_HOURS(24h) 超あと ―― 同じ JST 日か
    # どうかに関わらず NAV は既に古い。
    consume_at = generated + timedelta(hours=25)

    assert m.snapshot_is_current(
        today=consume_at.date(), now=consume_at,
    ) is False


def test_snapshot_rejects_stale_fx_age_at_consumption_time(m, monkeypatch):
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    generated = datetime(2026, 9, 8, 6, 15, tzinfo=timezone(timedelta(hours=9)))
    _write(
        m, holdings,
        generated_at=generated.strftime("%Y-%m-%d %H:%M:%S"),
        portfolio_jpy_as_of=generated.isoformat(),
        fx_rate_usdjpy_as_of=generated.isoformat(),
    )
    consume_at = generated + timedelta(hours=m.FX_INPUT_MAX_AGE_HOURS + 1)

    assert m.snapshot_is_current(
        today=consume_at.date(), now=consume_at,
    ) is False


def test_snapshot_accepts_fresh_nav_fx_at_consumption_time(m, monkeypatch):
    """対照: 消費時点でもまだ新鮮なら通常どおり有効。"""
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    generated = datetime(2026, 9, 8, 6, 15, tzinfo=timezone(timedelta(hours=9)))
    _write(
        m, holdings,
        generated_at=generated.strftime("%Y-%m-%d %H:%M:%S"),
        portfolio_jpy_as_of=generated.isoformat(),
        fx_rate_usdjpy_as_of=generated.isoformat(),
    )
    consume_at = generated + timedelta(hours=1)

    assert m.snapshot_is_current(today=consume_at.date(), now=consume_at) is True


# ── 再現の核心 6: 1行の表示崩壊で全件を失わない ─────────────────────────

def test_format_for_prompt_omits_a_single_malformed_row_without_crashing(m, monkeypatch):
    holdings = [
        {"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"},
        {"ticker": "SYNTH_B", "shares": 1.0, "currency": "USD"},
    ]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    good_row = {
        "ticker": "SYNTH_A", "business_days": 2,
        "earnings_date": (date.today() + timedelta(days=3)).isoformat(),
        "implied_move_pct": 5.0, "damage_pct": 2.0,
        "recommended_action": "trim_50pct",
    }
    broken_row = {
        "ticker": "SYNTH_B", "business_days": 2,
        "earnings_date": (date.today() + timedelta(days=3)).isoformat(),
        "implied_move_pct": "not-a-number",  # .1f フォーマットで例外化する値
        "damage_pct": 2.0,
        "recommended_action": "trim_50pct",
    }
    _write(m, holdings, suggestions=[good_row, broken_row])

    result = m.format_for_prompt()  # 例外を投げないことそのものが再現の核心

    assert "SYNTH_A" in result, "壊れていない行まで失ってはいけない"
    assert "1件" in result or "表示不可" in result or "省略" in result, (
        "省略件数・理由をどこかに残すべき"
    )

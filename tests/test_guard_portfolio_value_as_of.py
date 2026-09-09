"""behavioral_guard: portfolio_value_as_of ―― 実際に再評価した writer だけが
書く専用フィールド (2026-09 レビュー S1)。

behavioral_guard.save_state() は全 save で last_updated を更新するため、
値を再評価しない writer（update_positions / log_override / _print_status /
損切りアラート書込）でもそのまま進んでしまう。これを evaluate_額の「本当に
評価したか」の証拠として消費する外部（earnings_proximity_manager）から見ると、
「古い評価額」を「新しい as_of」として誤って信頼させ得た。

update_pnl / snapshot_portfolio_pnl（唯一の実評価 writer）だけが
portfolio_value_as_of を進める。load_state() は移行時に last_updated から
これを補完しない（不在＝不明を潰さない）。
"""
from __future__ import annotations

import json

import behavioral_guard as bg


def test_default_state_has_portfolio_value_as_of_field():
    state = bg._default_state()
    assert "portfolio_value_as_of" in state
    assert state["portfolio_value_as_of"] is None


def test_update_pnl_stamps_portfolio_value_as_of(tmp_path, monkeypatch):
    state_file = tmp_path / "guard_state.json"
    monkeypatch.setattr(bg, "STATE_FILE", state_file)
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 10_000_000
    init["portfolio_value"] = 10_000_000
    state_file.write_text(json.dumps(init))

    bg.update_pnl(pnl_jpy=50_000, portfolio_value=10_040_000)

    state = json.loads(state_file.read_text())
    assert state["portfolio_value_as_of"] is not None
    assert state["portfolio_value"] == 10_040_000


def test_snapshot_portfolio_pnl_stamps_portfolio_value_as_of(tmp_path, monkeypatch):
    state_file = tmp_path / "guard_state.json"
    monkeypatch.setattr(bg, "STATE_FILE", state_file)
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 10_000_000
    state_file.write_text(json.dumps(init))

    class _FakeSnapshot:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 10_100_000, "positions": []}

    import sys
    monkeypatch.setitem(sys.modules, "portfolio_manager", _FakeSnapshot)

    result = bg.snapshot_portfolio_pnl()

    assert result["portfolio_value_as_of"] is not None
    state = json.loads(state_file.read_text())
    assert state["portfolio_value_as_of"] is not None


def test_writers_that_do_not_revalue_never_advance_portfolio_value_as_of(tmp_path, monkeypatch):
    """position 数の更新・override 記録・status 表示は評価時刻を進めない。"""
    state_file = tmp_path / "guard_state.json"
    monkeypatch.setattr(bg, "STATE_FILE", state_file)
    # log_override() は override_log.json を BASE_DIR 直下に書く（STATE_FILE とは
    # 別のハードコードされたパス）。隔離しないと本物のワークツリーを汚染する。
    monkeypatch.setattr(bg, "BASE_DIR", tmp_path)
    init = bg._default_state()
    init["portfolio_value"] = 10_000_000
    init["portfolio_value_as_of"] = "2026-09-01T00:00:00"
    init["last_eod_portfolio_value"] = 10_000_000
    state_file.write_text(json.dumps(init))

    bg.update_positions(active_trades=5, short_positions=0)
    after_positions = json.loads(state_file.read_text())
    assert after_positions["portfolio_value_as_of"] == "2026-09-01T00:00:00"

    bg.log_override("test override", "test action")
    after_override = json.loads(state_file.read_text())
    assert after_override["portfolio_value_as_of"] == "2026-09-01T00:00:00"

    bg._print_status()
    after_status = json.loads(state_file.read_text())
    assert after_status["portfolio_value_as_of"] == "2026-09-01T00:00:00"


def test_load_state_does_not_backfill_portfolio_value_as_of_from_last_updated(
    tmp_path, monkeypatch,
):
    """移行前の旧 state（portfolio_value_as_of 無し）を読んでも、last_updated
    から捏造しない ―― 不在は不在のまま。実際に評価が起きるまで欠落が続くのは
    意図した挙動（1時間毎の alert.py サイクルで自然に埋まる）。"""
    state_file = tmp_path / "guard_state.json"
    monkeypatch.setattr(bg, "STATE_FILE", state_file)
    old_state = bg._default_state()
    del old_state["portfolio_value_as_of"]
    old_state["last_updated"] = "2026-09-01T12:00:00"
    old_state["portfolio_value"] = 10_000_000
    state_file.write_text(json.dumps(old_state))

    loaded = bg.load_state()
    assert loaded.get("portfolio_value_as_of") is None

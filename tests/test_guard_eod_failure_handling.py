"""behavioral_guard: EOD 確定は「評価に成功した run」だけが行う (2026-09 レビュー
S1b・Codex 指摘1)。

再現された欠陥: snapshot_portfolio_pnl() は評価額取得に失敗すると内部で
print して load_state() をそのまま返すだけだった（例外を投げない）。CLI の
`snapshot --eod` 分岐はこの「失敗」を検知する手段が無く、--eod ブロックを
無条件に実行して既存の（無関係に古いままの）portfolio_value を翌日の EOD
基準として確定していた:

    [SNAPSHOT] ポートフォリオ取得失敗: No module named 'portfolio_manager'
    [SNAPSHOT] EOD基準を確定: ¥20,000,000   ← 3日前の値が「今日確定」した

これは平日17:35 cron の実際の失敗経路。修正: 評価失敗は専用例外
PortfolioValuationUnavailable で呼出元へ伝える。CLI 側は EOD 確定・
_print_status を一切実行せず、heartbeat を warn、非ゼロ終了する。
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import behavioral_guard as bg


@pytest.fixture
def guard_state(tmp_path, monkeypatch):
    state_file = tmp_path / "guard_state.json"
    monkeypatch.setattr(bg, "STATE_FILE", state_file)
    init = bg._default_state()
    init["last_eod_portfolio_value"] = 20_000_000.0
    init["portfolio_value"] = 20_000_000.0
    state_file.write_text(json.dumps(init), encoding="utf-8")
    return state_file


def test_snapshot_portfolio_pnl_raises_a_dedicated_exception_on_valuation_failure(
    guard_state, monkeypatch,
):
    monkeypatch.setitem(sys.modules, "portfolio_manager", None)  # import 失敗を模倣

    with pytest.raises(bg.PortfolioValuationUnavailable):
        bg.snapshot_portfolio_pnl()

    # 失敗した run は state を一切書き換えていない。
    state = json.loads(guard_state.read_text())
    assert state["portfolio_value"] == 20_000_000.0
    assert state.get("portfolio_value_as_of") is None


def test_eod_flag_is_not_confirmed_when_valuation_fails(guard_state, monkeypatch):
    monkeypatch.setitem(sys.modules, "portfolio_manager", None)

    rc = bg._run_snapshot_cli(["snapshot", "--eod"])

    assert rc == 1
    state = json.loads(guard_state.read_text())
    assert state["last_eod_portfolio_value"] == 20_000_000.0, (
        "評価に失敗した run が EOD 基準を確定してはいけない"
    )


def test_eod_flag_still_confirms_on_successful_valuation(guard_state, monkeypatch):
    class _FakeSnapshot:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 20_500_000.0, "positions": []}

    monkeypatch.setitem(sys.modules, "portfolio_manager", _FakeSnapshot)

    rc = bg._run_snapshot_cli(["snapshot", "--eod"])

    assert rc == 0
    state = json.loads(guard_state.read_text())
    assert state["last_eod_portfolio_value"] == 20_500_000.0
    assert state["last_eod_portfolio_value_as_of"] is not None


def test_valuation_failure_heartbeats_warn_not_error(guard_state, monkeypatch):
    monkeypatch.setitem(sys.modules, "portfolio_manager", None)
    calls = []
    monkeypatch.setattr(bg, "heartbeat", lambda *a, **k: calls.append((a, k)))

    bg._run_snapshot_cli(["snapshot", "--eod"])

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "behavioral_guard_snapshot"
    status = kwargs.get("status") or (args[1] if len(args) > 1 else None)
    assert status == "warn"


def test_valuation_failure_never_raises_a_traceback(guard_state, monkeypatch):
    """他の未知例外とは違い、評価失敗は「予想される外部データ異常」として
    静かに non-zero を返す（portfolio_agent.py の LockBusy と同じ扱い）。"""
    monkeypatch.setitem(sys.modules, "portfolio_manager", None)
    monkeypatch.setattr(bg, "heartbeat", lambda *a, **k: None)

    rc = bg._run_snapshot_cli(["snapshot"])  # --eod 無しでも同様
    assert rc == 1


def test_unexpected_exception_after_successful_valuation_still_reraises(
    guard_state, monkeypatch,
):
    """valuation 自体は成功したが、それ以降で未知の例外が起きた場合は
    従来どおり heartbeat=error のうえ re-raise する（挙動維持）。"""
    class _FakeSnapshot:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 20_500_000.0, "positions": []}

    monkeypatch.setitem(sys.modules, "portfolio_manager", _FakeSnapshot)

    def _boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(bg, "_print_status", _boom)
    calls = []
    monkeypatch.setattr(bg, "heartbeat", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(RuntimeError, match="unexpected"):
        bg._run_snapshot_cli(["snapshot"])

    assert len(calls) == 1
    args, kwargs = calls[0]
    status = kwargs.get("status") or (args[1] if len(args) > 1 else None)
    assert status == "error"


def test_unexpected_exception_inside_snapshot_computation_still_heartbeats_error(
    guard_state, monkeypatch,
):
    """再現 (2026-09 レビュー Codex 2ラウンド目 指摘 #8): valuation 自体
    (portfolio_manager.build_portfolio_snapshot / positive_finite) は成功
    したのに、それ以降 snapshot_portfolio_pnl() 内部（例: save_state の
    atomic_write_json 書き込み失敗）で起きた未知の例外は
    PortfolioValuationUnavailable を経由しないため、_run_snapshot_cli の
    最初の except では捕まらず、heartbeat が一切呼ばれないまま無音で
    伝播していた。__main__ が平坦だった旧実装はここも捕捉していたため、
    _run_snapshot_cli への分離でこの経路だけ退行していた。"""
    class _FakeSnapshot:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 20_500_000.0, "positions": []}

    monkeypatch.setitem(sys.modules, "portfolio_manager", _FakeSnapshot)

    def _boom(_state):
        raise OSError("disk full")

    monkeypatch.setattr(bg, "save_state", _boom)
    calls = []
    monkeypatch.setattr(bg, "heartbeat", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(OSError, match="disk full"):
        bg._run_snapshot_cli(["snapshot"])

    assert len(calls) == 1, "snapshot_portfolio_pnl 内部の未知例外は heartbeat=error されるべき"
    args, kwargs = calls[0]
    assert args[0] == "behavioral_guard_snapshot"
    status = kwargs.get("status") or (args[1] if len(args) > 1 else None)
    assert status == "error"


def test_successful_run_without_eod_flag_still_heartbeats_ok(guard_state, monkeypatch):
    class _FakeSnapshot:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": 20_500_000.0, "positions": []}

    monkeypatch.setitem(sys.modules, "portfolio_manager", _FakeSnapshot)
    calls = []
    monkeypatch.setattr(bg, "heartbeat", lambda *a, **k: calls.append((a, k)))

    rc = bg._run_snapshot_cli(["snapshot"])

    assert rc == 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    status = kwargs.get("status") or (args[1] if len(args) > 1 else None)
    assert status == "ok"


# ── issue #5 (Codex): 評価額の書込み側にも検証が要る ────────────────────

@pytest.mark.parametrize("bad_value", [0, -5_000_000, True, float("nan"), "not-a-number"])
def test_invalid_valuation_is_rejected_not_saved(guard_state, monkeypatch, bad_value):
    """0・負数・bool・NaN・非数値文字列はいずれも PortfolioValuationUnavailable
    として扱い、guard_state を汚染しない（2026-09 レビュー Codex 指摘 #5:
    読み取り側 (earnings_proximity_manager) は検証するのに書込み側には検証が
    無く、bool True が portfolio_value=True としてそのまま保存され、
    翌日の EOD 基準まで汚染されていた）。"""
    class _Fake:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": bad_value, "positions": []}

    monkeypatch.setitem(sys.modules, "portfolio_manager", _Fake)

    with pytest.raises(bg.PortfolioValuationUnavailable):
        bg.snapshot_portfolio_pnl()

    state = json.loads(guard_state.read_text())
    assert state["portfolio_value"] == 20_000_000.0, "無効な評価額を保存してはいけない"
    assert state.get("portfolio_value_as_of") is None


def test_invalid_valuation_via_cli_does_not_confirm_eod(guard_state, monkeypatch):
    class _Fake:
        @staticmethod
        def build_portfolio_snapshot():
            return {"total_jpy": True, "positions": []}  # bool 混入

    monkeypatch.setitem(sys.modules, "portfolio_manager", _Fake)
    calls = []
    monkeypatch.setattr(bg, "heartbeat", lambda *a, **k: calls.append((a, k)))

    rc = bg._run_snapshot_cli(["snapshot", "--eod"])

    assert rc == 1
    state = json.loads(guard_state.read_text())
    assert state["last_eod_portfolio_value"] == 20_000_000.0, (
        "bool/0/負数の評価額で EOD 基準を確定してはいけない"
    )
    assert type(state["last_eod_portfolio_value"]) is not bool
    assert len(calls) == 1
    status = calls[0][1].get("status") or calls[0][0][1]
    assert status == "warn"

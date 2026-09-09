"""earnings_proximity_manager: producer 監視 (2026-09 レビュー S0)。

b9f3f9d/1e95b38 で holdings 破損・NAV/FX 未検証を fail-closed 例外化したが、
この producer は heartbeat() を一度も呼んでおらず watchdog にも未登録だった
――例外が誰にも気づかれず cron が消えるだけになり得た。

設計:
  - --scheduled 実行だけが共有 heartbeat（watchdog が見る信号）を更新する。
    手動実行や analyst 内 self-heal の成功が cron 停止を隠してはいけない。
  - --dry-run は監視記録も成果物も更新しない。
  - 起動区分（scheduled/manual）・成否・生成時刻・snapshot hash・再利用有無は
    scan() 内で共通に実行履歴 (earnings_proximity_run_history.jsonl) へ残す。
    self-heal（analyst/__init__.py から scan() を直接呼ぶ経路）もこの履歴を
    通る一方、heartbeat は更新しない。
"""
from __future__ import annotations

import importlib
import json
import sys

import pytest


@pytest.fixture
def m(monkeypatch, tmp_path):
    mod = importlib.import_module("earnings_proximity_manager")
    monkeypatch.setattr(mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(mod, "OUTPUT", tmp_path / "earnings_hedge_suggestions.json")
    monkeypatch.setattr(mod, "RUN_HISTORY_PATH", tmp_path / "earnings_proximity_run_history.jsonl")
    return mod


def _run_history_rows(m):
    if not m.RUN_HISTORY_PATH.exists():
        return []
    return [json.loads(line) for line in m.RUN_HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── main(argv) / --scheduled が heartbeat を分離すること ─────────────────

def test_dry_run_never_writes_heartbeat_or_run_history(m, monkeypatch):
    monkeypatch.setattr(m, "scan", lambda **kw: {"suggestion_count": 0})
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    rc = m.main(["--dry-run"])

    assert rc == 0
    assert calls == []
    assert _run_history_rows(m) == []


def test_scheduled_success_updates_the_shared_heartbeat(m, monkeypatch):
    monkeypatch.setattr(
        m, "scan",
        lambda dry_run=False, reuse_current=True, kind="manual": {
            "suggestion_count": 2, "holdings_scanned": 5,
            "generated_at": "2026-09-08 06:15:00",
        },
    )
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    rc = m.main(["--scheduled"])

    assert rc == 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "earnings_proximity"
    assert kwargs.get("status") == "ok" or args[1:2] == ("ok",)


def test_manual_success_never_touches_the_shared_heartbeat(m, monkeypatch):
    """手動実行の成功が cron 停止を隠してはならない ―― heartbeat を一切更新しない。"""
    monkeypatch.setattr(
        m, "scan",
        lambda dry_run=False, reuse_current=True, kind="manual": {
            "suggestion_count": 0, "holdings_scanned": 5, "generated_at": "x",
        },
    )
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    rc = m.main([])  # --scheduled を付けない = 手動

    assert rc == 0
    assert calls == [], "手動実行は共有 heartbeat を更新してはいけない"


def test_scheduled_failure_heartbeats_error_and_reraises(m, monkeypatch):
    def _boom(**kw):
        raise ValueError("holdings source is unreadable: boom")

    monkeypatch.setattr(m, "scan", _boom)
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(ValueError, match="boom"):
        m.main(["--scheduled"])

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "earnings_proximity"
    status = kwargs.get("status") or (args[1] if len(args) > 1 else None)
    assert status == "error"


def test_manual_failure_also_never_touches_the_shared_heartbeat(m, monkeypatch):
    def _boom(**kw):
        raise ValueError("boom")

    monkeypatch.setattr(m, "scan", _boom)
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(ValueError):
        m.main(["--force"])  # 手動の強制再スキャン、--scheduled 無し

    assert calls == []


def test_scheduled_lock_busy_warns_and_returns_nonzero_without_reraising(m, monkeypatch):
    """他プロセスが scan 中なのは異常終了ではない（portfolio_agent.py と同じ扱い）。"""
    from utils import LockBusy

    def _busy(**kw):
        raise LockBusy("lock busy")

    monkeypatch.setattr(m, "scan", _busy)
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    rc = m.main(["--scheduled"])

    assert rc == 1
    assert len(calls) == 1
    args, kwargs = calls[0]
    status = kwargs.get("status") or (args[1] if len(args) > 1 else None)
    assert status == "warn"


def test_force_flag_disables_reuse_current(m, monkeypatch):
    captured = {}

    def _capture(dry_run=False, reuse_current=True, kind="manual"):
        captured["reuse_current"] = reuse_current
        return {"suggestion_count": 0, "holdings_scanned": 0, "generated_at": "x"}

    monkeypatch.setattr(m, "scan", _capture)
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: None)

    m.main(["--force"])
    assert captured["reuse_current"] is False

    m.main([])
    assert captured["reuse_current"] is True


def test_scheduled_kind_is_passed_through_to_scan(m, monkeypatch):
    captured = {}

    def _capture(dry_run=False, reuse_current=True, kind="manual"):
        captured["kind"] = kind
        return {"suggestion_count": 0, "holdings_scanned": 0, "generated_at": "x"}

    monkeypatch.setattr(m, "scan", _capture)
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: None)

    m.main(["--scheduled"])
    assert captured["kind"] == "scheduled"

    m.main([])
    assert captured["kind"] == "manual"


# ── scan(kind=...) が起動区分ごとに実行履歴を残すこと ─────────────────────

def test_scan_default_kind_is_manual_for_backward_compatibility(m, monkeypatch):
    """analyst/__init__.py の既存 self-heal 呼出し (`scan(dry_run=False,
    reuse_current=True)`) を変更しないため、kind 未指定は "manual" になる。"""
    holdings = [{"ticker": "SYNTH_A", "shares": 1.0, "currency": "USD"}]
    monkeypatch.setattr(m, "_load_holdings", lambda: holdings)
    monkeypatch.setattr(
        m, "_portfolio_total_observation",
        lambda: {"value_jpy": 30_000_000.0, "source": "guard_state", "as_of": "2026-09-08T06:00:00"},
    )
    monkeypatch.setattr(
        m, "_fx_rate_observation",
        lambda: {"rate": 150.0, "source": "live", "observed_at": 0},
    )
    monkeypatch.setattr(m, "_next_earnings_with_source", lambda _t: None)

    out = m.scan(dry_run=False, reuse_current=False)  # self-heal と同じ呼び方 (kind 省略)

    rows = _run_history_rows(m)
    assert len(rows) == 1
    assert rows[0]["kind"] == "manual"
    assert rows[0]["status"] == "ok"
    assert rows[0]["reused"] is False


def test_scan_records_reused_true_on_cache_hit(m, monkeypatch):
    current = {
        "schema_version": m.OUTPUT_SCHEMA_VERSION,
        "generated_at": "2026-09-08 06:15:00",
        "holdings_scanned": 0,
        "suggestions": [], "skipped": [],
    }
    monkeypatch.setattr(m, "_load_current_snapshot", lambda: current)

    out = m.scan(dry_run=False, reuse_current=True, kind="scheduled")

    assert out is current
    rows = _run_history_rows(m)
    assert len(rows) == 1
    assert rows[0]["kind"] == "scheduled"
    assert rows[0]["reused"] is True
    assert rows[0]["status"] == "ok"


def test_scan_dry_run_never_writes_run_history_regardless_of_kind(m, monkeypatch):
    monkeypatch.setattr(m, "_load_holdings", lambda: [])
    monkeypatch.setattr(
        m, "_portfolio_total_observation",
        lambda: {"value_jpy": 30_000_000.0, "source": "guard_state", "as_of": "2026-09-08T06:00:00"},
    )
    monkeypatch.setattr(
        m, "_fx_rate_observation",
        lambda: {"rate": 150.0, "source": "live", "observed_at": 0},
    )
    m.scan(dry_run=True, kind="scheduled")
    assert _run_history_rows(m) == []


def test_scan_records_error_status_and_still_raises(m, monkeypatch):
    monkeypatch.setattr(m, "_load_current_snapshot", lambda: None)

    def _boom(dry_run=False):
        raise RuntimeError("network down")

    monkeypatch.setattr(m, "_scan_once", _boom)

    with pytest.raises(RuntimeError, match="network down"):
        m.scan(dry_run=False, reuse_current=True, kind="scheduled")

    rows = _run_history_rows(m)
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["kind"] == "scheduled"


def test_scan_lock_busy_is_recorded_in_run_history(m, monkeypatch):
    from utils import LockBusy

    def _fake_process_lock(name, timeout=0.0):
        raise LockBusy(f"lock '{name}' busy")

    import contextlib

    @contextlib.contextmanager
    def _cm(name, timeout=0.0):
        raise LockBusy(f"lock '{name}' busy")
        yield  # pragma: no cover

    monkeypatch.setattr("utils.process_lock", _cm)

    with pytest.raises(LockBusy):
        m.scan(dry_run=False, reuse_current=True, kind="scheduled")

    rows = _run_history_rows(m)
    assert len(rows) == 1
    assert rows[0]["status"] == "lock_busy"


def test_lock_busy_from_inside_scan_once_is_recorded_only_once(m, monkeypatch):
    """LockBusy が (将来的にでも) _scan_once() 内部から上がってきても、
    内側の except Exception と外側の except LockBusy の両方で二重記録しない
    （自己レビューで発見: 現状 _scan_once() はロックを取らないため未到達だが、
    どちらのハンドラが本来の記録者かを明確にしておく）。"""
    from utils import LockBusy

    monkeypatch.setattr(m, "_load_current_snapshot", lambda: None)

    def _boom(dry_run=False):
        raise LockBusy("unexpected nested lock contention")

    monkeypatch.setattr(m, "_scan_once", _boom)

    with pytest.raises(LockBusy):
        m.scan(dry_run=False, reuse_current=True, kind="scheduled")

    rows = _run_history_rows(m)
    assert len(rows) == 1, f"expected exactly one row, got {rows}"
    assert rows[0]["status"] == "lock_busy"


def test_run_history_is_append_only_across_multiple_scans(m, monkeypatch):
    current = {
        "schema_version": m.OUTPUT_SCHEMA_VERSION, "generated_at": "x",
        "holdings_scanned": 0, "suggestions": [], "skipped": [],
    }
    monkeypatch.setattr(m, "_load_current_snapshot", lambda: current)

    m.scan(dry_run=False, reuse_current=True, kind="scheduled")
    m.scan(dry_run=False, reuse_current=True, kind="manual")

    rows = _run_history_rows(m)
    assert len(rows) == 2
    assert [r["kind"] for r in rows] == ["scheduled", "manual"]


# ── S9-Codex#9: run history 書き込み失敗が scheduled heartbeat から見えること ──
#
# _record_run_history() は書き込み失敗時に stderr へ print するだけで、
# scan() にも main() にも一切失敗を返していなかった（戻り値は常に None）。
# 「監視記録という補助情報の失敗で scan() 自体は止めない」設計は正しいが、
# その失敗自体が watchdog から完全に不可視だった ―― scan() は成功、
# --scheduled heartbeat も "ok" のまま、run history だけが静かに欠落し得た。


def test_record_run_history_returns_false_on_write_failure(m):
    """direct unit test: 書き込み失敗時は False を返す（従来は None 固定）。"""
    missing_dir = m.BASE_DIR / "does_not_exist" / "history.jsonl"
    m2 = m  # alias for clarity
    orig = m2.RUN_HISTORY_PATH
    try:
        m2.RUN_HISTORY_PATH = missing_dir  # type: ignore[attr-defined]
        ok = m2._record_run_history(kind="manual", status="ok", reused=False)
    finally:
        m2.RUN_HISTORY_PATH = orig
    assert ok is False


def test_record_run_history_returns_true_on_success(m):
    ok = m._record_run_history(kind="manual", status="ok", reused=False)
    assert ok is True


def test_scan_success_surfaces_run_history_write_failure_in_return_value(m, monkeypatch):
    monkeypatch.setattr(m, "_load_current_snapshot", lambda: None)
    monkeypatch.setattr(
        m, "_scan_once",
        lambda dry_run=False: {"generated_at": "x", "holdings_snapshot_sha256": "y"},
    )
    monkeypatch.setattr(m, "_record_run_history", lambda **kw: False)

    out = m.scan(dry_run=False, reuse_current=True, kind="scheduled")

    assert out["run_history_recorded"] is False


def test_scan_success_surfaces_run_history_write_success_in_return_value(m, monkeypatch):
    monkeypatch.setattr(m, "_load_current_snapshot", lambda: None)
    monkeypatch.setattr(
        m, "_scan_once",
        lambda dry_run=False: {"generated_at": "x", "holdings_snapshot_sha256": "y"},
    )
    monkeypatch.setattr(m, "_record_run_history", lambda **kw: True)

    out = m.scan(dry_run=False, reuse_current=True, kind="scheduled")

    assert out["run_history_recorded"] is True


def test_scan_reused_cache_hit_surfaces_run_history_write_failure(m, monkeypatch):
    current = {
        "schema_version": m.OUTPUT_SCHEMA_VERSION,
        "generated_at": "2026-09-08 06:15:00",
        "holdings_scanned": 0,
        "suggestions": [], "skipped": [],
    }
    monkeypatch.setattr(m, "_load_current_snapshot", lambda: current)
    monkeypatch.setattr(m, "_record_run_history", lambda **kw: False)

    out = m.scan(dry_run=False, reuse_current=True, kind="scheduled")

    assert out is current, "既存の identity 契約（キャッシュヒットは同一オブジェクトを返す）を保つこと"
    assert out["run_history_recorded"] is False


def test_scheduled_heartbeat_extra_surfaces_run_history_recorded_false(m, monkeypatch):
    monkeypatch.setattr(
        m, "scan",
        lambda dry_run=False, reuse_current=True, kind="manual": {
            "suggestion_count": 2, "holdings_scanned": 5,
            "generated_at": "2026-09-08 06:15:00",
            "run_history_recorded": False,
        },
    )
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    rc = m.main(["--scheduled"])

    assert rc == 0
    args, kwargs = calls[0]
    extra = kwargs.get("extra") or {}
    assert extra.get("run_history_recorded") is False


# ── S6-Codex#6 3ラウンド目: run_history_recorded=False を実際の監視へ反映する ──
#
# ラウンド2で extra={"run_history_recorded": ...} への伝播は実装したが、
# それを読んで何かする consumer が一つも無かった ―― main() の終了コードは
# 常に 0、heartbeat の status も常に "ok" のまま、watchdog は extra の中身を
# 一切見ない（_check_backup_offsite の offsite_status 専用ロジックのみが
# extra を読む唯一の場所で、run_history_recorded とは無関係）。実際に
# 履歴書込みを失敗させて producer から watchdog まで通しても、
# heartbeat=ok・watchdog=ok のままだった（2026-09 レビュー Codex 3ラウンド目
# 指摘 #6・実機再現）。


def test_run_history_write_failure_downgrades_scheduled_heartbeat_to_warn(m, monkeypatch):
    """再現の核心: フラグを立てるだけでなく、既存の warn_is_error 経路
    （earnings_proximity は EXPECTED_INTERVALS で既に warn_is_error=True）
    へ実際に接続し、watchdog がこれを ok と区別できるようにする。"""
    monkeypatch.setattr(
        m, "scan",
        lambda dry_run=False, reuse_current=True, kind="manual": {
            "suggestion_count": 2, "holdings_scanned": 5,
            "generated_at": "2026-09-08 06:15:00",
            "run_history_recorded": False,
        },
    )
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    rc = m.main(["--scheduled"])

    assert rc == 0, "run history の失敗は scan 自体の成功を覆さない（補助記録のみの失敗）"
    args, kwargs = calls[0]
    status = kwargs.get("status") if "status" in kwargs else (args[1] if len(args) > 1 else None)
    assert status == "warn", "run_history_recorded=False は heartbeat=ok のままにしてはいけない"

    import watchdog as wd
    hb = {"earnings_proximity": {
        "last_run_ts": __import__("time").time(),
        "status": status,
        "error": kwargs.get("error"),
    }}
    result = wd.evaluate_heartbeats(hb)
    assert any(e["script"] == "earnings_proximity" for e in result["errors"]), (
        "warn_is_error 経路を通って watchdog の errors に現れるべき"
    )


def test_run_history_write_success_keeps_scheduled_heartbeat_ok(m, monkeypatch):
    monkeypatch.setattr(
        m, "scan",
        lambda dry_run=False, reuse_current=True, kind="manual": {
            "suggestion_count": 2, "holdings_scanned": 5,
            "generated_at": "2026-09-08 06:15:00",
            "run_history_recorded": True,
        },
    )
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    m.main(["--scheduled"])

    args, kwargs = calls[0]
    status = kwargs.get("status") if "status" in kwargs else (args[1] if len(args) > 1 else None)
    assert status == "ok"


def test_scheduled_heartbeat_extra_defaults_run_history_recorded_true_when_absent(
    m, monkeypatch,
):
    """scan() の戻り値にキー自体が無い場合（古いモック等）は True 扱い
    ―― 新キーの追加でこれまで緑だったテスト/呼び出し元を壊さない。"""
    monkeypatch.setattr(
        m, "scan",
        lambda dry_run=False, reuse_current=True, kind="manual": {
            "suggestion_count": 2, "holdings_scanned": 5, "generated_at": "x",
        },
    )
    calls = []
    monkeypatch.setattr(m, "heartbeat", lambda *a, **k: calls.append((a, k)))

    m.main(["--scheduled"])

    args, kwargs = calls[0]
    extra = kwargs.get("extra") or {}
    assert extra.get("run_history_recorded") is True

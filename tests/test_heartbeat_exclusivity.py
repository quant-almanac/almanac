"""heartbeat() の排他化 (2026-09 レビュー S0-d)。

utils.heartbeat() は heartbeats.json を read-modify-write していたが排他ロックが
無く、平日 18:25/18:47/18:50/18:55 のように近接した cron が同時に呼ぶと
lost-update で片方の更新が消え得た。

修正方針:
  - 有限タイムアウトの共有ロックで書込みを直列化する。
  - ロックが取れない場合は共有 JSON を書かない（更新消失より監視欠落を選ぶ、
    のではなく「ロック無しで書いて他プロセスの更新を上書きする」を禁止する）。
  - 書けなかった事実は stderr と独立の退避記録 (heartbeat_lock_failures.jsonl)
    へ残し、watchdog 側がそれを拾えるようにする。
"""
from __future__ import annotations

import fcntl
import json
import os
import time

import pytest

import utils


@pytest.fixture
def isolated_heartbeat(tmp_path, monkeypatch):
    """heartbeats.json とロックディレクトリを隔離する。"""
    monkeypatch.setattr(utils, "HEARTBEAT_PATH", tmp_path / "heartbeats.json")
    monkeypatch.setattr(utils, "LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(
        utils, "HEARTBEAT_LOCK_FAILURES_PATH",
        tmp_path / "heartbeat_lock_failures.jsonl",
    )
    return tmp_path


def _hold_lock_externally(tmp_path, name: str):
    """同一プロセス内から別 fd で flock を保持し、他の取得者を実際にブロックする。

    同一プロセスでも open file description が異なれば flock は競合するため、
    「別プロセスがロックを保持している」を安全に模倣できる（検証済み）。
    """
    lock_path = tmp_path / "locks" / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_heartbeat_succeeds_and_returns_true_when_lock_is_free(isolated_heartbeat):
    ok = utils.heartbeat("earnings_proximity", status="ok", extra={"n": 1})
    assert ok is True
    data = json.loads(utils.HEARTBEAT_PATH.read_text(encoding="utf-8"))
    assert data["earnings_proximity"]["status"] == "ok"
    assert data["earnings_proximity"]["extra"] == {"n": 1}


def test_heartbeat_does_not_clobber_shared_json_when_lock_is_busy(isolated_heartbeat):
    """ロック競合時、既存の heartbeats.json 内容を一切変更しない（lost-update 防止が核心）。"""
    # 先に別スクリプトの正常な heartbeat を書いておく。
    assert utils.heartbeat("data_fetcher", status="ok") is True
    before = utils.HEARTBEAT_PATH.read_text(encoding="utf-8")

    fd = _hold_lock_externally(isolated_heartbeat, utils.HEARTBEAT_LOCK_NAME)
    try:
        ok = utils.heartbeat("earnings_proximity", status="ok")
        assert ok is False, "ロック競合時は False を返し、共有 JSON へは書かない"
    finally:
        os.close(fd)

    after = utils.HEARTBEAT_PATH.read_text(encoding="utf-8")
    assert after == before, "ロック無しで書き込み、他プロセスの更新を上書きしてはならない"
    assert "earnings_proximity" not in json.loads(after)


def test_heartbeat_lock_timeout_is_finite_and_bounded(isolated_heartbeat):
    """ロックが解放されない場合でも、有限時間内に諦めて戻る。"""
    fd = _hold_lock_externally(isolated_heartbeat, utils.HEARTBEAT_LOCK_NAME)
    try:
        started = time.monotonic()
        ok = utils.heartbeat("earnings_proximity", status="ok")
        elapsed = time.monotonic() - started
        assert ok is False
        assert elapsed < utils.HEARTBEAT_LOCK_TIMEOUT_SECONDS + 2.0, (
            "有限待機のはずが長時間ブロックした"
        )
    finally:
        os.close(fd)


def test_heartbeat_lock_failure_never_raises_to_the_caller(isolated_heartbeat):
    """呼出元は heartbeat() の失敗で自身の処理を止めてはならない（例外を外へ漏らさない）。"""
    fd = _hold_lock_externally(isolated_heartbeat, utils.HEARTBEAT_LOCK_NAME)
    try:
        utils.heartbeat("earnings_proximity", status="error", error="boom")
    except Exception as exc:  # pragma: no cover - このテストの目的そのもの
        pytest.fail(f"heartbeat() が例外を送出した: {exc}")
    finally:
        os.close(fd)


def test_heartbeat_lock_failure_is_recorded_to_fallback_file(isolated_heartbeat):
    """書けなかった事実を独立の退避記録へ残す（監視がここを拾えるように）。"""
    fd = _hold_lock_externally(isolated_heartbeat, utils.HEARTBEAT_LOCK_NAME)
    try:
        utils.heartbeat("earnings_proximity", status="error", error="valuation failed")
    finally:
        os.close(fd)

    assert utils.HEARTBEAT_LOCK_FAILURES_PATH.exists()
    lines = utils.HEARTBEAT_LOCK_FAILURES_PATH.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["script"] == "earnings_proximity"
    assert row["status"] == "error"
    assert isinstance(row["attempted_ts"], (int, float))


def test_heartbeat_fallback_records_are_append_only_across_multiple_failures(
    isolated_heartbeat,
):
    """複数回の失敗が退避記録を破壊せず積み上がる（read-modify-write を再導入しない）。"""
    fd = _hold_lock_externally(isolated_heartbeat, utils.HEARTBEAT_LOCK_NAME)
    try:
        utils.heartbeat("earnings_proximity", status="error")
        utils.heartbeat("behavioral_guard_snapshot", status="warn")
    finally:
        os.close(fd)

    lines = utils.HEARTBEAT_LOCK_FAILURES_PATH.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    scripts = {json.loads(line)["script"] for line in lines}
    assert scripts == {"earnings_proximity", "behavioral_guard_snapshot"}


def test_heartbeat_recovers_once_the_lock_is_released(isolated_heartbeat):
    """競合が解消すれば通常どおり成功する（恒久的なフォールバックではない）。"""
    fd = _hold_lock_externally(isolated_heartbeat, utils.HEARTBEAT_LOCK_NAME)
    assert utils.heartbeat("earnings_proximity", status="ok") is False
    os.close(fd)

    ok = utils.heartbeat("earnings_proximity", status="ok", extra={"retry": True})
    assert ok is True
    data = json.loads(utils.HEARTBEAT_PATH.read_text(encoding="utf-8"))
    assert data["earnings_proximity"]["extra"] == {"retry": True}

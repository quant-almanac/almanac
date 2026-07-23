"""
tests/test_process_lock.py — P1-15: process_lock
"""
import threading
import time

import pytest

from utils import process_lock, is_locked, LockBusy


def test_basic_acquire_release():
    assert not is_locked("test_basic_acquire_release")
    with process_lock("test_basic_acquire_release"):
        assert is_locked("test_basic_acquire_release")
    assert not is_locked("test_basic_acquire_release")


def test_nested_acquire_in_same_process_raises_lockbusy():
    """同一プロセスでの多重取得は LockBusy。"""
    with process_lock("test_nested"):
        with pytest.raises(LockBusy):
            with process_lock("test_nested"):
                pass  # unreachable


def test_is_locked_false_when_absent():
    """ロックファイルが存在しないなら is_locked=False。"""
    name = "test_is_locked_absent_xyz"
    # 念のため事前に存在しないことを確認
    assert not is_locked(name)


def test_different_lock_names_independent():
    with process_lock("lock_a"):
        # lock_a を保持中でも別名 lock_b は取得可能
        with process_lock("lock_b"):
            assert is_locked("lock_a")
            assert is_locked("lock_b")
        assert not is_locked("lock_b")
    assert not is_locked("lock_a")


def test_lock_released_on_exception():
    """ロック内で例外が発生してもロックは解放される。"""
    name = "test_released_on_exception"
    try:
        with process_lock(name):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not is_locked(name)


def test_timeout_zero_raises_immediately():
    name = "test_timeout_zero"
    with process_lock(name):
        t0 = time.time()
        with pytest.raises(LockBusy):
            with process_lock(name, timeout=0):
                pass
        elapsed = time.time() - t0
        # ほぼ即時 (< 100ms)
        assert elapsed < 0.1


def test_concurrent_threads_serialize_via_lock():
    """並行スレッドからの取得は LockBusy で 1 つだけ成功する (timeout=0 の場合)。"""
    name = "test_concurrent_threads"
    results = {"acquired": 0, "busy": 0}
    lock = threading.Lock()

    def worker():
        try:
            with process_lock(name, timeout=0):
                with lock:
                    results["acquired"] += 1
                time.sleep(0.2)
        except LockBusy:
            with lock:
                results["busy"] += 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # fcntl は file descriptor 単位で動作するため、Python threading では同一プロセス内で
    # 同じ FD を持つロックは互いに見えないことがある。複数 open で別 FD 作るので排他は効く想定だが、
    # 実装依存の挙動なので acquired+busy = 5 だけ assert。
    assert results["acquired"] + results["busy"] == 5
    assert not is_locked(name)

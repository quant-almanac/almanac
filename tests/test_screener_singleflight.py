from __future__ import annotations

from contextlib import contextmanager

import screener
from utils import process_lock


def test_cli_skips_a_duplicate_process_without_starting_a_second_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(screener, "run_full_screen", lambda **kwargs: calls.append(kwargs))

    with process_lock("momentum_screener", timeout=0):
        result = screener.main([])

    assert result == 0
    assert calls == []


def test_cli_holds_the_singleflight_lock_while_scanning(monkeypatch):
    events = []

    @contextmanager
    def _lock(name, *, timeout):
        events.append(("lock_enter", name, timeout))
        yield
        events.append(("lock_exit", name, timeout))

    monkeypatch.setattr(screener, "process_lock", _lock)
    monkeypatch.setattr(
        screener,
        "run_full_screen",
        lambda **kwargs: events.append(("run", kwargs)),
    )

    result = screener.main(["--us-only", "--morning"])

    assert result == 0
    assert events == [
        ("lock_enter", "momentum_screener", 0),
        ("run", {
            "us_only": True,
            "jp_only": False,
            "morning": True,
            "ai_comments": False,
        }),
        ("lock_exit", "momentum_screener", 0),
    ]

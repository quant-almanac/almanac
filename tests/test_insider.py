"""Part E-1: insider_tracker openinsider HTML parse."""
from __future__ import annotations

import importlib


def test_module_imports():
    m = importlib.import_module("insider_tracker")
    assert callable(getattr(m, "format_for_prompt", None))


def test_extract_ticker_regex():
    from insider_tracker import _TK_RE  # type: ignore
    m = _TK_RE.search('<a href="/NVDA">NVDA</a> <span onmouseover="...">hover</span>')
    assert m and m.group(1) == "NVDA"


def test_format_returns_string(monkeypatch):
    """format_for_prompt は絶対パスの OUTPUT を見に行くので cwd 変更は効かない。
    戻り値が str なら OK（空文字 or 実データのフォーマット済み）。"""
    from insider_tracker import format_for_prompt
    result = format_for_prompt()
    assert isinstance(result, str)

"""Part E-7: leveraged_decay_monitor の mapping / format 出力。"""
from __future__ import annotations

import importlib


def test_lev_map_sanity():
    m = importlib.import_module("leveraged_decay_monitor")
    for tk, cfg in m.LEV_MAP.items():
        assert "underlying" in cfg
        assert cfg["leverage"] in (2.0, 3.0)


def test_format_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from leveraged_decay_monitor import format_for_prompt
    assert format_for_prompt() == ""


def test_thresholds_reasonable():
    m = importlib.import_module("leveraged_decay_monitor")
    assert m.MIN_HOLD_DAYS >= 30
    assert 0 < m.DECAY_RATIO_TRIG < 1.0

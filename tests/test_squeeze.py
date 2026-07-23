"""Part E-5: squeeze_detector の RSI 反発判定ロジック。"""
from __future__ import annotations

import importlib
import numpy as np
import pandas as pd


def test_rsi_series_shape():
    m = importlib.import_module("squeeze_detector")
    s = pd.Series(np.linspace(10, 50, 30))
    rsi = m._rsi(s, period=14)
    assert len(rsi) == 30
    assert rsi.tail(5).notna().all()


def test_universe_load_survives_no_files(tmp_path, monkeypatch):
    import importlib
    monkeypatch.chdir(tmp_path)
    m = importlib.import_module("squeeze_detector")
    importlib.reload(m)
    uni = m._load_universe()
    # fallback watchlist is non-empty
    assert isinstance(uni, list)
    assert len(uni) >= 3


def test_constants_reasonable():
    m = importlib.import_module("squeeze_detector")
    assert 0 < m.SPF_THRESH < 1
    assert m.D2C_THRESH > 0
    assert m.RSI_REVERSED_TO > m.RSI_REVERSED_FROM

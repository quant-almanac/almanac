"""Part E-6: earnings_proximity_manager のビジネス日計算 & format 出力。"""
from __future__ import annotations

import importlib
from datetime import date, timedelta


def test_business_days_future():
    m = importlib.import_module("earnings_proximity_manager")
    # 来週の同曜日 → 5 営業日
    tgt = date.today() + timedelta(days=7)
    n = m._business_days_until(tgt)
    assert 4 <= n <= 6


def test_business_days_past():
    m = importlib.import_module("earnings_proximity_manager")
    n = m._business_days_until(date.today() - timedelta(days=1))
    assert n == -1


def test_thresholds():
    m = importlib.import_module("earnings_proximity_manager")
    assert 0 < m.DAMAGE_PCT_THRESHOLD < 0.1
    assert 0 < m.IMPL_MOVE_FUDGE < 1.0
    assert 0 < m.BEAT_RATE_FORCE_TRIM <= 1.0

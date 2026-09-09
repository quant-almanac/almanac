"""risk_policy.loss_guard_state(): 日次/30日を独立評価する (2026-09 レビュー S1b)。

再現された欠陥: 旧実装は daily/rolling のどちらかが None なら即
data_confidence_caution を返す both-or-neither 早期 return だった。

    daily=0.0,  rolling=-0.13 -> stage_3   （確認済みの30日制御）
    daily=None, rolling=-0.13 -> data_confidence_caution （stage_3 が消えた）

EOD 評価が失敗して daily が None になっただけで、判明している 30日 -13% という
実際の制御が失われていた。日次と30日は独立に評価し、判明している側の制約は
必ず維持する。
"""
from __future__ import annotations

import pytest

from risk_policy import loss_guard_state


def test_known_rolling_stage3_survives_unknown_daily():
    """再現の核心: daily=None でも rolling の stage_3 は消えない。"""
    result = loss_guard_state(daily_pnl_decimal=None, rolling_30_pnl_decimal=-0.13)
    assert result["loss_guard_stage"] == "stage_3"
    assert result["new_risk_allowed"] is False


def test_known_rolling_stage2_survives_unknown_daily():
    result = loss_guard_state(daily_pnl_decimal=None, rolling_30_pnl_decimal=-0.10)
    assert result["loss_guard_stage"] == "stage_2"


def test_known_rolling_stage1_survives_unknown_daily():
    result = loss_guard_state(daily_pnl_decimal=None, rolling_30_pnl_decimal=-0.07)
    assert result["loss_guard_stage"] == "stage_1"


def test_known_daily_block_survives_unknown_rolling():
    result = loss_guard_state(daily_pnl_decimal=-0.05, rolling_30_pnl_decimal=None)
    assert result["loss_guard_stage"] == "daily_block"
    assert result["new_risk_allowed"] is False


def test_both_unknown_is_data_confidence_caution():
    result = loss_guard_state(daily_pnl_decimal=None, rolling_30_pnl_decimal=None)
    assert result["loss_guard_stage"] == "data_confidence_caution"
    assert result["new_risk_allowed"] is None
    assert result["trading_allowed"] is True


def test_rolling_clean_but_daily_unknown_is_caution_not_ok():
    """30日が閾値内でも、日次が不明な間は「全問題なし」と主張しない。"""
    result = loss_guard_state(daily_pnl_decimal=None, rolling_30_pnl_decimal=0.01)
    assert result["loss_guard_stage"] == "data_confidence_caution"


def test_daily_clean_but_rolling_unknown_is_caution_not_ok():
    result = loss_guard_state(daily_pnl_decimal=0.005, rolling_30_pnl_decimal=None)
    assert result["loss_guard_stage"] == "data_confidence_caution"


def test_both_known_and_clean_is_ok():
    result = loss_guard_state(daily_pnl_decimal=0.005, rolling_30_pnl_decimal=0.01)
    assert result["loss_guard_stage"] == "ok"
    assert result["new_risk_allowed"] is True


def test_both_known_daily_block_and_rolling_clean():
    result = loss_guard_state(daily_pnl_decimal=-0.05, rolling_30_pnl_decimal=0.01)
    assert result["loss_guard_stage"] == "daily_block"


def test_stage_thresholds_are_unchanged():
    """閾値そのもの（日次-3% / 30日-6/-9/-12%）は変更しない。"""
    assert loss_guard_state(daily_pnl_decimal=-0.029, rolling_30_pnl_decimal=0)["loss_guard_stage"] == "ok"
    assert loss_guard_state(daily_pnl_decimal=-0.03, rolling_30_pnl_decimal=0)["loss_guard_stage"] == "daily_block"
    assert loss_guard_state(daily_pnl_decimal=0, rolling_30_pnl_decimal=-0.059)["loss_guard_stage"] == "ok"
    assert loss_guard_state(daily_pnl_decimal=0, rolling_30_pnl_decimal=-0.06)["loss_guard_stage"] == "stage_1"
    assert loss_guard_state(daily_pnl_decimal=0, rolling_30_pnl_decimal=-0.09)["loss_guard_stage"] == "stage_2"
    assert loss_guard_state(daily_pnl_decimal=0, rolling_30_pnl_decimal=-0.12)["loss_guard_stage"] == "stage_3"

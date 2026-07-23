"""T6: CVaR ヒストリカル + unstable フラグ"""
import numpy as np
import pandas as pd
import pytest
import risk_engine as re_


def test_cvar_historical_normal_dist():
    """正規分布で理論 CVaR と比較（誤差 20% 以内 — 有限サンプル揺らぎ許容）"""
    np.random.seed(42)
    returns = pd.Series(np.random.normal(0, 0.01, 2000))
    res = re_.calculate_cvar(returns, confidence=0.95)
    # 正規分布 CVaR95 ≈ σ × φ(z95)/(1-0.95) ≈ 0.01 × 2.063 ≈ 0.02063
    theoretical = 0.01 * 2.063
    assert abs(res['cvar_pct'] - theoretical) / theoretical < 0.20
    assert res['method'] == 'historical'
    assert not res['cvar_unstable']   # 2000 件なら tail 100 件


def test_cvar_unstable_on_short_series():
    """短いシリーズ（tail_observations < 10）で unstable=True"""
    np.random.seed(1)
    returns = pd.Series(np.random.normal(0, 0.01, 50))   # tail = ~2-3
    res = re_.calculate_cvar(returns, confidence=0.95)
    if 'error' not in res:
        # tail < 10 で unstable フラグが立つ
        if res['tail_observations'] < 10:
            assert res['cvar_unstable'] is True


def test_cvar_keys():
    np.random.seed(0)
    returns = pd.Series(np.random.normal(0, 0.01, 500))
    res = re_.calculate_cvar(returns, portfolio_value=10_000_000)
    for k in ('cvar_pct', 'cvar_cf_pct', 'cvar_jpy', 'var_pct', 'var_hist_pct',
              'tail_observations', 'cvar_unstable', 'confidence', 'method'):
        assert k in res


def test_cvar_rejects_short_input():
    returns = pd.Series([0.01, -0.02, 0.005])
    res = re_.calculate_cvar(returns)
    assert 'error' in res


def test_var_cornish_fisher_never_returns_negative_loss():
    """Positive-skewed realized trades can make raw CF VaR negative; public VaR is a loss magnitude."""
    returns = pd.Series(
        [-0.86, -0.448, -0.367, -0.087, -0.086, -0.064]
        + [0.0, 0.004, 0.006, 0.012, 0.02, 0.04, 0.08, 0.12] * 5
        + [0.519, 1.12458]
    )
    res = re_.calculate_var_cornish_fisher(returns, confidence=0.95)
    assert res["var_pct"] >= 0
    assert res["var_jpy"] >= 0
    assert "raw_var_pct" in res

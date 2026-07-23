"""T19: OLS alpha/beta/R² on synthetic data"""
import numpy as np
import pytest

import factor_attribution as fa


def test_ols_recovers_known_betas():
    np.random.seed(42)
    n = 60
    mkt = np.random.randn(n) * 0.04
    smb = np.random.randn(n) * 0.02
    hml = np.random.randn(n) * 0.02
    y = 0.002 + 0.8 * mkt + 0.2 * smb + 0.1 * hml + np.random.randn(n) * 0.005
    X = np.column_stack([mkt, smb, hml])
    res = fa.run_ols(y, X)
    assert 0.75 < res['betas'][0] < 0.85
    assert res['r_squared'] > 0.8
    assert res['dof'] == n - 3 - 1


def test_ols_schema_keys():
    np.random.seed(1)
    y = np.random.randn(30)
    X = np.random.randn(30, 2)
    res = fa.run_ols(y, X)
    for key in ('alpha', 'alpha_tstat', 'betas', 'beta_tstats', 'r_squared', 'dof', 'n_obs'):
        assert key in res

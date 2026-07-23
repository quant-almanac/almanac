"""Extended tests for factor_attribution.py.

Existing tests (test_factor_attribution.py):
  - run_ols: beta recovery on synthetic data, schema keys

New coverage (no network / no yfinance calls):
  - run_ols:  alpha recovery, perfect fit R²=1, dof formula, t-stats=0 when dof≤0
  - _estimate_portfolio_monthly_returns: empty holdings, skip-tickers, weight normalisation
  - attribution_monthly error paths: y=None, len<12, panel failed
  - attribution_monthly verdict: all 4 branches (positive_alpha, neutral, negative_alpha, uncertain)
  - attribution_monthly: result schema, persist=False leaves no file
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import factor_attribution as fa  # noqa: E402


# ---------------------------------------------------------------------------
# run_ols — extended pure-math tests
# ---------------------------------------------------------------------------


def test_run_ols_recovers_alpha() -> None:
    """y = 0.003 + 1.0*x + tiny noise → alpha ≈ 0.003."""
    np.random.seed(7)
    n = 60
    x = np.random.randn(n) * 0.04
    y = 0.003 + 1.0 * x + np.random.randn(n) * 0.001
    res = fa.run_ols(y, x.reshape(-1, 1))
    assert pytest.approx(0.003, abs=0.001) == res["alpha"]


def test_run_ols_perfect_fit_r2_equals_one() -> None:
    """Exact linear relationship → R² = 1.0."""
    x = np.linspace(0, 1, 40)
    y = 0.5 + 2.0 * x          # no noise
    res = fa.run_ols(y, x.reshape(-1, 1))
    assert res["r_squared"] == pytest.approx(1.0, abs=1e-8)


def test_run_ols_n_obs_matches_input() -> None:
    y = np.random.randn(50)
    X = np.random.randn(50, 3)
    res = fa.run_ols(y, X)
    assert res["n_obs"] == 50


def test_run_ols_dof_formula() -> None:
    """dof = n - k - 1  (k = number of factors)."""
    n, k = 48, 4
    y = np.random.randn(n)
    X = np.random.randn(n, k)
    res = fa.run_ols(y, X)
    assert res["dof"] == n - k - 1


def test_run_ols_t_stats_zero_when_dof_le_zero() -> None:
    """When n ≤ k+1 degrees-of-freedom fall to 0 → t-stats array of zeros."""
    n, k = 5, 4          # dof = 5 - 4 - 1 = 0
    y = np.random.randn(n)
    X = np.random.randn(n, k)
    res = fa.run_ols(y, X)
    assert res["dof"] == 0
    assert all(t == pytest.approx(0.0) for t in res["beta_tstats"])


def test_run_ols_betas_length_matches_factors() -> None:
    k = 5
    y = np.random.randn(60)
    X = np.random.randn(60, k)
    res = fa.run_ols(y, X)
    assert len(res["betas"]) == k
    assert len(res["beta_tstats"]) == k


# ---------------------------------------------------------------------------
# _estimate_portfolio_monthly_returns (monkeypatched _fetch_monthly_returns)
# ---------------------------------------------------------------------------


def _make_series(n: int = 36):
    """Return a dummy pandas Series of length n with DatetimeIndex."""
    import pandas as pd
    idx = pd.date_range("2023-01-01", periods=n, freq="MS")
    return pd.Series(np.random.randn(n) * 0.03, index=idx)


def test_estimate_returns_none_for_empty_holdings(monkeypatch) -> None:
    monkeypatch.setattr(fa, "_fetch_monthly_returns", lambda t, m=36: _make_series())
    result = fa._estimate_portfolio_monthly_returns({})
    assert result is None


def test_estimate_returns_none_when_all_skip_tickers(monkeypatch) -> None:
    """Holdings consisting only of SKIP tickers → no weight → None."""
    monkeypatch.setattr(fa, "_fetch_monthly_returns", lambda t, m=36: _make_series())
    holdings = {
        "SLIM_SP500": {"ticker": "SLIM_SP500", "shares": 1000, "entry_price": 15000.0, "currency": "JPY"},
        "CASH_JPY":   {"ticker": "CASH_JPY",   "shares": 500000, "entry_price": 1.0,    "currency": "JPY"},
    }
    result = fa._estimate_portfolio_monthly_returns(holdings)
    assert result is None


def test_estimate_returns_none_when_fetch_fails(monkeypatch) -> None:
    """If _fetch_monthly_returns always returns None, output is None."""
    monkeypatch.setattr(fa, "_fetch_monthly_returns", lambda t, m=36: None)
    holdings = {
        "NVDA": {"ticker": "NVDA", "shares": 10, "entry_price": 500.0, "currency": "USD"},
        "AAPL": {"ticker": "AAPL", "shares": 5,  "entry_price": 200.0, "currency": "USD"},
    }
    result = fa._estimate_portfolio_monthly_returns(holdings)
    assert result is None


def test_estimate_returns_series_with_valid_holdings(monkeypatch) -> None:
    """Two valid holdings → returns a non-empty Series."""
    monkeypatch.setattr(fa, "_fetch_monthly_returns", lambda t, m=36: _make_series(24))
    holdings = {
        "NVDA": {"ticker": "NVDA", "shares": 10, "entry_price": 500.0, "currency": "USD"},
        "AAPL": {"ticker": "AAPL", "shares": 5,  "entry_price": 200.0, "currency": "USD"},
    }
    result = fa._estimate_portfolio_monthly_returns(holdings)
    assert result is not None
    assert len(result) > 0


def test_estimate_skips_zero_value_positions(monkeypatch) -> None:
    """Shares=0 contributes zero weight and should not appear in result."""
    calls: list[str] = []

    def fake_fetch(ticker, months=36):
        calls.append(ticker)
        return _make_series()

    monkeypatch.setattr(fa, "_fetch_monthly_returns", fake_fetch)
    holdings = {
        "NVDA": {"ticker": "NVDA", "shares": 10, "entry_price": 500.0, "currency": "USD"},
        "DEAD": {"ticker": "DEAD", "shares": 0,  "entry_price": 100.0, "currency": "USD"},
    }
    fa._estimate_portfolio_monthly_returns(holdings)
    assert "DEAD" not in calls


# ---------------------------------------------------------------------------
# attribution_monthly error paths
# ---------------------------------------------------------------------------


def test_attribution_error_when_portfolio_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(fa, "_estimate_portfolio_monthly_returns", lambda h, m: None)
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert "error" in result


def test_attribution_error_when_too_few_months(monkeypatch) -> None:
    """Fewer than 12 observations in y → error."""
    import pandas as pd
    short = pd.Series(np.random.randn(10) * 0.02,
                      index=pd.date_range("2025-01-01", periods=10, freq="MS"))
    monkeypatch.setattr(fa, "_estimate_portfolio_monthly_returns", lambda h, m: short)
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert "error" in result


def test_attribution_error_when_panel_empty(monkeypatch) -> None:
    """build_factor_panel returns {} → error."""
    import pandas as pd
    y = pd.Series(np.random.randn(24), index=pd.date_range("2024-01-01", periods=24, freq="MS"))
    monkeypatch.setattr(fa, "_estimate_portfolio_monthly_returns", lambda h, m: y)
    monkeypatch.setattr(fa, "build_factor_panel", lambda m: {})
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert "error" in result


def test_attribution_error_when_overlap_too_small(monkeypatch) -> None:
    """After aligning y and factors fewer than 12 months remain → error."""
    import pandas as pd
    # y: 24 months Jan 2022 – Dec 2023
    y = pd.Series(np.ones(24), index=pd.date_range("2022-01-01", periods=24, freq="MS"))
    # factor: only 10 months with no overlap with y
    factor_idx = pd.date_range("2024-01-01", periods=10, freq="MS")
    factor_df = pd.DataFrame({"MKT": np.ones(10)}, index=factor_idx)
    monkeypatch.setattr(fa, "_estimate_portfolio_monthly_returns", lambda h, m: y)
    monkeypatch.setattr(fa, "build_factor_panel", lambda m: {"df": factor_df, "months": 10})
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert "error" in result


# ---------------------------------------------------------------------------
# attribution_monthly — verdict branches (monkeypatched run_ols)
# ---------------------------------------------------------------------------


def _setup_valid_data(monkeypatch, *, n: int = 24) -> None:
    """Set up valid y and factor panel with n months of aligned data."""
    import pandas as pd
    idx = pd.date_range("2024-01-01", periods=n, freq="MS")
    y = pd.Series(np.random.randn(n) * 0.02, index=idx)
    df = pd.DataFrame({"MKT": np.random.randn(n) * 0.04}, index=idx)
    monkeypatch.setattr(fa, "_estimate_portfolio_monthly_returns", lambda h, m: y)
    monkeypatch.setattr(fa, "build_factor_panel", lambda m: {"df": df, "months": n})


def test_verdict_positive_alpha(monkeypatch) -> None:
    """alpha>0 AND alpha_tstat>2 → positive_alpha."""
    _setup_valid_data(monkeypatch)
    monkeypatch.setattr(fa, "run_ols", lambda y, X: {
        "alpha": 0.005, "alpha_tstat": 2.5,
        "betas": [0.8], "beta_tstats": [5.0],
        "r_squared": 0.85, "dof": 20, "n_obs": 24,
    })
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert result["verdict"] == "positive_alpha"


def test_verdict_neutral(monkeypatch) -> None:
    """|alpha_tstat| < 1 → neutral."""
    _setup_valid_data(monkeypatch)
    monkeypatch.setattr(fa, "run_ols", lambda y, X: {
        "alpha": 0.001, "alpha_tstat": 0.4,
        "betas": [0.9], "beta_tstats": [6.0],
        "r_squared": 0.80, "dof": 20, "n_obs": 24,
    })
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert result["verdict"] == "neutral"


def test_verdict_negative_alpha(monkeypatch) -> None:
    """alpha<0 AND alpha_tstat<-1 → negative_alpha."""
    _setup_valid_data(monkeypatch)
    monkeypatch.setattr(fa, "run_ols", lambda y, X: {
        "alpha": -0.004, "alpha_tstat": -1.8,
        "betas": [1.1], "beta_tstats": [7.0],
        "r_squared": 0.75, "dof": 20, "n_obs": 24,
    })
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert result["verdict"] == "negative_alpha"


def test_verdict_uncertain(monkeypatch) -> None:
    """alpha>0 but alpha_tstat between 1 and 2 → uncertain."""
    _setup_valid_data(monkeypatch)
    monkeypatch.setattr(fa, "run_ols", lambda y, X: {
        "alpha": 0.003, "alpha_tstat": 1.5,
        "betas": [0.7], "beta_tstats": [4.0],
        "r_squared": 0.70, "dof": 20, "n_obs": 24,
    })
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert result["verdict"] == "uncertain"


# ---------------------------------------------------------------------------
# attribution_monthly — result schema and persist=False
# ---------------------------------------------------------------------------


def test_attribution_result_has_required_keys(monkeypatch) -> None:
    _setup_valid_data(monkeypatch)
    monkeypatch.setattr(fa, "run_ols", lambda y, X: {
        "alpha": 0.002, "alpha_tstat": 1.0,
        "betas": [0.8], "beta_tstats": [3.0],
        "r_squared": 0.65, "dof": 20, "n_obs": 24,
    })
    result = fa.attribution_monthly(holdings={}, persist=False)
    for key in ("alpha", "alpha_annual", "alpha_tstat", "betas", "beta_tstats",
                "r_squared", "n_months", "dof", "verdict", "as_of", "factors_used"):
        assert key in result, f"missing key: {key}"


def test_attribution_persist_false_writes_no_file(monkeypatch, tmp_path) -> None:
    """persist=False must not write factor_attribution.json."""
    monkeypatch.setattr(fa, "ATTR_PATH", tmp_path / "factor_attribution.json")
    _setup_valid_data(monkeypatch)
    monkeypatch.setattr(fa, "run_ols", lambda y, X: {
        "alpha": 0.001, "alpha_tstat": 0.5,
        "betas": [0.9], "beta_tstats": [4.0],
        "r_squared": 0.60, "dof": 20, "n_obs": 24,
    })
    fa.attribution_monthly(holdings={}, persist=False)
    assert not (tmp_path / "factor_attribution.json").exists()


def test_attribution_alpha_annual_is_monthly_times_12(monkeypatch) -> None:
    _setup_valid_data(monkeypatch)
    monkeypatch.setattr(fa, "run_ols", lambda y, X: {
        "alpha": 0.003, "alpha_tstat": 1.0,
        "betas": [0.9], "beta_tstats": [3.0],
        "r_squared": 0.70, "dof": 20, "n_obs": 24,
    })
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert result["alpha_annual"] == pytest.approx(result["alpha"] * 12, abs=1e-6)


def test_attribution_betas_dict_keyed_by_factor_name(monkeypatch) -> None:
    """betas dict keys must match factors_used list."""
    _setup_valid_data(monkeypatch)
    monkeypatch.setattr(fa, "run_ols", lambda y, X: {
        "alpha": 0.001, "alpha_tstat": 0.5,
        "betas": [0.8], "beta_tstats": [2.0],
        "r_squared": 0.60, "dof": 20, "n_obs": 24,
    })
    result = fa.attribution_monthly(holdings={}, persist=False)
    assert set(result["betas"].keys()) == set(result["factors_used"])

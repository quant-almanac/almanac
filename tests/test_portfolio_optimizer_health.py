import math

import numpy as np
import pandas as pd

import portfolio_optimizer as optimizer


def test_implausible_max_sharpe_is_not_allocation_eligible():
    health = optimizer.assess_allocation_health({
        "method": "max_sharpe",
        "weights": {"AAA": 0.5, "BBB": 0.5},
        "expected_return": 0.5144,
        "volatility": 0.1125,
        "sharpe": 4.1737,
    })

    assert health["health"] == "invalid"
    assert health["allocation_eligible"] is False
    assert {"expected_return_out_of_range", "sharpe_out_of_range"} <= set(health["health_reasons"])


def test_fallback_and_missing_volatility_are_never_allocation_eligible():
    health = optimizer.assess_allocation_health({
        "method": "equal_weight_fallback",
        "weights": {"AAA": 0.5, "BBB": 0.5},
        "error": "MeanCVaR import failed",
        "expected_return": None,
        "volatility": None,
        "sharpe": None,
    })

    assert health["health"] == "invalid"
    assert health["allocation_eligible"] is False
    assert "optimizer_fallback" in health["health_reasons"]
    assert "volatility_missing" in health["health_reasons"]


def test_equal_risk_exposes_finite_metrics_and_can_be_eligible():
    rng = np.random.default_rng(7)
    returns = pd.DataFrame(rng.normal(0.0002, 0.01, size=(252, 3)), columns=["AAA", "BBB", "CCC"])

    result = optimizer.optimize_pypfopt(returns, method="equal_risk")
    health = optimizer.assess_allocation_health(result)

    assert math.isfinite(result["volatility"])
    assert math.isfinite(result["sharpe"])
    assert health["allocation_eligible"] is True


def test_run_optimization_does_not_recommend_invalid_regime_preference(monkeypatch):
    rng = np.random.default_rng(7)
    returns = pd.DataFrame(rng.normal(0.0002, 0.01, size=(252, 3)), columns=["AAA", "BBB", "CCC"])
    monkeypatch.setattr(optimizer, "_load_holdings_tickers", lambda: list(returns.columns))
    monkeypatch.setattr(optimizer, "_load_regime", lambda: "A_強気")
    monkeypatch.setattr(optimizer, "load_returns", lambda *_args, **_kwargs: returns)
    monkeypatch.setattr(optimizer, "compute_risk_parity_weights", lambda **_kwargs: {})

    result = optimizer.run_optimization(methods=["max_sharpe", "equal_risk"])

    assert result["results"]["max_sharpe"]["allocation_eligible"] is False
    assert result["recommended"] == "equal_risk"

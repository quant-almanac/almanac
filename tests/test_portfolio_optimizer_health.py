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


class TestReadOnlyCovarianceIsHandled:
    """returns.cov().values が read-only な view を返すケース。

    pandas は内部表現によって .values が読み取り専用の numpy array を
    返すことがある。以前は cov_arr += ... で直接 in-place 演算しており、
    numpy 2.5.x でこれを踏むと ValueError: output array is read-only で
    落ちていた (レビューで CI 初回のテスト実行時に実測)。
    今は .values.copy() で明示的にコピーしてから足すので、read-only な
    ソースでも壊れないはず。
    """

    def _read_only_returns(self) -> pd.DataFrame:
        """.cov() の結果が read-only になりやすい形の returns を作る。

        直接 .cov() の出力を read-only にするのは内部実装依存で不安定
        なので、ここでは _read_only_cov_values を使って
        「cov_df.values が read-only」という状況そのものを再現する。
        """
        rng = np.random.default_rng(0)
        dates = pd.date_range("2026-01-01", periods=60, freq="B")
        return pd.DataFrame(
            rng.normal(0, 0.01, size=(60, 3)),
            index=dates, columns=["AAA", "BBB", "CCC"])

    def test_optimize_pypfopt_survives_a_read_only_covariance_view(self, monkeypatch):
        returns = self._read_only_returns()

        # ⚠️ .cov() 単体の戻り値だけを read-only にしても、その後の
        # `* 252` (numpy の通常の算術演算) が新しい書き込み可能な配列を
        # 作ってしまい、実際に踏んだ状況を再現できない。DataFrame.values
        # プロパティそのものを「常に read-only なコピーを返す」よう
        # 差し替えることで、cov_df.values が read-only という状況を
        # (.cov() の内部実装に依存せず) 確実に再現する。
        real_values = pd.DataFrame.values

        def _readonly_values(self):
            arr = real_values.fget(self).copy()
            arr.flags.writeable = False
            return arr

        monkeypatch.setattr(pd.DataFrame, "values", property(_readonly_values))

        cov_check = (returns.cov() * 252)
        assert cov_check.values.flags.writeable is False, (
            "fixture が read-only を再現できていない")

        result = optimizer.optimize_pypfopt(returns)
        assert isinstance(result, dict)
        assert result.get("error") is None or "read-only" not in str(result.get("error"))

from __future__ import annotations

import numpy as np
import pandas as pd

import analyst
import risk_engine


def _write_fake_ohlcv(base_dir, ticker: str, n: int = 100) -> None:
    ohlcv_dir = base_dir / "data" / "ohlcv"
    ohlcv_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    prices = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    pd.DataFrame({"Close": prices}).to_parquet(ohlcv_dir / f"{ticker}.parquet")


def test_heading_is_model_neutral_not_hardcoded_ginn(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _write_fake_ohlcv(tmp_path, "FAKE1")
    monkeypatch.setattr(
        risk_engine,
        "estimate_gjr_garch",
        lambda returns, use_ginn=True: {
            "forecast_vol": 0.25,
            "model": "GJR-GARCH(1,1)-skewt",
        },
    )

    prompt, values, models = analyst._compute_ginn_vol(["FAKE1"])

    assert "GINN推定" not in prompt
    assert values["FAKE1"] == 25.0
    assert models["FAKE1"] == "GJR-GARCH(1,1)-skewt"


def test_model_dict_reflects_real_ginn_adoption(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _write_fake_ohlcv(tmp_path, "FAKE2", n=300)
    monkeypatch.setattr(
        risk_engine,
        "estimate_gjr_garch",
        lambda returns, use_ginn=True: {
            "forecast_vol": 0.30,
            "model": "GINN+GJR-GARCH",
        },
    )

    prompt, _, models = analyst._compute_ginn_vol(["FAKE2"])

    assert models["FAKE2"] == "GINN+GJR-GARCH"
    assert "GINN+GJR-GARCH" in prompt


def test_empty_tickers_returns_three_empty_values(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    assert analyst._compute_ginn_vol([]) == ("", {}, {})

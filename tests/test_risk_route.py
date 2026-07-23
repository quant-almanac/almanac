import json

import pytest

from api.routes import risk as risk_module


def test_currency_exposure_counts_non_jpy_as_foreign() -> None:
    snapshot = {
        "total_jpy": 1_000_000,
        "currency_breakdown": {
            "USD": {"value_jpy": 600_000, "ratio": 0.6},
            "EUR": {"value_jpy": 200_000, "ratio": 0.2},
            "JPY": {"value_jpy": 200_000, "ratio": 0.2},
        },
    }

    exposure = risk_module._currency_exposure_from_snapshot(snapshot)

    assert exposure["total_jpy"] == 1_000_000
    assert exposure["foreign_value_jpy"] == 800_000
    assert exposure["foreign_ratio"] == pytest.approx(0.8)
    assert exposure["foreign_pct"] == pytest.approx(80.0)
    assert exposure["jpy_value_jpy"] == 200_000
    assert exposure["by_currency"]["USD"]["pct"] == pytest.approx(60.0)
    assert exposure["by_currency"]["EUR"]["pct"] == pytest.approx(20.0)


def test_currency_exposure_surfaces_unallocated_total_as_unknown() -> None:
    snapshot = {
        "total_jpy": 1_000_000,
        "currency_breakdown": {
            "USD": {"value_jpy": 300_000, "ratio": 0.3},
            "JPY": {"value_jpy": 200_000, "ratio": 0.2},
        },
    }

    exposure = risk_module._currency_exposure_from_snapshot(snapshot)

    assert exposure["foreign_value_jpy"] == 300_000
    assert exposure["foreign_pct"] == pytest.approx(30.0)
    assert exposure["jpy_value_jpy"] == 200_000
    assert exposure["unknown_value_jpy"] == 500_000
    assert exposure["unknown_pct"] == pytest.approx(50.0)


def test_calc_risk_keeps_currency_exposure_when_ohlcv_is_insufficient(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "data" / "ohlcv").mkdir(parents=True)
    (tmp_path / "holdings.json").write_text(
        json.dumps({
            "AAPL": {
                "ticker": "AAPL",
                "shares": 1,
                "entry_price": 100,
                "currency": "USD",
            }
        }),
        encoding="utf-8",
    )
    (tmp_path / "account.json").write_text(
        json.dumps({"fx_rate_usdjpy": 150.0}), encoding="utf-8"
    )
    monkeypatch.setattr(risk_module, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        risk_module,
        "_load_portfolio_snapshot_for_risk",
        lambda: {
            "total_jpy": 1_000_000,
            "currency_breakdown": {
                "USD": {"value_jpy": 750_000, "ratio": 0.75},
                "JPY": {"value_jpy": 250_000, "ratio": 0.25},
            },
        },
    )

    risk = risk_module._calc_risk()

    assert risk["sample_size"] == 0
    assert risk["currency_exposure"]["foreign_pct"] == pytest.approx(75.0)
    assert risk["currency_exposure"]["jpy_pct"] == pytest.approx(25.0)

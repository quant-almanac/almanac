import sys
import types
from datetime import datetime

import pandas as pd
import pytest

import macro_fetcher


def _install_fake_fred(monkeypatch, cpi: pd.Series) -> None:
    class FakeFred:
        def __init__(self, api_key):
            self.api_key = api_key

        def get_series(self, series_id):
            if series_id == "CPIAUCNS":
                return cpi
            return pd.Series(
                [4.0],
                index=pd.DatetimeIndex(["2026-07-01"]),
                dtype=float,
            )

    monkeypatch.setitem(sys.modules, "fredapi", types.SimpleNamespace(Fred=FakeFred))
    monkeypatch.setenv("FRED_API_KEY", "test-key")


def test_cpi_yoy_uses_calendar_matched_prior_year_not_row_position(monkeypatch):
    index = pd.date_range("2025-05-01", "2026-06-01", freq="MS")
    index = index[index != pd.Timestamp("2025-10-01")]
    values = pd.Series(
        [100 + i for i in range(len(index))],
        index=index,
        dtype=float,
    )
    values.loc[pd.Timestamp("2025-06-01")] = 100.0
    values.loc[pd.Timestamp("2026-06-01")] = 103.5
    _install_fake_fred(monkeypatch, values)

    result = macro_fetcher._fetch_fred()

    assert result["cpi_yoy"] == pytest.approx(3.5)
    assert result["series_provenance"]["cpi_yoy"] == {
        "source": "FRED:CPIAUCNS",
        "observation_date": "2026-06-01",
        "comparison_date": "2025-06-01",
        "status": "ok",
    }
    assert result["series_provenance"]["cpi_index"]["source"] == "FRED:CPIAUCNS"


def test_cpi_yoy_is_none_when_exact_prior_year_observation_is_missing(monkeypatch):
    values = pd.Series(
        [100.0, 103.5],
        index=pd.DatetimeIndex(["2025-05-01", "2026-06-01"]),
    )
    _install_fake_fred(monkeypatch, values)

    result = macro_fetcher._fetch_fred()

    assert result["cpi_yoy"] is None
    assert (
        result["series_provenance"]["cpi_yoy"]["status"]
        == "prior_year_observation_missing"
    )


def test_legacy_cache_without_schema_or_cpi_provenance_is_rejected(monkeypatch, tmp_path):
    cache = tmp_path / "macro_state.json"
    cache.write_text(
        '{"cached_at": "%s", "cpi_yoy": 3.7265}' % datetime.now().isoformat(),
        encoding="utf-8",
    )
    monkeypatch.setattr(macro_fetcher, "CACHE_FILE", cache)

    assert macro_fetcher._load_cache() == {}


def test_current_cache_contract_is_reused(monkeypatch, tmp_path):
    cache = tmp_path / "macro_state.json"
    cache.write_text(
        """{
          "schema_version": 3,
          "cached_at": "%s",
          "cpi_yoy": 3.5,
          "series_provenance": {
            "cpi_yoy": {"source": "FRED:CPIAUCNS", "status": "ok"}
          }
        }""" % datetime.now().isoformat(),
        encoding="utf-8",
    )
    monkeypatch.setattr(macro_fetcher, "CACHE_FILE", cache)

    assert macro_fetcher._load_cache()["cpi_yoy"] == 3.5


def test_rate_series_include_real_yield_breakeven_and_bps_changes(monkeypatch):
    index = pd.bdate_range("2026-06-01", periods=25)

    class FakeFred:
        def __init__(self, api_key):
            self.api_key = api_key

        def get_series(self, series_id):
            if series_id == "CPIAUCNS":
                return pd.Series(
                    [100.0, 103.0],
                    index=pd.DatetimeIndex(["2025-06-01", "2026-06-01"]),
                )
            if series_id == "DGS10":
                return pd.Series([4.0 + i * 0.01 for i in range(25)], index=index)
            if series_id == "DFII10":
                return pd.Series([1.5 + i * 0.005 for i in range(25)], index=index)
            if series_id == "DGS2":
                return pd.Series([3.9] * 25, index=index)
            if series_id == "DGS3MO":
                return pd.Series([3.7] * 25, index=index)
            if series_id == "T10YIE":
                return pd.Series([2.3] * 25, index=index)
            return pd.Series([4.0] * 25, index=index)

    monkeypatch.setitem(sys.modules, "fredapi", types.SimpleNamespace(Fred=FakeFred))
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    result = macro_fetcher._fetch_fred()

    assert result["yield_10y_change_5d_bps"] == pytest.approx(5.0)
    assert result["yield_10y_change_20d_bps"] == pytest.approx(20.0)
    assert result["real_yield_10y_change_20d_bps"] == pytest.approx(10.0)
    assert result["yield_spread_10y_2y"] == pytest.approx(0.34)
    assert result["yield_spread_10y_3m"] == pytest.approx(0.54)
    assert result["series_provenance"]["real_yield_10y"]["source"] == "FRED:DFII10"
    assert result["series_provenance"]["breakeven_10y"]["source"] == "FRED:T10YIE"

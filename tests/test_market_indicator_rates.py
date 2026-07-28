import sys
import types

import pandas as pd

from analyst import data_gatherer as dg


def test_irx_is_three_month_and_two_year_comes_from_fred(monkeypatch):
    index = pd.bdate_range("2026-06-01", periods=22)

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, period):
            base = 4.5 if self.symbol == "^TNX" else 3.7
            if self.symbol not in {"^TNX", "^IRX"}:
                base = 20.0
            return pd.DataFrame(
                {"Close": [base + i * 0.01 for i in range(len(index))]},
                index=index,
            )

    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        types.SimpleNamespace(Ticker=FakeTicker),
    )
    fake_macro = types.ModuleType("macro_fetcher")
    fake_macro.get_macro_context = lambda: {
        "yield_10y": 4.71,
        "yield_2y": 4.31,
        "yield_3m": 3.81,
        "real_yield_10y": 2.03,
        "breakeven_10y": 2.68,
        "yield_10y_change_5d_bps": 12.0,
        "yield_10y_change_20d_bps": 35.0,
        "real_yield_10y_change_5d_bps": 8.0,
        "real_yield_10y_change_20d_bps": 22.0,
        "hy_oas_bps": 315.0,
        "series_provenance": {
            "yield_2y": {"source": "FRED:DGS2", "observation_date": "2026-07-27"},
            "real_yield_10y": {
                "source": "FRED:DFII10",
                "observation_date": "2026-07-27",
            },
        },
    }
    monkeypatch.setitem(sys.modules, "macro_fetcher", fake_macro)

    result = dg.gather_market_indicators()

    assert result["us3m_yield"]["label"] == "米3カ月金利(%)"
    assert result["us2y_yield"]["value"] == 4.31
    assert result["us2y_yield"]["source"] == "FRED:DGS2"
    assert result["yield_curve_spread"] == 0.4
    assert "10Y-2Y" in result["yield_curve_status"]
    assert result["yield_curve_spread_10y_3m"] == 0.9
    assert result["real10y_yield"]["source"] == "FRED:DFII10"
    assert result["yield_10y_change_20d_bps"] == 35.0

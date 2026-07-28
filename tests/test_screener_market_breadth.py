import pandas as pd

import screener


def _history(last: float, *, n: int = 220, slope: float = 0.1):
    values = [last - slope * (n - 1 - i) for i in range(n)]
    return pd.DataFrame({"Close": values})


def test_breadth_uses_explicit_eligible_denominators():
    breadth = screener._compute_market_breadth({
        "AAPL": _history(100, slope=0.1),
        "MSFT": _history(100, slope=-0.1),
        "7203.T": _history(3000, slope=1.0),
        # Enough for MA50, not MA200.
        "6758.T": _history(10000, n=60, slope=-1.0),
        # Missing history is excluded, not counted below an MA.
        "BAD": pd.DataFrame(),
    })

    assert breadth["us"]["eligible_ma50"] == 2
    assert breadth["us"]["eligible_ma200"] == 2
    assert breadth["us"]["above_ma50_pct"] == 50.0
    assert breadth["us"]["above_ma200_pct"] == 50.0
    assert breadth["jp"]["eligible_ma50"] == 2
    assert breadth["jp"]["eligible_ma200"] == 1
    assert breadth["jp"]["above_ma50_pct"] == 50.0
    assert breadth["jp"]["above_ma200_pct"] == 100.0
    assert "sufficient valid Close" in breadth["jp"]["universe_basis"]


def test_market_meta_contains_ma200_distance(monkeypatch):
    histories = {
        "SPY": _history(700, slope=0.4),
        "^N225": _history(50000, slope=-5.0),
    }

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, period):
            assert period == "1y"
            return histories[self.symbol]

    monkeypatch.setattr(screener.yf, "Ticker", FakeTicker)

    result = screener.get_market_meta()

    assert result["sp500_vs_ma50_pct"] > 0
    assert result["sp500_vs_ma200_pct"] > 0
    assert result["nikkei_vs_ma50_pct"] < 0
    assert result["nikkei_vs_ma200_pct"] < 0

"""市場の鼓動に日本株を織り込んだ分の検証。

ポートフォリオの約3割が日本株なのに、market 側の指標が VIX/SPY/原油/
米金利/DXY と全部米国だった (2026-08-19)。「市場」を米国指標だけで
語ると、自分が晒されている市場の3割が抜ける。
"""
from __future__ import annotations

from api.routes import today


class TestJapanExposureWeight:
    def test_weights_by_actual_position_value(self):
        holdings = {
            "1489": {"ticker": "1489.T", "currency": "JPY", "broker_position_value_jpy": 300_000},
            "V": {"ticker": "V", "currency": "USD", "broker_position_value_jpy": 700_000},
        }
        assert today._japan_exposure_weight(holdings) == 0.3

    def test_a_jpy_holding_without_the_suffix_still_counts_as_japan(self):
        # 投信など .T を持たない円建て資産を米国側に数えると比率が狂う。
        holdings = {
            "SLIM_ORCAN": {"currency": "JPY", "broker_position_value_jpy": 500_000},
            "V": {"ticker": "V", "currency": "USD", "broker_position_value_jpy": 500_000},
        }
        assert today._japan_exposure_weight(holdings) == 0.5

    def test_zero_and_negative_values_are_skipped(self):
        holdings = {
            "A": {"ticker": "A.T", "currency": "JPY", "broker_position_value_jpy": 0},
            "B": {"ticker": "B", "currency": "USD", "broker_position_value_jpy": 100_000},
        }
        assert today._japan_exposure_weight(holdings) == 0.0

    def test_no_valuation_returns_none_not_zero(self):
        # 0 は「日本株を持っていない」という別の意味になる。評価額が
        # 取れないことと、日本株がないことは区別しなければならない。
        assert today._japan_exposure_weight({}) is None
        assert today._japan_exposure_weight({"A": {"broker_position_value_jpy": None}}) is None

    def test_malformed_rows_do_not_crash(self):
        holdings = {
            "A": "not a dict",
            "B": {"broker_position_value_jpy": "abc"},
            "C": {"ticker": "C.T", "currency": "JPY", "broker_position_value_jpy": 1_000},
        }
        assert today._japan_exposure_weight(holdings) == 1.0


class TestPulsePayload:
    def _pulse(self, vix_state, holdings=None):
        return today._build_pulse({}, vix_state, holdings or {})

    def test_japan_series_passes_through(self):
        pulse = self._pulse({"japan": {
            "level": 68_713.8, "change_1d_pct": 0.59, "change_5d_pct": 4.61,
            "source": "^N225",
            "history_1mo": [{"date": "2026-08-14", "close": 68_713.8}],
        }})
        assert pulse["japan_level"] == 68_713.8
        assert pulse["japan_change_5d_pct"] == 4.61
        assert pulse["japan_source"] == "^N225"
        assert len(pulse["japan_history_1mo"]) == 1

    def test_a_cache_written_before_japan_existed_does_not_break(self):
        # vix_state.json は日本株ブロックが入る前から存在する。
        pulse = self._pulse({"vix": {"level": 15.0}})
        assert pulse["japan_level"] is None
        assert pulse["japan_history_1mo"] == []

    def test_exposure_weight_reaches_the_payload(self):
        pulse = self._pulse({}, {
            "1489": {"ticker": "1489.T", "currency": "JPY", "broker_position_value_jpy": 300_000},
            "V": {"ticker": "V", "currency": "USD", "broker_position_value_jpy": 700_000},
        })
        assert pulse["japan_exposure_weight"] == 0.3

    def test_the_fallback_ticker_is_recorded_when_the_index_is_unavailable(self):
        # ^N225 と 1306.T は別物なので、どちらを見たかが読めないと
        # 「日経が動いた」と誤読される。
        pulse = self._pulse({"japan": {"level": 3_000.0, "source": "1306.T"}})
        assert pulse["japan_source"] == "1306.T"

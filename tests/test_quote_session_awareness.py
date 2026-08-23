"""時間外クオートで発注可否を判定しない契約の検証。

2026-08-20 に実際に起きたこと: 朝の分析は 06:15 JST に走る = NY クローズの
1時間18分後。そこで取得した米国株クオートは板が薄く、
  AVGO  bid 363.00 / ask 386.00 → spread 614bps
  MA    bid 574.86 > ask 574.53 → 交差
となり、どちらも「異常」として発注がブロックされた。しかし実際に発注する
のは次の米国セッションで、そこにこのスプレッドは存在しない。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_quote_validation import validate_market_quote

JST = ZoneInfo("Asia/Tokyo")

# NY クローズ後 (16:00 ET = 05:00 JST 翌日) の実際の取得時刻
AFTER_US_CLOSE = "2026-08-20T06:18:48+09:00"
# 米国場中 (23:30 JST = 10:30 ET)
DURING_US_SESSION = "2026-08-20T23:30:00+09:00"


def _quote(ticker, bid, ask, as_of):
    return {"ticker": ticker, "quote_bid": bid, "quote_ask": ask, "quote_as_of": as_of}


def _now(iso):
    return datetime.fromisoformat(iso) + timedelta(minutes=1)


class TestAfterHours:
    def test_the_real_avgo_wide_spread_is_not_treated_as_a_market_fault(self):
        out = validate_market_quote(
            _quote("AVGO", 363.00, 386.00, AFTER_US_CLOSE), now=_now(AFTER_US_CLOSE)
        )
        assert out["status"] == "session_closed"
        assert out["code"] == "market_quote_session_closed"

    def test_the_wide_spread_is_kept_visible_but_not_as_an_execution_number(self):
        # 参考として読めることは大事だが、spread_bps としては下流へ渡さない。
        out = validate_market_quote(
            _quote("AVGO", 363.00, 386.00, AFTER_US_CLOSE), now=_now(AFTER_US_CLOSE)
        )
        assert out["observed_spread_bps"] == pytest.approx(614.15, abs=0.5)
        assert "spread_bps" not in out

    def test_the_real_mastercard_crossed_quote_is_not_called_a_data_fault(self):
        out = validate_market_quote(
            _quote("MA", 574.86, 574.53, AFTER_US_CLOSE), now=_now(AFTER_US_CLOSE)
        )
        assert out["status"] == "session_closed"
        assert out["code"] != "market_quote_inverted"


class TestDuringSession:
    def test_a_crossed_quote_during_trading_hours_is_still_a_fault(self):
        # 場中の bid>ask は本物のデータ異常。時間外の扱いを場中へ広げない。
        out = validate_market_quote(
            _quote("AVGO", 386.00, 363.00, DURING_US_SESSION), now=_now(DURING_US_SESSION)
        )
        assert out["status"] == "invalid"
        assert out["code"] == "market_quote_inverted"

    def test_a_wide_spread_during_trading_hours_still_yields_an_execution_spread(self):
        out = validate_market_quote(
            _quote("AVGO", 363.00, 386.00, DURING_US_SESSION), now=_now(DURING_US_SESSION)
        )
        assert out["status"] == "valid"
        assert out["spread_bps"] == pytest.approx(614.15, abs=0.5)

    def test_a_normal_quote_during_trading_hours_is_unchanged(self):
        out = validate_market_quote(
            _quote("AVGO", 372.40, 372.60, DURING_US_SESSION), now=_now(DURING_US_SESSION)
        )
        assert out["status"] == "valid"
        assert out["spread_bps"] < 30


class TestUnchangedContracts:
    def test_a_quote_without_a_timestamp_is_still_rejected(self):
        out = validate_market_quote(
            {"ticker": "AVGO", "quote_bid": 363.0, "quote_ask": 386.0}, now=_now(AFTER_US_CLOSE)
        )
        assert out["status"] == "invalid"
        assert out["code"] == "market_quote_timestamp_missing"

    def test_a_half_quote_is_still_rejected_regardless_of_session(self):
        out = validate_market_quote(
            {"ticker": "AVGO", "quote_bid": 363.0, "quote_as_of": AFTER_US_CLOSE},
            now=_now(AFTER_US_CLOSE),
        )
        assert out["status"] == "invalid"
        assert out["code"] == "market_quote_incomplete"

    def test_no_quote_at_all_is_still_absent_not_closed(self):
        out = validate_market_quote({"ticker": "AVGO"}, now=_now(AFTER_US_CLOSE))
        assert out["status"] == "absent"

    def test_an_unknown_exchange_falls_back_to_the_original_checks(self):
        # 取引所が引けない銘柄でセッション判定に頼ると、判定不能を理由に
        # すべて素通りしてしまう。従来どおりの検証に落ちること。
        out = validate_market_quote(
            _quote("SLIM_SP500", 200.0, 100.0, AFTER_US_CLOSE), now=_now(AFTER_US_CLOSE)
        )
        assert out["status"] == "invalid"
        assert out["code"] == "market_quote_inverted"


# 週末・祝日は「終日休場」で、平日引け後とはカレンダー上の別分岐になる。
# 月曜朝 06:16 JST の分析が見るのは日曜 17:16 ET のクオート。
WEEKEND_MONDAY_MORNING = "2026-08-24T06:16:41+09:00"


def test_a_weekend_quote_is_not_handed_to_the_execution_gate():
    """本題: 週末に取得した spread を執行判断に渡さないこと。

    2026-08-24 の朝分析で実際に起きた: AVGO bid 350.00 / ask 366.50 の
    日曜クオートが spread 460.6bps として通り、limit_order_wide_spread_review
    を発生させた。market_session_context は終日休場の分岐で session_state を
    返しておらず、_session_state() が None に落ちて時間外判定が発火しなかった。
    平日引け後は正しく遮断できていたので、週末と祝日だけ穴が開いていた。
    """
    result = validate_market_quote(
        _quote("AVGO", 350.00, 366.50, WEEKEND_MONDAY_MORNING),
        now=datetime.fromisoformat(WEEKEND_MONDAY_MORNING),
    )

    assert result["status"] == "session_closed"
    assert result["code"] == "market_quote_session_closed"
    assert result["quote_session_state"] == "closed"
    # 参考値としては読めるが、spread_bps としては下流へ渡さない。
    assert "spread_bps" not in result
    assert result["observed_spread_bps"] == pytest.approx(460.57, abs=0.1)


def test_a_weekend_crossed_quote_is_not_called_a_market_fault():
    """週末の bid>ask 交差も「異常」ではなく時間外として扱うこと。"""
    result = validate_market_quote(
        _quote("MA", 574.86, 574.53, WEEKEND_MONDAY_MORNING),
        now=datetime.fromisoformat(WEEKEND_MONDAY_MORNING),
    )

    assert result["status"] == "session_closed"
    assert result["code"] != "market_quote_inverted"

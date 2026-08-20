import json

import pytest

import execution_safety
from analyst import llm_client
from analyst import order_strategy


@pytest.fixture
def us_session_open(monkeypatch):
    """米国場中に固定する。

    spread は「実際に発注するセッションのコスト」なので、取引所が閉じている
    間のクオートからは執行数値として採らない (2026-08-20)。時刻を固定しないと
    このテストは実行時刻で結果が変わる。
    """
    monkeypatch.setattr(
        execution_safety, "market_session_context",
        lambda ticker, when: {"status": "trading_day", "session_state": "open"},
    )


@pytest.fixture
def us_session_closed(monkeypatch):
    monkeypatch.setattr(
        execution_safety, "market_session_context",
        lambda ticker, when: {
            "status": "trading_day", "session_state": "closed",
            "reason": "after_regular_session",
        },
    )


def _write_analysis(path, action):
    path.write_text(json.dumps({"synthesis": {"priority_actions": [action]}}), encoding="utf-8")


def test_order_strategy_converts_unsafe_market_to_limit(monkeypatch, tmp_path, us_session_open):
    cache = tmp_path / "ai_portfolio_analysis.json"
    _write_analysis(cache, {
        "ticker": "ROBO", "type": "sell", "urgency": "low",
        "action": "ROBOを売却", "order_type": "market",
    })
    monkeypatch.setattr(order_strategy, "CACHE_PATH", cache)
    monkeypatch.setattr(order_strategy, "_get_market_meta", lambda: {"vix": 17})
    monkeypatch.setattr(order_strategy, "_get_current_price_atr", lambda ticker: {
        "current_price": 82.96, "atr_pct": 1.2, "bid": 81.30, "ask": 84.70,
        "spread_bps": 408.0,
    })
    monkeypatch.setattr(llm_client, "call_claude", lambda **kwargs: json.dumps({
        "orders": [{"ticker": "ROBO", "order_type": "market", "decision_price": 82.96}],
    }))

    result = order_strategy.re_evaluate()

    action = json.loads(cache.read_text(encoding="utf-8"))["synthesis"]["priority_actions"][0]
    assert result["status"] == "ok"
    assert action["order_type"] == "limit"
    assert action["limit_price"] == 82.96
    assert action["spread_bps"] == 408.0
    assert "成行を禁止" in action["execution_reason"]
    assert action["execution_readiness"] != "ready"


def test_unsafe_market_order_still_becomes_limit_when_the_exchange_is_closed(
    monkeypatch, tmp_path, us_session_closed,
):
    """時間外でも成行→指値の変換は効く。変わるのは spread の扱いだけ。

    時間外の spread は次のセッションのコストではないので執行数値としては
    残さないが、生の bid/ask は証跡として保持する。
    """
    cache = tmp_path / "ai_portfolio_analysis.json"
    _write_analysis(cache, {
        "ticker": "ROBO", "type": "sell", "urgency": "low",
        "action": "ROBOを売却", "order_type": "market",
    })
    monkeypatch.setattr(order_strategy, "CACHE_PATH", cache)
    monkeypatch.setattr(order_strategy, "_get_market_meta", lambda: {"vix": 17})
    monkeypatch.setattr(order_strategy, "_get_current_price_atr", lambda ticker: {
        "current_price": 82.96, "atr_pct": 1.2, "bid": 81.30, "ask": 84.70,
        "spread_bps": 408.0,
    })
    monkeypatch.setattr(llm_client, "call_claude", lambda **kwargs: json.dumps({
        "orders": [{"ticker": "ROBO", "order_type": "market", "decision_price": 82.96}],
    }))

    order_strategy.re_evaluate()

    action = json.loads(cache.read_text(encoding="utf-8"))["synthesis"]["priority_actions"][0]
    assert action["order_type"] == "limit"
    assert action["execution_readiness"] != "ready"
    # 執行数値としては渡さない
    assert action.get("spread_bps") is None
    # 証跡は残る
    assert action["quote_bid"] == 81.30
    assert action["quote_ask"] == 84.70


def test_order_strategy_marks_no_trade_when_market_quote_is_unverifiable(monkeypatch, tmp_path):
    cache = tmp_path / "ai_portfolio_analysis.json"
    _write_analysis(cache, {
        "ticker": "UNKNOWN", "type": "buy", "urgency": "high",
        "action": "UNKNOWNを買付", "order_type": "market",
    })
    monkeypatch.setattr(order_strategy, "CACHE_PATH", cache)
    monkeypatch.setattr(order_strategy, "_get_market_meta", lambda: {})
    monkeypatch.setattr(order_strategy, "_get_current_price_atr", lambda ticker: {})
    monkeypatch.setattr(llm_client, "call_claude", lambda **kwargs: json.dumps({
        "orders": [{"ticker": "UNKNOWN", "order_type": "market"}],
    }))

    order_strategy.re_evaluate()

    action = json.loads(cache.read_text(encoding="utf-8"))["synthesis"]["priority_actions"][0]
    assert action["no_trade_zone"] is True
    assert "order_type" not in action
    assert "current price/bid/ask/spread" in action["skip_reason"]
    assert action["execution_readiness"] == "blocked"
    assert any(reason["code"] == "no_trade_zone" for reason in action["execution_block_reasons"])

import json

from analyst import llm_client
from analyst import order_strategy


def _write_analysis(path, action):
    path.write_text(json.dumps({"synthesis": {"priority_actions": [action]}}), encoding="utf-8")


def test_order_strategy_converts_unsafe_market_to_limit(monkeypatch, tmp_path):
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

"""T3: actions.py の Pydantic スキーマ検証"""
import uuid
import json
import pytest
from api.routes.actions import (
    ExecutionRequest, Direction, Status, InvestmentType, Currency,
)

_ExecutionRequestModel = ExecutionRequest


def ExecutionRequest(**kwargs):
    kwargs.setdefault("idempotency_key", f"test-{uuid.uuid4().hex}")
    return _ExecutionRequestModel(**kwargs)


def test_idempotency_key_is_required():
    with pytest.raises(Exception):
        _ExecutionRequestModel(ticker="NVDA")


def test_valid_minimal():
    req = ExecutionRequest(ticker='NVDA')
    assert req.ticker == 'NVDA'
    assert req.direction == Direction.hold
    assert req.status == Status.executed


def test_margin_directions_supported():
    assert ExecutionRequest(ticker='MU', direction='short').direction == Direction.short
    assert ExecutionRequest(ticker='MU', direction='cover').direction == Direction.cover
    assert ExecutionRequest(ticker='MU', direction='margin_buy').direction == Direction.margin_buy


def test_partial_status_supported():
    req = ExecutionRequest(ticker='NVDA', status='partial')
    assert req.status == Status.partial


def test_ticker_required():
    with pytest.raises(Exception):
        ExecutionRequest(ticker='')


def test_quantity_nonneg():
    with pytest.raises(Exception):
        ExecutionRequest(ticker='AVGO', quantity=-5)


def test_price_nonneg():
    with pytest.raises(Exception):
        ExecutionRequest(ticker='AVGO', price=-100)


def test_order_type_enum():
    # 有効な値は market/limit/stop（大文字化も受ける）
    assert ExecutionRequest(ticker='X', order_type='market').order_type == 'market'
    assert ExecutionRequest(ticker='X', order_type='LIMIT').order_type == 'limit'
    with pytest.raises(Exception):
        ExecutionRequest(ticker='X', order_type='gtc')


def test_bid_ask_nonneg():
    with pytest.raises(Exception):
        ExecutionRequest(ticker='X', bid_at_order=-1)


def test_investment_type_enum():
    # 'short' は無効（旧互換を避けるため 'swing' に統一済み）
    with pytest.raises(Exception):
        ExecutionRequest(ticker='X', investment_type='short')
    assert ExecutionRequest(ticker='X', investment_type='swing').investment_type == InvestmentType.swing


def test_currency_optional():
    req = ExecutionRequest(ticker='NVDA', currency=None)
    assert req.currency is None
    req2 = ExecutionRequest(ticker='NVDA', currency='USD')
    assert req2.currency == Currency.USD


def test_ai_provenance_fields_are_normalized():
    req = ExecutionRequest(
        ticker="LLY",
        analysis_id=" run-1 ",
        action_state_id=" state-1 ",
        policy_override_reason=" manual fill truth ",
    )
    assert req.analysis_id == "run-1"
    assert req.action_state_id == "state-1"
    assert req.policy_override_reason == "manual fill truth"


def test_execution_routing_fields_are_canonicalized():
    req = ExecutionRequest(
        ticker="1489.T",
        execution_owner="妻",
        execution_broker="SBI証券",
        execution_position_keys=["1489_WIFE"],
    )

    assert req.execution_owner == "wife"
    assert req.execution_broker == "sbi"
    assert req.execution_position_keys == ["1489_WIFE"]


def test_state_only_endpoint_rejects_placed_or_filled_transition():
    import asyncio
    from fastapi import HTTPException
    from api.routes import actions

    for status in ("placed", "filled"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(actions.update_action_status(
                "action-id",
                actions.StatusPatchRequest(status=status),
            ))
        assert exc.value.status_code == 409


def test_linked_order_cannot_reuse_reprice_required_candidate(tmp_path, monkeypatch):
    from api.routes import actions

    (tmp_path / "action_state.json").write_text(json.dumps({"actions": {
        "holiday": {
            "id": "holiday", "ticker": "ROBO", "action_type": "sell",
            "status": "reprice_required", "execution_readiness": "review",
            "market_reprice_required": True,
            "recommended_at": "2026-07-20T06:08:00",
        },
    }}), encoding="utf-8")
    analysis = tmp_path / "ai_portfolio_analysis.json"
    analysis.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(actions, "BASE_DIR", tmp_path)
    monkeypatch.setattr(actions, "ANALYSIS_FILE", analysis)

    readiness, reasons = actions._linked_ai_readiness_values(
        analysis_id=None,
        action_state_id="holiday",
        ticker="ROBO",
        direction="sell",
    )

    assert readiness == "review"
    assert any(row["code"] == "market_closed_reprice_required" for row in reasons)


def test_linked_order_ttl_does_not_expire_before_market_open(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from api.routes import actions

    opens_at = datetime.now().astimezone() + timedelta(hours=2)
    (tmp_path / "action_state.json").write_text(json.dumps({"actions": {
        "morning": {
            "id": "morning", "ticker": "ROBO", "action_type": "sell",
            "status": "pending", "execution_readiness": "ready",
            "recommended_at": "2020-01-01T06:15:00",
            "expiry_starts_at": opens_at.isoformat(), "expiry_minutes": 30,
        },
    }}), encoding="utf-8")
    analysis = tmp_path / "ai_portfolio_analysis.json"
    analysis.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(actions, "BASE_DIR", tmp_path)
    monkeypatch.setattr(actions, "ANALYSIS_FILE", analysis)

    readiness, reasons = actions._linked_ai_readiness_values(
        analysis_id=None, action_state_id="morning", ticker="ROBO", direction="sell",
    )

    assert readiness == "ready"
    assert all(row.get("code") != "order_expired" for row in reasons)

from broker_cost_basis import validate_broker_cost_basis
from position_identity import PositionIdentity


POSITION = PositionIdentity(
    owner="husband",
    broker="rakuten",
    account="specific",
    canonical_instrument_id="DEMO",
)


def _holding(account="特定"):
    return {
        "key": "DEMO",
        "ticker": "DEMO",
        "owner": "husband",
        "broker": "楽天証券",
        "account": account,
        "shares": 80,
        "broker_quantity": 80,
        "broker_total_cost_basis_jpy": 640_000,
        "broker_cost_basis_source": "rakuten_assetbalance_csv",
        "broker_cost_basis_as_of": "2026-07-28",
    }


def test_taxable_broker_basis_is_prorated_for_final_quantity(tmp_path, monkeypatch):
    monkeypatch.setenv("ALMANAC_STATE_DIR", str(tmp_path))
    result = validate_broker_cost_basis(
        position=POSITION,
        quantity=-20,
        holding=_holding(),
        executions=[],
    )
    assert result["status"] == "ready"
    estimate = result["estimate"]
    assert estimate.amount_jpy == 160_000
    assert estimate.source == "broker_report"
    assert estimate.method == "broker_reported"
    assert estimate.reconciled is True


def test_quantity_mismatch_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("ALMANAC_STATE_DIR", str(tmp_path))
    holding = _holding()
    holding["broker_quantity"] = 79
    result = validate_broker_cost_basis(
        position=POSITION,
        quantity=-20,
        holding=holding,
        executions=[],
    )
    assert result["status"] == "review"
    assert result["reason"] == "broker_quantity_mismatch"


def test_fill_before_snapshot_is_safe_but_same_day_or_after_is_review(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALMANAC_STATE_DIR", str(tmp_path))

    def fill(saved_at):
        return {
            "id": f"fill-{saved_at}",
            "ticker": "DEMO",
            "direction": "buy",
            "quantity": 1,
            "price": 10,
            "status": "executed",
            "saved_at": saved_at,
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_account": "特定",
        }

    before = validate_broker_cost_basis(
        position=POSITION,
        quantity=-20,
        holding=_holding(),
        executions=[fill("2026-07-27T12:00:00")],
    )
    same_day = validate_broker_cost_basis(
        position=POSITION,
        quantity=-20,
        holding=_holding(),
        executions=[fill("2026-07-28T12:00:00")],
    )
    after = validate_broker_cost_basis(
        position=POSITION,
        quantity=-20,
        holding=_holding(),
        executions=[fill("2026-07-29T12:00:00")],
    )
    assert before["status"] == "ready"
    assert same_day["reason"] == "temporal_order_unknown"
    assert after["reason"] == "after_snapshot"


def test_active_order_blocks_and_cancelled_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("ALMANAC_STATE_DIR", str(tmp_path))
    base = {
        "id": "order",
        "ticker": "DEMO",
        "direction": "sell",
        "quantity": 5,
        "price": 10,
        "saved_at": "2026-07-29T12:00:00",
        "execution_owner": "husband",
        "execution_broker": "rakuten",
        "execution_account": "特定",
    }
    active = validate_broker_cost_basis(
        position=POSITION,
        quantity=-20,
        holding=_holding(),
        executions=[{**base, "status": "ordered"}],
    )
    cancelled = validate_broker_cost_basis(
        position=POSITION,
        quantity=-20,
        holding=_holding(),
        executions=[{**base, "status": "cancelled"}],
    )
    assert active["reason"] == "active_order_after_broker_snapshot_unknown"
    assert cancelled["status"] == "ready"


def test_nisa_tax_exemption_does_not_require_basis(tmp_path, monkeypatch):
    monkeypatch.setenv("ALMANAC_STATE_DIR", str(tmp_path))
    position = PositionIdentity(
        owner="husband",
        broker="rakuten",
        account="nisa_growth",
        canonical_instrument_id="DEMO",
    )
    holding = _holding("NISA成長投資枠")
    holding.pop("broker_total_cost_basis_jpy")
    holding.pop("broker_cost_basis_source")
    result = validate_broker_cost_basis(
        position=position,
        quantity=-20,
        holding=holding,
        executions=[],
    )
    assert result["status"] == "ready"
    assert result["estimate"].source == "nisa_tax_exempt"

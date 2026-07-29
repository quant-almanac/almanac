import json

import pytest

from execution_reconciliation import (
    classify_temporal_order,
    record_route_correction,
    resolve_effective_execution_record,
)


def _execution():
    return {
        "id": "ABC_sell_20260716010000_demo",
        "ticker": "ABC",
        "direction": "sell",
        "quantity": 5,
        "price": 42.5,
        "account": "特定",
        "saved_at": "2026-07-16T01:00:00",
        "status": "executed",
    }


def test_route_correction_is_overlay_only(tmp_path):
    raw = _execution()
    original = dict(raw)
    state_path = tmp_path / "execution_reconciliation_state.json"
    correction = record_route_correction(
        execution_record=raw,
        corrected_route={
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_account": "NISA成長投資枠",
        },
        evidence={"source_sha256": "abc", "row_hash": "def"},
        reason="broker trade history proves the route",
        approved_by="test",
        state_path=state_path,
    )
    effective = resolve_effective_execution_record(raw, state_path=state_path)

    assert raw == original
    assert effective["execution_owner"] == "husband"
    assert effective["execution_broker"] == "rakuten"
    assert effective["execution_account"] == "nisa_growth"
    assert effective["quantity"] == raw["quantity"]
    assert effective["price"] == raw["price"]
    assert effective["saved_at"] == raw["saved_at"]
    assert effective["execution_reconciliation_status"] == "corrected"
    assert effective["execution_reconciliation_correction_id"] == correction["correction_id"]


def test_route_correction_rejects_quantity_or_silent_overwrite(tmp_path):
    raw = _execution()
    state_path = tmp_path / "execution_reconciliation_state.json"
    with pytest.raises(ValueError, match="cannot change"):
        record_route_correction(
            execution_record=raw,
            corrected_route={
                "execution_owner": "husband",
                "execution_broker": "rakuten",
                "execution_account": "特定",
                "quantity": 10,
            },
            evidence={"row_hash": "x"},
            reason="bad",
            approved_by="test",
            state_path=state_path,
        )

    first = record_route_correction(
        execution_record=raw,
        corrected_route={
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_account": "NISA成長投資枠",
        },
        evidence={"row_hash": "one"},
        reason="first",
        approved_by="test",
        state_path=state_path,
    )
    with pytest.raises(ValueError, match="supersedes_correction_id"):
        record_route_correction(
            execution_record=raw,
            corrected_route={
                "execution_owner": "husband",
                "execution_broker": "rakuten",
                "execution_account": "特定",
            },
            evidence={"row_hash": "two"},
            reason="second",
            approved_by="test",
            state_path=state_path,
        )
    second = record_route_correction(
        execution_record=raw,
        corrected_route={
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_account": "特定",
        },
        evidence={"row_hash": "two"},
        reason="second",
        approved_by="test",
        supersedes_correction_id=first["correction_id"],
        state_path=state_path,
    )
    effective = resolve_effective_execution_record(raw, state_path=state_path)
    assert effective["execution_account"] == "specific"
    assert effective["execution_reconciliation_correction_id"] == second["correction_id"]
    assert len(json.loads(state_path.read_text())["corrections"]) == 2


def test_base_record_change_fails_closed(tmp_path):
    raw = _execution()
    state_path = tmp_path / "execution_reconciliation_state.json"
    record_route_correction(
        execution_record=raw,
        corrected_route={
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_account": "NISA成長投資枠",
        },
        evidence={"row_hash": "x"},
        reason="route",
        approved_by="test",
        state_path=state_path,
    )
    changed = {**raw, "quantity": 6}
    effective = resolve_effective_execution_record(changed, state_path=state_path)
    assert effective["execution_reconciliation_status"] == "review"
    assert effective["execution_reconciliation_reasons"] == ["base_record_hash_mismatch"]
    assert "execution_owner" not in effective


@pytest.mark.parametrize(
    ("trade_date", "expected", "review"),
    [
        ("2026-07-14", "before_snapshot", False),
        ("2026-07-16", "temporal_order_unknown", True),
        ("2026-07-17", "after_snapshot", True),
    ],
)
def test_date_only_temporal_order_three_way(trade_date, expected, review):
    result = classify_temporal_order(
        snapshot_as_of="2026-07-16T12:00:00",
        trade_date=trade_date,
    )
    assert result["temporal_order"] == expected
    assert result["requires_review"] is review
    assert result["comparison_basis"] == "date_only"


def test_naive_exact_timestamps_use_jst():
    result = classify_temporal_order(
        snapshot_as_of="2026-07-16T12:00:00",
        trade_timestamp="2026-07-16T11:59:59",
    )
    assert result == {
        "temporal_order": "before_snapshot",
        "requires_review": False,
        "comparison_basis": "exact_timestamp",
    }

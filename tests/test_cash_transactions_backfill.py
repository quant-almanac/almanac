import json

import event_ledger as el
import cash_transactions_backfill as ctb


def test_cash_transactions_backfill_is_idempotent(tmp_path):
    tx_path = tmp_path / "cash_transactions.json"
    db_path = tmp_path / "ledger.db"
    tx_path.write_text(
        json.dumps({
            "transactions": [
                {
                    "id": "tx_jpy_in",
                    "timestamp": "2026-05-01T09:00:00",
                    "type": "deposit",
                    "currency": "JPY",
                    "broker": "rakuten",
                    "amount": 100000,
                    "description": "monthly cash",
                },
                {
                    "id": "tx_usd_out",
                    "timestamp": "2026-05-02 10:30:00",
                    "type": "withdraw",
                    "currency": "USD",
                    "broker": "rakuten",
                    "amount": 100,
                    "fx_rate_usdjpy": 155.5,
                },
            ],
        }),
        encoding="utf-8",
    )

    first = ctb.backfill(apply=True, tx_path=tx_path, db_path=db_path)
    second = ctb.backfill(apply=True, tx_path=tx_path, db_path=db_path)

    assert first["inserted"] == 2
    assert first["duplicates"] == 0
    assert second["inserted"] == 0
    assert second["duplicates"] == 2

    events = el.query_events(types=["cash_flow"], db_path=db_path)
    assert [e["event_id"] for e in events] == ["tx_jpy_in", "tx_usd_out"]
    assert events[0]["amount_jpy"] == 100000
    assert events[1]["amount_jpy"] == -15550


def test_cash_transactions_backfill_requires_usd_fx(tmp_path):
    tx_path = tmp_path / "cash_transactions.json"
    db_path = tmp_path / "ledger.db"
    tx_path.write_text(
        json.dumps({
            "transactions": [
                {
                    "id": "tx_usd_in",
                    "timestamp": "2026-05-01T09:00:00",
                    "type": "deposit",
                    "currency": "USD",
                    "broker": "rakuten",
                    "amount": 100,
                },
            ],
        }),
        encoding="utf-8",
    )

    result = ctb.backfill(apply=True, tx_path=tx_path, db_path=db_path)

    assert result["inserted"] == 0
    assert result["skipped"] == 1
    assert "fx_rate_usdjpy" in result["errors"][0]["error"]
    assert el.query_events(types=["cash_flow"], db_path=db_path) == []


def test_cash_transactions_backfill_explicit_fx_is_marked_provisional(tmp_path):
    tx_path = tmp_path / "cash_transactions.json"
    db_path = tmp_path / "ledger.db"
    tx_path.write_text(
        json.dumps({
            "transactions": [
                {
                    "id": "tx_usd_in",
                    "timestamp": "2026-05-01",
                    "type": "deposit",
                    "currency": "USD",
                    "broker": "rakuten",
                    "amount": 10,
                },
            ],
        }),
        encoding="utf-8",
    )

    result = ctb.backfill(apply=True, tx_path=tx_path, fx_rate_usdjpy=158.77, db_path=db_path)

    assert result["inserted"] == 1
    event = el.query_events(types=["cash_flow"], db_path=db_path)[0]
    assert event["note"].endswith("(provisional FX 158.77)")
    payload = json.loads(event["raw_payload"])
    assert payload["provisional_fx"] is True

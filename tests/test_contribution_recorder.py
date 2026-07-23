import json

import contribution_recorder as cr


def test_record_dry_run_does_not_write_cash_transactions(tmp_path, monkeypatch):
    tx_path = tmp_path / "cash_transactions.json"
    monkeypatch.setattr(cr, "CASH_TX_FILE", tx_path)

    result = cr.record(lookback_days=8, apply=False)

    assert result["dry_run"] is True
    assert result["added_to_json"] >= 0
    assert not tx_path.exists()
    assert "planned_transactions" in result


def test_record_dry_run_preserves_existing_file(tmp_path, monkeypatch):
    tx_path = tmp_path / "cash_transactions.json"
    original = {"transactions": [{"id": "existing", "timestamp": "2026-05-01"}]}
    tx_path.write_text(json.dumps(original), encoding="utf-8")
    before = tx_path.read_text(encoding="utf-8")
    monkeypatch.setattr(cr, "CASH_TX_FILE", tx_path)

    cr.record(lookback_days=8, apply=False)

    assert tx_path.read_text(encoding="utf-8") == before

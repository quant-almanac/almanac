"""The retired aggregate cash rollforward must remain impossible to use."""
from datetime import datetime
import json

import holdings_freshness as hf


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_retired_cash_rollforward_is_explicitly_disabled(tmp_path):
    result = hf.plan_cash_rollforward(base_dir=tmp_path)

    assert result == {
        "ok": False,
        "status": "disabled",
        "reason_code": "wallet_scoped_projection_required",
        "reason": (
            "旧rollforward-cashは口座・通貨・event種別を区別しないため無効です。"
            "wallet_ledger_projection_v1を使用してください。"
        ),
    }


def test_retired_apply_never_writes_financial_artifacts(tmp_path):
    _write(tmp_path / "account.json", {"total_cash": 100, "last_updated": "2026-08-01"})
    _write(tmp_path / "holdings.json", {"CASH_JPY_SBI_WIFE": {"shares": 50}})
    before_account = (tmp_path / "account.json").read_bytes()
    before_holdings = (tmp_path / "holdings.json").read_bytes()

    result = hf.apply_cash_rollforward(base_dir=tmp_path, now=datetime(2026, 8, 16))

    assert result["reason_code"] == "wallet_scoped_projection_required"
    assert (tmp_path / "account.json").read_bytes() == before_account
    assert (tmp_path / "holdings.json").read_bytes() == before_holdings
    assert not (tmp_path / hf.CASH_ROLLFORWARD_FILE_NAME).exists()


def test_old_sidecar_cannot_advance_cash_freshness(tmp_path):
    _write(tmp_path / "account.json", {"total_cash": 100, "last_updated": "2026-08-01"})
    _write(tmp_path / hf.CASH_ROLLFORWARD_FILE_NAME, {
        "derived_at": "2026-08-16T09:00:00",
        "provenance": "ledger_rollforward",
        "source_hash": hf.content_hash(tmp_path / "account.json"),
    })

    as_of, source = hf.effective_source_as_of(
        scope="cash", file_as_of=datetime(2026, 8, 1), base_dir=tmp_path,
    )

    assert source == "file"
    assert as_of == datetime(2026, 8, 1)
    assert hf.latest_valid_cash_rollforward(base_dir=tmp_path) is None

import csv
import json

import pytest

import event_ledger as el
import event_ledger_backfill as backfill


def _write_trade_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["日時", "アクション", "ティッカー", "価格", "株数"])
        writer.writeheader()
        writer.writerows(rows)


def test_trade_backfill_explicit_fx_is_marked_provisional(tmp_path, monkeypatch):
    csv_path = tmp_path / "trade_history.csv"
    db_path = tmp_path / "ledger.db"
    _write_trade_csv(
        csv_path,
        [
            {
                "日時": "2026-05-01 09:00",
                "アクション": "BUY",
                "ティッカー": "AAPL",
                "価格": "100",
                "株数": "2",
            },
        ],
    )
    monkeypatch.setattr(el, "DB_PATH", db_path)

    result = backfill.backfill(apply=True, csv_path=csv_path, fx_rate_usdjpy=158.77)

    assert result["inserted"] == 1
    event = el.query_events(types=["trade"], db_path=db_path)[0]
    assert event["note"].endswith("(provisional FX 158.77)")
    payload = json.loads(event["raw_payload"])
    assert payload["provisional_fx"] is True
    assert payload["fx_source"] == "explicit"


def test_trade_backfill_csv_fx_is_not_marked_provisional(tmp_path, monkeypatch):
    csv_path = tmp_path / "trade_history.csv"
    db_path = tmp_path / "ledger.db"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["日時", "アクション", "ティッカー", "価格", "株数", "fx_rate_usdjpy"])
        writer.writeheader()
        writer.writerow({
            "日時": "2026-05-01 09:00",
            "アクション": "BUY",
            "ティッカー": "AAPL",
            "価格": "100",
            "株数": "2",
            "fx_rate_usdjpy": "151.25",
        })
    monkeypatch.setattr(el, "DB_PATH", db_path)

    result = backfill.backfill(apply=True, csv_path=csv_path, fx_rate_usdjpy=158.77)

    assert result["inserted"] == 1
    event = el.query_events(types=["trade"], db_path=db_path)[0]
    assert event["fx_rate_usdjpy"] == 151.25
    assert "provisional FX" not in event["note"]
    payload = json.loads(event["raw_payload"])
    assert payload["provisional_fx"] is False
    assert payload["fx_source"] == "csv"


def test_ticker_currency_treats_domestic_funds_as_jpy():
    assert backfill._ticker_currency("SLIM_SP500") == "JPY"
    assert backfill._ticker_currency("SLIM_ORCAN_WIFE") == "JPY"
    assert backfill._ticker_currency("IFREE_FANGPLUS") == "JPY"
    assert backfill._ticker_currency("NOMURA_SEMI") == "JPY"
    assert backfill._ticker_currency("MNXACT") == "JPY"
    assert backfill._ticker_currency("GS_MMF_USD") == "USD"
    assert backfill._ticker_currency("AAPL") == "USD"


def test_domestic_fund_backfill_uses_jpy_and_normalizes_nav_price(tmp_path, monkeypatch):
    csv_path = tmp_path / "trade_history.csv"
    db_path = tmp_path / "ledger.db"
    _write_trade_csv(
        csv_path,
        [
            {
                "日時": "2026-04-26 17:13",
                "アクション": "BUY",
                "ティッカー": "SLIM_SP500",
                "価格": "41675.0",
                "株数": "191819.0",
            },
        ],
    )
    monkeypatch.setattr(el, "DB_PATH", db_path)

    result = backfill.backfill(apply=True, csv_path=csv_path)

    assert result["inserted"] == 1
    assert result["skipped"] == 0
    event = el.query_events(types=["trade"], db_path=db_path)[0]
    assert event["currency"] == "JPY"
    assert event["fx_rate_usdjpy"] is None
    assert event["price"] == pytest.approx(4.1675)
    assert event["amount_jpy"] == pytest.approx(-799405.68)
    payload = json.loads(event["raw_payload"])
    assert payload["csv_price"] == 41675.0
    assert payload["ledger_price"] == pytest.approx(4.1675)
    assert payload["price_unit"] == "per_unit"
    assert payload["csv_price_unit"] == "per_10000_units"

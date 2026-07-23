"""
tests/test_event_ledger.py — P1-18-A: Event Ledger
"""
import tempfile
from pathlib import Path

import pytest

import event_ledger as el


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test_ledger.db"


def test_init_schema_idempotent(tmp_db):
    el.init_schema(tmp_db)
    el.init_schema(tmp_db)  # 2 度呼んでも OK
    # ledger_events テーブルが存在することを確認
    import sqlite3
    conn = sqlite3.connect(str(tmp_db))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ledger_events'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_append_event_basic(tmp_db):
    r = el.append_event(
        event_type="trade",
        ticker="AAPL",
        direction="buy",
        quantity=10,
        price=170.0,
        currency="USD",
        fx_rate_usdjpy=150.0,
        account="特定",
        db_path=tmp_db,
    )
    assert r["duplicate"] is False
    assert r["amount_jpy"] == -255000.0  # 10 * 170 * 150 = 255000 (buy=負)
    assert r["event_id"]


def test_append_event_idempotent(tmp_db):
    r1 = el.append_event(
        event_type="trade", ticker="AAPL", direction="buy",
        quantity=10, price=170.0, currency="USD", fx_rate_usdjpy=150.0,
        event_id="fixed-uuid-123",
        db_path=tmp_db,
    )
    r2 = el.append_event(
        event_type="trade", ticker="AAPL", direction="buy",
        quantity=99, price=999.0, currency="USD", fx_rate_usdjpy=150.0,
        event_id="fixed-uuid-123",
        db_path=tmp_db,
    )
    assert r1["duplicate"] is False
    assert r2["duplicate"] is True
    assert r1["rowid"] == r2["rowid"]
    # 元の値が変わっていないこと
    assert r2["amount_jpy"] == r1["amount_jpy"]


def test_append_event_sell_is_positive(tmp_db):
    r = el.append_event(
        event_type="trade", ticker="AAPL", direction="sell",
        quantity=5, price=200.0, currency="USD", fx_rate_usdjpy=150.0,
        db_path=tmp_db,
    )
    assert r["amount_jpy"] == 5 * 200 * 150  # 正値


def test_append_event_short_is_positive(tmp_db):
    r = el.append_event(
        event_type="trade", ticker="7203.T", direction="short",
        quantity=3, price=1000.0, currency="JPY", db_path=tmp_db,
    )
    assert r["amount_jpy"] == 3000.0


def test_append_event_cover_is_negative(tmp_db):
    r = el.append_event(
        event_type="trade", ticker="7203.T", direction="cover",
        quantity=2, price=900.0, currency="JPY", db_path=tmp_db,
    )
    assert r["amount_jpy"] == -1800.0


def test_append_event_margin_buy_is_negative(tmp_db):
    r = el.append_event(
        event_type="trade", ticker="7203.T", direction="margin_buy",
        quantity=2, price=8000.0, currency="JPY", db_path=tmp_db,
    )
    assert r["amount_jpy"] == -16000.0


def test_append_event_jpy_no_fx(tmp_db):
    r = el.append_event(
        event_type="trade", ticker="9999.T", direction="buy",
        quantity=100, price=2500, currency="JPY",
        db_path=tmp_db,
    )
    assert r["amount_jpy"] == -250000.0


def test_append_event_unknown_type_raises(tmp_db):
    with pytest.raises(ValueError):
        el.append_event(event_type="invalid_type", db_path=tmp_db)


def test_append_event_unknown_direction_raises(tmp_db):
    with pytest.raises(ValueError):
        el.append_event(event_type="trade", direction="hodl", db_path=tmp_db)


def test_query_events_filters_by_type(tmp_db):
    el.append_event(event_type="trade", ticker="AAPL", direction="buy",
                    quantity=1, price=100, currency="JPY", db_path=tmp_db)
    el.append_event(event_type="cash_flow", direction="in",
                    quantity=100000, price=1.0, currency="JPY", db_path=tmp_db)
    el.append_event(event_type="dividend", ticker="AAPL", direction="in",
                    quantity=500, price=1.0, currency="JPY", db_path=tmp_db)

    trades = el.query_events(types=["trade"], db_path=tmp_db)
    assert len(trades) == 1
    assert trades[0]["ticker"] == "AAPL"
    assert trades[0]["event_type"] == "trade"

    flows = el.query_events(types=["cash_flow", "dividend"], db_path=tmp_db)
    assert len(flows) == 2


def test_cash_flow_sum_jpy(tmp_db):
    el.append_event(event_type="cash_flow", direction="in",
                    quantity=300000, price=1.0, currency="JPY",
                    occurred_at="2026-01-15T10:00:00", db_path=tmp_db)
    el.append_event(event_type="cash_flow", direction="out",
                    quantity=100000, price=1.0, currency="JPY",
                    occurred_at="2026-02-10T10:00:00", db_path=tmp_db)
    # trade event は cash_flow_sum に含めない
    el.append_event(event_type="trade", ticker="X", direction="buy",
                    quantity=1, price=1000, currency="JPY",
                    occurred_at="2026-01-20T10:00:00", db_path=tmp_db)

    total = el.cash_flow_sum_jpy(
        date_from="2026-01-01", date_to="2026-03-01", db_path=tmp_db,
    )
    # +300000 - 100000 = 200000
    assert total == 200000.0


def test_cash_flow_sum_jpy_filters_outside_range(tmp_db):
    el.append_event(event_type="cash_flow", direction="in",
                    quantity=500000, price=1.0, currency="JPY",
                    occurred_at="2025-12-31T23:59:00", db_path=tmp_db)
    el.append_event(event_type="cash_flow", direction="in",
                    quantity=100000, price=1.0, currency="JPY",
                    occurred_at="2026-02-01T10:00:00", db_path=tmp_db)

    total = el.cash_flow_sum_jpy(
        date_from="2026-01-01", date_to="2026-03-01", db_path=tmp_db,
    )
    # 2025-12-31 は範囲外
    assert total == 100000.0


def test_unsupported_currency_amount_raises(tmp_db):
    with pytest.raises(ValueError, match="未対応通貨"):
        el.append_event(
            event_type="trade", ticker="ABC.L", direction="buy",
            quantity=10, price=500, currency="GBP",
            db_path=tmp_db,
        )


def test_usd_event_without_fx_rate_raises(tmp_db):
    with pytest.raises(ValueError, match="fx_rate_usdjpy"):
        el.append_event(
            event_type="trade", ticker="AAPL", direction="buy",
            quantity=10, price=170, currency="USD",
            fx_rate_usdjpy=None,
            db_path=tmp_db,
        )


# Codex P1 #7 — amount 必須 event はフィールド欠落を黙認せず raise

def test_amount_required_cash_flow_missing_quantity_raises(tmp_path):
    import pytest
    import event_ledger as el
    db = tmp_path / "l.db"
    with pytest.raises(ValueError):
        el.append_event(event_type="cash_flow", direction="in",
                        price=1.0, currency="JPY", account="楽天", db_path=db)


def test_amount_required_dividend_missing_price_raises(tmp_path):
    import pytest
    import event_ledger as el
    db = tmp_path / "l.db"
    with pytest.raises(ValueError):
        el.append_event(event_type="dividend", direction="in",
                        quantity=1000, currency="JPY", db_path=db)


def test_usd_dividend_without_fx_raises(tmp_path):
    import pytest
    import event_ledger as el
    db = tmp_path / "l.db"
    with pytest.raises(ValueError):
        el.append_event(event_type="dividend", direction="in",
                        quantity=10, price=1.0, currency="USD", db_path=db)

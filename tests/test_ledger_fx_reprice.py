import json

import event_ledger as el
import ledger_fx_reprice as fxr


def test_dry_run_detects_legacy_unmarked_backfill(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-01T09:00:00",
        ticker="AAPL",
        direction="buy",
        quantity=2,
        price=100,
        currency="USD",
        fx_rate_usdjpy=158.77,
        account="特定",
        source="backfill",
        note="trade_history.csv backfill",
        event_id="legacy_trade",
        db_path=db_path,
    )

    result = fxr.reprice_usd_events(
        apply=False,
        db_path=db_path,
        fx_by_date={"2026-05-01": 151.25},
    )

    assert result["dry_run"] is True
    assert result["would_update"] == 1
    assert result["sample"][0]["reason"] == "legacy_unmarked_backfill"

    event = el.query_events(types=["trade"], db_path=db_path)[0]
    assert event["fx_rate_usdjpy"] == 158.77
    assert event["amount_jpy"] == -31754.0


def test_apply_reprices_fx_and_amount_and_marks_payload(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-03T09:00:00",
        ticker="AAPL",
        direction="sell",
        quantity=2,
        price=100,
        currency="USD",
        fx_rate_usdjpy=158.77,
        account="特定",
        source="backfill",
        note="trade_history.csv backfill (provisional FX 158.77)",
        raw_payload={"fx_source": "explicit", "provisional_fx": True},
        event_id="provisional_trade",
        db_path=db_path,
    )

    result = fxr.reprice_usd_events(
        apply=True,
        db_path=db_path,
        fx_by_date={"2026-05-01": 151.25},
    )

    assert result["updated"] == 1
    event = el.query_events(types=["trade"], db_path=db_path)[0]
    assert event["fx_rate_usdjpy"] == 151.25
    assert event["amount_jpy"] == 30250.0
    assert "provisional FX" not in event["note"]
    assert "historical FX 2026-05-01: 151.2500 via provided" in event["note"]

    payload = json.loads(event["raw_payload"])
    assert payload["provisional_fx"] is False
    assert payload["fx_source"] == "historical_provided"
    assert payload["historical_fx_date"] == "2026-05-01"
    assert payload["previous_fx_rate_usdjpy"] == 158.77
    assert payload["fx_reprice_history"][0]["reason"] == "marked_provisional"


def test_csv_fx_backfill_event_is_not_repriced(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-01T09:00:00",
        ticker="AAPL",
        direction="buy",
        quantity=2,
        price=100,
        currency="USD",
        fx_rate_usdjpy=151.25,
        account="特定",
        source="backfill",
        note="trade_history.csv backfill",
        raw_payload={"fx_source": "csv", "provisional_fx": False},
        event_id="csv_fx_trade",
        db_path=db_path,
    )

    result = fxr.reprice_usd_events(
        apply=True,
        db_path=db_path,
        fx_by_date={"2026-05-01": 158.77},
    )

    assert result["updated"] == 0
    assert result["skipped"] == 1
    event = el.query_events(types=["trade"], db_path=db_path)[0]
    assert event["fx_rate_usdjpy"] == 151.25
    assert event["amount_jpy"] == -30250.0


# ────────────────────────────────────────────────────────────────
# _is_candidate 締め (Codex 2026-05-17 P2)
# ────────────────────────────────────────────────────────────────

def test_explicitly_confirmed_event_is_not_repriced(tmp_path):
    """provisional_fx=False の event は明示的に確定済みとして protect される。"""
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-01T09:00:00",
        ticker="AAPL",
        direction="buy",
        quantity=2,
        price=100,
        currency="USD",
        fx_rate_usdjpy=151.25,
        account="特定",
        source="backfill",
        # provisional_fx=False で明示的に確定済みとマーク
        raw_payload={"fx_source": "explicit", "provisional_fx": False},
        event_id="confirmed_trade",
        db_path=db_path,
    )

    result = fxr.reprice_usd_events(
        apply=True,
        db_path=db_path,
        fx_by_date={"2026-05-01": 158.77},
    )

    assert result["updated"] == 0
    assert result["skipped"] == 1
    event = el.query_events(types=["trade"], db_path=db_path)[0]
    assert event["fx_rate_usdjpy"] == 151.25


def test_already_repriced_event_is_not_double_repriced(tmp_path):
    """fx_reprice_history を持つ event は再 reprice しない。"""
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-01T09:00:00",
        ticker="AAPL",
        direction="buy",
        quantity=2,
        price=100,
        currency="USD",
        fx_rate_usdjpy=151.25,
        account="特定",
        source="backfill",
        # 既に 1 度 reprice 済みの payload
        raw_payload={
            "fx_source": "historical_yfinance",
            "provisional_fx": False,
            "fx_reprice_history": [
                {"repriced_at": "2026-05-17T16:00:00", "previous_fx_rate_usdjpy": 158.77,
                 "previous_amount_jpy": -31754.0, "reason": "marked_provisional"}
            ],
        },
        event_id="already_repriced_trade",
        db_path=db_path,
    )

    result = fxr.reprice_usd_events(
        apply=True,
        db_path=db_path,
        fx_by_date={"2026-05-01": 99.99},  # わざと意味のない FX
    )

    assert result["updated"] == 0
    assert result["skipped"] == 1
    event = el.query_events(types=["trade"], db_path=db_path)[0]
    assert event["fx_rate_usdjpy"] == 151.25


def test_historical_marked_source_is_protected(tmp_path):
    """fx_source='historical_*' / 'csv' の event は protect される。"""
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-01T09:00:00",
        ticker="AAPL",
        direction="buy",
        quantity=1, price=100, currency="USD", fx_rate_usdjpy=151.25,
        source="backfill",
        raw_payload={"fx_source": "historical_yfinance"},
        event_id="historical_marked",
        db_path=db_path,
    )

    result = fxr.reprice_usd_events(
        apply=True, db_path=db_path, fx_by_date={"2026-05-01": 99.99},
    )
    assert result["updated"] == 0
    assert result["skipped"] == 1


def test_amount_jpy_sign_matches_event_ledger_for_all_directions():
    """Codex P1 #2: 符号は event_ledger に委譲。buy/margin_buy/cover/out=負、sell/short=正。"""
    fx = 150.0

    def row(direction):
        return {"quantity": 2, "price": 50, "currency": "USD",
                "direction": direction, "event_id": "e"}

    # 2 * 50 * 150 = 15000
    assert fxr._amount_jpy(row("buy"), fx) == -15000.0
    assert fxr._amount_jpy(row("margin_buy"), fx) == -15000.0   # 旧コードは +15000 (バグ)
    assert fxr._amount_jpy(row("cover"), fx) == -15000.0        # 旧コードは +15000 (バグ)
    assert fxr._amount_jpy(row("out"), fx) == -15000.0
    assert fxr._amount_jpy(row("sell"), fx) == 15000.0
    assert fxr._amount_jpy(row("short"), fx) == 15000.0


# ────────────────────────────────────────────────────────
# Codex P1 #3 — append-only: reprice は原行を UPDATE せず訂正イベントを追記
# ────────────────────────────────────────────────────────

def test_reprice_appends_correction_and_preserves_original(tmp_path):
    import sqlite3
    import pytest
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade", occurred_at="2026-05-01T09:00:00", ticker="AAPL",
        direction="buy", quantity=2, price=100, currency="USD", fx_rate_usdjpy=158.77,
        account="特定", source="backfill", note="provisional FX 158.77",
        raw_payload={"fx_source": "explicit", "provisional_fx": True},
        event_id="prov1", db_path=db_path,
    )
    res = fxr.reprice_usd_events(apply=True, db_path=db_path, fx_by_date={"2026-05-01": 151.25})
    assert res["updated"] == 1

    # 原行は append-only で残存 (raw table に 2 行: 原 + 訂正)
    conn = sqlite3.connect(str(db_path))
    ids = [r[0] for r in conn.execute("SELECT event_id FROM ledger_events ORDER BY id").fetchall()]
    conn.close()
    assert len(ids) == 2
    assert "prov1" in ids
    assert any(i.endswith(":fxreprice:2026-05-01") for i in ids)

    # query_events は原行を除外し、訂正後 (buy=負, 2*100*151.25) を返す
    trades = el.query_events(types=["trade"], db_path=db_path)
    assert len(trades) == 1
    assert trades[0]["fx_rate_usdjpy"] == 151.25
    assert trades[0]["amount_jpy"] == pytest.approx(-2 * 100 * 151.25)


def test_reprice_is_idempotent_no_double_correction(tmp_path):
    import sqlite3
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade", occurred_at="2026-05-01T09:00:00", ticker="AAPL",
        direction="buy", quantity=2, price=100, currency="USD", fx_rate_usdjpy=158.77,
        source="backfill", note="provisional FX",
        raw_payload={"fx_source": "explicit", "provisional_fx": True},
        event_id="prov1", db_path=db_path,
    )
    fxr.reprice_usd_events(apply=True, db_path=db_path, fx_by_date={"2026-05-01": 151.25})
    res2 = fxr.reprice_usd_events(apply=True, db_path=db_path, fx_by_date={"2026-05-01": 151.25})
    assert res2["updated"] == 0
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
    conn.close()
    assert n == 2  # 二重訂正していない


def test_tax_lot_sees_reprice_corrected_fx(tmp_path):
    import tax_lot as tl
    import pytest
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade", occurred_at="2026-01-10T09:00:00", ticker="AAPL",
        direction="buy", quantity=10, price=150, currency="USD", fx_rate_usdjpy=158.0,
        account="特定", source="backfill", note="provisional FX",
        raw_payload={"fx_source": "explicit", "provisional_fx": True},
        event_id="b1", db_path=db_path,
    )
    fxr.reprice_usd_events(apply=True, db_path=db_path, fx_by_date={"2026-01-10": 145.0})
    st = tl.build_lots("AAPL", db_path=db_path)
    assert len(st.open_lots) == 1
    assert st.open_lots[0].cost_per_share_jpy == pytest.approx(150 * 145.0)  # 訂正後 fx

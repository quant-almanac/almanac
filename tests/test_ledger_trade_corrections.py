import sqlite3
import json

import pytest

import event_ledger as el
import ledger_trade_corrections as ltc


def _ids(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in conn.execute("SELECT event_id FROM ledger_events ORDER BY id").fetchall()]
    finally:
        conn.close()


def _event_by_id(db_path, event_id):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM ledger_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _seed_6762_missing_opening_context(db_path):
    el.append_event(
        event_type="trade",
        occurred_at="2026-04-22T23:08:00",
        ticker="6762.T",
        direction="buy",
        quantity=100.0,
        price=2690.6,
        currency="JPY",
        account="特定",
        source="backfill",
        event_id="backfill_a8b1b5e343afba13",
        db_path=db_path,
    )
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-26T20:05:17",
        ticker="6762.T",
        direction="sell",
        quantity=100.0,
        price=3708.0,
        currency="JPY",
        account="特定",
        source="api",
        event_id="exec_6762.T_sell_20260526200517",
        db_path=db_path,
    )
    el.append_event(
        event_type="trade",
        occurred_at="2026-06-01T01:24:20",
        ticker="6762.T",
        direction="sell",
        quantity=100.0,
        price=3999.0,
        currency="JPY",
        account="特定",
        source="api",
        event_id="exec_6762.T_sell_20260529004619",
        db_path=db_path,
    )


def test_corrects_known_domestic_fund_trade_append_only(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-04-26T17:13:00",
        ticker="SLIM_SP500",
        direction="buy",
        quantity=191819.0,
        price=41675.0,
        currency="USD",
        fx_rate_usdjpy=159.7469940185547,
        source="backfill",
        event_id="backfill_744c26637500caeb",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1
    assert "backfill_744c26637500caeb" in _ids(db_path)
    assert "backfill_744c26637500caeb:tradecorr:v1" in _ids(db_path)
    trades = el.query_events(types=["trade"], db_path=db_path)
    assert len(trades) == 1
    corrected = trades[0]
    assert corrected["event_id"] == "backfill_744c26637500caeb:tradecorr:v1"
    assert corrected["currency"] == "JPY"
    assert corrected["fx_rate_usdjpy"] is None
    assert corrected["price"] == pytest.approx(4.1675)
    assert corrected["amount_jpy"] == pytest.approx(-799405.68)


def test_corrects_known_us_price_decimal_shift(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-04-02T01:27:00",
        ticker="EWG",
        direction="sell",
        quantity=120.0,
        price=4815.3,
        currency="USD",
        fx_rate_usdjpy=158.68800354003906,
        source="backfill",
        event_id="backfill_2cb58f863647f50c",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1
    trades = el.query_events(types=["trade"], db_path=db_path)
    assert len(trades) == 1
    corrected = trades[0]
    assert corrected["event_id"] == "backfill_2cb58f863647f50c:tradecorr:v1"
    assert corrected["currency"] == "USD"
    assert corrected["fx_rate_usdjpy"] == pytest.approx(158.68800354003906)
    assert corrected["price"] == pytest.approx(48.153)
    assert corrected["amount_jpy"] == pytest.approx(916956.41)


def test_correction_is_idempotent(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-04-02T01:08:00",
        ticker="EPOL",
        direction="sell",
        quantity=100.0,
        price=3653.86,
        currency="USD",
        fx_rate_usdjpy=158.68800354003906,
        source="backfill",
        event_id="backfill_5b1bda5015d95c3b",
        db_path=db_path,
    )

    first = ltc.correct_known_trade_events(apply=True, db_path=db_path)
    second = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert first["corrected"] == 1
    assert second["corrected"] == 0
    assert {"event_id": "backfill_5b1bda5015d95c3b", "reason": "already_superseded"} in second["skipped"]
    assert len([s for s in second["skipped"] if s["reason"] == "already_superseded"]) == 1
    assert len(_ids(db_path)) == 2


def test_adds_known_missing_6762_opening_lot_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    _seed_6762_missing_opening_context(db_path)

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1
    assert "manual_opening_6762.T_20260301_100sh" in _ids(db_path)

    import tax_lot as tl

    state = tl.build_lots("6762.T", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 2


def test_missing_6762_opening_lot_correction_is_idempotent(tmp_path):
    db_path = tmp_path / "ledger.db"
    _seed_6762_missing_opening_context(db_path)
    first = ltc.correct_known_trade_events(apply=True, db_path=db_path)
    second = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert first["corrected"] == 1
    assert second["corrected"] == 0
    assert {
        "event_id": "manual_opening_6762.T_20260301_100sh",
        "reason": "already_present",
    } in second["skipped"]


def test_adds_known_missing_7751_buy_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-06-09T00:15:34",
        ticker="7751.T",
        direction="sell",
        quantity=100.0,
        price=4380.0,
        currency="JPY",
        account="特定",
        source="api",
        event_id="exec_7751.T_sell_20260609001534",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1
    assert "manual_missing_7751.T_buy_20260428_100sh" in _ids(db_path)

    import tax_lot as tl

    state = tl.build_lots("7751.T", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 1
    assert state.realized_trades[0].realized_jpy == 33_425
    raw_payload = json.loads(_event_by_id(db_path, "manual_missing_7751.T_buy_20260428_100sh")["raw_payload"])
    assert "7751.T_buy_20260428004418" in " ".join(raw_payload["evidence"])
    assert "6762.T" not in " ".join(raw_payload["evidence"])


def test_adds_known_missing_adbe_usd_buy_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-04-22T01:15:00",
        ticker="ADBE",
        direction="buy",
        quantity=2.0,
        price=249.76,
        currency="USD",
        fx_rate_usdjpy=159.3730010986328,
        account="特定",
        source="backfill",
        event_id="backfill_79424418cba1fed4",
        db_path=db_path,
    )
    el.append_event(
        event_type="trade",
        occurred_at="2026-04-28T00:41:00",
        ticker="ADBE",
        direction="buy",
        quantity=1.0,
        price=241.71,
        currency="USD",
        fx_rate_usdjpy=159.35699462890625,
        account="特定",
        source="backfill",
        event_id="backfill_5fbbdd40aca00d4a",
        db_path=db_path,
    )
    el.append_event(
        event_type="trade",
        occurred_at="2026-06-19T01:01:00",
        ticker="ADBE",
        direction="sell",
        quantity=4.0,
        price=193.78,
        currency="USD",
        fx_rate_usdjpy=161.09800720214844,
        account="特定",
        source="api",
        event_id="exec_ADBE_sell_20260619010100",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1
    event = _event_by_id(db_path, "manual_missing_ADBE_buy_20260422_1sh")
    assert event is not None
    assert event["currency"] == "USD"
    assert event["fx_rate_usdjpy"] == pytest.approx(159.3730010986328)
    assert event["amount_jpy"] == pytest.approx(-39236.04)
    raw_payload = json.loads(event["raw_payload"])
    assert "ADBE 4 shares" in " ".join(raw_payload["evidence"])

    import tax_lot as tl

    state = tl.build_lots("ADBE", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 3


def test_adds_known_missing_amat_opening_lot_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-06-19T01:00:14",
        ticker="AMAT",
        direction="sell",
        quantity=2.0,
        price=628.795,
        currency="USD",
        fx_rate_usdjpy=161.09800720214844,
        account="特定",
        source="api",
        event_id="exec_AMAT_sell_20260619010014",
        db_path=db_path,
    )
    el.append_event(
        event_type="trade",
        occurred_at="2026-06-26T00:17:21",
        ticker="AMAT",
        direction="sell",
        quantity=1.0,
        price=628.0,
        currency="USD",
        fx_rate_usdjpy=161.6959991455078,
        account="特定",
        source="api",
        event_id="exec_AMAT_sell_20260624005345",
        db_path=db_path,
    )
    el.append_event(
        event_type="trade",
        occurred_at="2026-06-26T01:10:11",
        ticker="AMAT",
        direction="sell",
        quantity=1.0,
        price=641.35,
        currency="USD",
        fx_rate_usdjpy=161.69000244140625,
        account="特定",
        source="api",
        event_id="exec_AMAT_sell_20260626011011",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1
    event = _event_by_id(db_path, "manual_opening_AMAT_20260517_5sh")
    assert event is not None
    assert event["currency"] == "USD"
    assert event["fx_rate_usdjpy"] == pytest.approx(156.6295523433811)
    assert event["amount_jpy"] == pytest.approx(-308_496.0)
    raw_payload = json.loads(event["raw_payload"])
    assert "AMAT 5 shares" in " ".join(raw_payload["evidence"])

    import tax_lot as tl

    state = tl.build_lots("AMAT", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == pytest.approx(1.0)
    assert len(state.realized_trades) == 3


def test_corrects_avgo_price_and_rebuilds_account_lots(tmp_path):
    db_path = tmp_path / "ledger.db"
    avgo_sells = [
        ("backfill_fb5389def81ad442", "2026-04-02T01:29:00", 15.0, 4693.28, 158.68800354003906),
        ("backfill_6fb07eefe0f2fcb7", "2026-04-17T00:33:00", 5.0, 397.265, 159.19500732421875),
        ("backfill_45da544b670f2913", "2026-04-18T01:52:00", 5.0, 397.265, 159.19500732421875),
        ("backfill_a89f6a99e8b9e93a", "2026-04-21T00:56:00", 3.0, 397.03, 158.843994140625),
        ("backfill_c3a961c89795785f", "2026-04-22T01:12:00", 3.0, 401.5, 159.3730010986328),
        ("backfill_5591a2a342e308ae", "2026-04-25T11:31:00", 1.0, 414.85, 159.7469940185547),
        ("exec_AVGO_sell_20260604005033", "2026-06-04T00:50:33", 3.0, 486.19, 159.98300170898438),
        ("exec_AVGO_sell_20260606002104", "2026-06-06T00:21:04", 3.0, 398.3117, 160.2729949951172),
        ("exec_AVGO_sell_20260619010034", "2026-06-19T01:00:34", 3.0, 407.28, 161.09800720214844),
        ("exec_AVGO_sell_20260624004845", "2026-06-26T00:16:46", 3.0, 383.25, 161.6959991455078),
    ]
    for event_id, occurred_at, quantity, price, fx in avgo_sells:
        el.append_event(
            event_type="trade",
            occurred_at=occurred_at,
            ticker="AVGO",
            direction="sell",
            quantity=quantity,
            price=price,
            currency="USD",
            fx_rate_usdjpy=fx,
            account="特定",
            source="api" if event_id.startswith("exec_") else "backfill",
            event_id=event_id,
            db_path=db_path,
        )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 4
    corrected_first_sell = _event_by_id(db_path, "backfill_fb5389def81ad442:tradecorr:v1")
    assert corrected_first_sell is not None
    assert corrected_first_sell["price"] == pytest.approx(312.88533333)
    assert corrected_first_sell["amount_jpy"] == pytest.approx(744_767.23)
    assert _event_by_id(db_path, "manual_missing_AVGO_sell_20260507_3sh") is not None

    import tax_lot as tl

    state = tl.build_lots("AVGO", db_path=db_path)
    open_by_account = {}
    for lot in state.open_lots:
        if lot.is_open:
            open_by_account[lot.account] = open_by_account.get(lot.account, 0.0) + lot.remaining_qty
    assert open_by_account == {"特定": pytest.approx(3.0), "一般": pytest.approx(27.0)}
    first_realized = state.realized_trades[0]
    assert first_realized.lot_id == "manual_opening_AVGO_toku_20260301_50sh"
    assert first_realized.proceeds_jpy == pytest.approx(744_767.23)


def test_adds_known_missing_crm_buy_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-13T00:08:00",
        ticker="CRM",
        direction="sell",
        quantity=1.0,
        price=174.6,
        currency="USD",
        fx_rate_usdjpy=157.67100524902344,
        account="特定",
        source="backfill",
        event_id="backfill_6e7e0c8f45f15533",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1
    event = _event_by_id(db_path, "manual_missing_CRM_buy_20260423_1sh")
    assert event is not None
    assert event["currency"] == "USD"
    assert event["fx_rate_usdjpy"] == pytest.approx(159.48800659179688)

    import tax_lot as tl

    state = tl.build_lots("CRM", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 1


def test_adds_known_missing_crwv_opening_lot_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-03-07T00:27:00",
        ticker="CRWV",
        direction="sell",
        quantity=10.0,
        price=75.99,
        currency="USD",
        fx_rate_usdjpy=157.53399658203125,
        account="特定",
        source="backfill",
        event_id="backfill_9f3ac76a4adf3217",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1
    event = _event_by_id(db_path, "manual_opening_CRWV_20260307_10sh")
    assert event is not None
    assert event["amount_jpy"] == pytest.approx(-185_673.08)

    import tax_lot as tl

    state = tl.build_lots("CRWV", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 1
    assert state.realized_trades[0].realized_jpy == pytest.approx(-65_963.0)


def test_adds_known_missing_epol_opening_lot_with_price_correction(tmp_path):
    db_path = tmp_path / "ledger.db"
    epol_sells = [
        ("backfill_c742285dc077329e", "2026-03-27T01:30:00", 140.0, 35.1759, 159.70399475097656),
        ("backfill_5b1bda5015d95c3b", "2026-04-02T01:08:00", 100.0, 3653.86, 158.68800354003906),
        ("backfill_8ed1241f7a6f02b5", "2026-04-03T01:23:00", 70.0, 36.6787, 159.49099731445312),
        ("backfill_696e2b997c31138e", "2026-04-07T01:08:00", 80.0, 36.9454, 159.68299865722656),
        ("backfill_4685f85dddb89d5f", "2026-04-08T00:44:00", 20.0, 36.5992, 158.71600341796875),
    ]
    for event_id, occurred_at, quantity, price, fx in epol_sells:
        el.append_event(
            event_type="trade",
            occurred_at=occurred_at,
            ticker="EPOL",
            direction="sell",
            quantity=quantity,
            price=price,
            currency="USD",
            fx_rate_usdjpy=fx,
            account="特定",
            source="backfill",
            event_id=event_id,
            db_path=db_path,
        )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 2
    corrected = _event_by_id(db_path, "backfill_5b1bda5015d95c3b:tradecorr:v1")
    assert corrected is not None
    assert corrected["price"] == pytest.approx(36.5386)
    assert _event_by_id(db_path, "manual_opening_EPOL_20260301_410sh") is not None

    import tax_lot as tl

    state = tl.build_lots("EPOL", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 5


def test_adds_known_missing_ewg_opening_lot_with_price_correction(tmp_path):
    db_path = tmp_path / "ledger.db"
    ewg_sells = [
        ("backfill_d3f5269c6a0f4d3f", "2026-03-27T01:27:00", 130.0, 38.771, 159.70399475097656),
        ("backfill_2cb58f863647f50c", "2026-04-02T01:27:00", 120.0, 4815.3, 158.68800354003906),
        ("backfill_4a4dbb8e2a2a6408", "2026-04-03T01:22:00", 240.0, 39.745, 159.49099731445312),
    ]
    for event_id, occurred_at, quantity, price, fx in ewg_sells:
        el.append_event(
            event_type="trade",
            occurred_at=occurred_at,
            ticker="EWG",
            direction="sell",
            quantity=quantity,
            price=price,
            currency="USD",
            fx_rate_usdjpy=fx,
            account="特定",
            source="backfill",
            event_id=event_id,
            db_path=db_path,
        )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 2
    corrected = _event_by_id(db_path, "backfill_2cb58f863647f50c:tradecorr:v1")
    assert corrected is not None
    assert corrected["price"] == pytest.approx(48.153)
    assert _event_by_id(db_path, "manual_opening_EWG_20260301_490sh") is not None

    import tax_lot as tl

    state = tl.build_lots("EWG", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 3


def test_adds_known_missing_gld_account_lots_and_external_sell(tmp_path):
    db_path = tmp_path / "ledger.db"
    gld_sells = [
        ("backfill_3ddacf0558ba1335", "2026-03-27T01:29:00", 7.0, 403.8179, 159.70399475097656, "特定"),
        ("backfill_70db339d3d884aa8", "2026-04-07T01:07:00", 5.0, 428.6164, 159.68299865722656, "特定"),
        ("backfill_88c56e0b2edb21d0", "2026-04-17T00:33:00", 1.0, 441.09, 159.19500732421875, "特定"),
        ("backfill_ae7d7c3f5588c4d6", "2026-04-18T01:52:00", 3.0, 447.1224, 159.19500732421875, "特定"),
        ("backfill_b244df44d3b994b1", "2026-04-21T00:56:00", 4.0, 440.8152, 158.843994140625, "特定"),
        ("backfill_acad6597f399ca92", "2026-04-22T01:11:00", 10.0, 435.42, 159.3730010986328, "特定"),
        ("backfill_dbbe9ee13ac3d2f5", "2026-04-25T11:30:00", 1.0, 434.3432, 159.7469940185547, "特定"),
        ("backfill_99d320ab3b6ed359", "2026-04-28T00:39:00", 5.0, 429.8731, 159.35699462890625, "特定"),
        ("exec_GLD_sell_20260528013459", "2026-05-28T01:34:59", 3.0, 408.27, 159.49099731445312, "NISA成長投資枠"),
        ("exec_GLD_sell_20260604004942", "2026-06-04T00:49:42", 2.0, 407.6795, 159.96099853515625, "特定"),
        ("exec_GLD_sell_20260606002135", "2026-06-06T00:21:36", 6.0, 399.2939, 160.2729949951172, "特定"),
        ("exec_GLD_sell_20260626010930", "2026-06-26T01:09:30", 3.0, 368.23, 161.69000244140625, "特定"),
    ]
    for event_id, occurred_at, quantity, price, fx, account in gld_sells:
        el.append_event(
            event_type="trade",
            occurred_at=occurred_at,
            ticker="GLD",
            direction="sell",
            quantity=quantity,
            price=price,
            currency="USD",
            fx_rate_usdjpy=fx,
            account=account,
            source="api" if event_id.startswith("exec_") else "backfill",
            event_id=event_id,
            db_path=db_path,
        )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 3
    assert _event_by_id(db_path, "manual_missing_GLD_sell_20260507_5sh") is not None

    import tax_lot as tl

    state = tl.build_lots("GLD", db_path=db_path)
    open_by_account = {}
    for lot in state.open_lots:
        if lot.is_open:
            open_by_account[lot.account] = open_by_account.get(lot.account, 0.0) + lot.remaining_qty
    assert open_by_account == {
        "特定": pytest.approx(15.0),
        "NISA成長投資枠": pytest.approx(2.0),
    }


def test_corrects_iev_first_sell_and_rebuilds_open_lot(tmp_path):
    db_path = tmp_path / "ledger.db"
    events = [
        ("backfill_aeae9469f5ddf2a5", "sell", "2026-03-27T01:27:00", 140.0, 35.1759, 159.70399475097656),
        ("backfill_8ea19682a5746036", "sell", "2026-04-03T01:24:00", 50.0, 68.3675, 159.49099731445312),
        ("backfill_a8a731b24f227e30", "sell", "2026-04-07T01:09:00", 30.0, 68.9219, 159.68299865722656),
        ("backfill_35aa2de7492cd6f5", "sell", "2026-04-08T00:45:00", 40.0, 67.88, 158.71600341796875),
        ("backfill_d497f548e440f0a6", "sell", "2026-04-10T23:55:00", 20.0, 71.75, 159.11199951171875),
        ("backfill_c6b8d392f46474b4", "buy", "2026-04-18T01:53:00", 5.0, 72.0, 159.19500732421875),
        ("backfill_db4838c1139507a4", "buy", "2026-04-21T00:59:00", 10.0, 72.9, 158.843994140625),
        ("backfill_1f363868ca8d4311", "buy", "2026-04-22T01:14:00", 5.0, 72.16, 159.3730010986328),
    ]
    for event_id, direction, occurred_at, quantity, price, fx in events:
        el.append_event(
            event_type="trade",
            occurred_at=occurred_at,
            ticker="IEV",
            direction=direction,
            quantity=quantity,
            price=price,
            currency="USD",
            fx_rate_usdjpy=fx,
            account="特定",
            source="backfill",
            event_id=event_id,
            db_path=db_path,
        )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 2
    corrected = _event_by_id(db_path, "backfill_aeae9469f5ddf2a5:tradecorr:v1")
    assert corrected is not None
    assert corrected["price"] == pytest.approx(65.1759)

    import tax_lot as tl

    state = tl.build_lots("IEV", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == pytest.approx(80.0)
    assert len(state.realized_trades) == 5


def test_adds_known_missing_lrcx_opening_lot_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-09T00:01:00",
        ticker="LRCX",
        direction="sell",
        quantity=1.0,
        price=295.51,
        currency="USD",
        fx_rate_usdjpy=156.82899475097656,
        account="特定",
        source="backfill",
        event_id="backfill_c4f19bf3c77c54a0",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1
    event = _event_by_id(db_path, "manual_opening_LRCX_20260509_1sh")
    assert event is not None
    assert event["amount_jpy"] == pytest.approx(-40_277.99)

    import tax_lot as tl

    state = tl.build_lots("LRCX", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 1
    assert state.realized_trades[0].realized_jpy == pytest.approx(6_066.55)


def test_voids_corrupted_meta_sell_and_rebuilds_account_lots(tmp_path):
    db_path = tmp_path / "ledger.db"
    meta_events = [
        ("backfill_8351d28fa8b92d72", "sell", "2026-03-13T00:48:00", 1.0, 640.915, 159.20599365234375, "特定"),
        ("backfill_8839775adf29460d", "sell", "2026-04-10T00:25:00", 20.0, 71.07, 159.11199951171875, "特定"),
        ("backfill_de59fd27855f007d", "buy", "2026-04-17T00:34:00", 1.0, 673.78, 159.19500732421875, "特定"),
        ("backfill_950e0ec9aad2e1dc", "buy", "2026-04-18T01:54:00", 1.0, 673.78, 159.19500732421875, "特定"),
        ("backfill_9ff63d067d1d75e3", "buy", "2026-04-21T00:57:00", 1.0, 672.7, 158.843994140625, "特定"),
        ("backfill_a836f7fd4d9cb6f6", "buy", "2026-04-22T01:13:00", 1.0, 668.0, 159.3730010986328, "特定"),
        ("backfill_bd1fd8475583ed62", "sell", "2026-04-25T11:31:00", 1.0, 675.21, 159.7469940185547, "特定"),
        ("backfill_0cf81a3676d17dee", "sell", "2026-04-28T00:39:00", 1.0, 676.52, 159.35699462890625, "特定"),
        ("exec_META_buy_20260528013400", "buy", "2026-05-28T01:34:00", 2.0, 612.73, 159.49099731445312, "特定"),
        ("exec_META_sell_20260626010952", "sell", "2026-06-26T01:09:52", 1.0, 551.095, 161.69000244140625, "一般"),
    ]
    for event_id, direction, occurred_at, quantity, price, fx, account in meta_events:
        el.append_event(
            event_type="trade",
            occurred_at=occurred_at,
            ticker="META",
            direction=direction,
            quantity=quantity,
            price=price,
            currency="USD",
            fx_rate_usdjpy=fx,
            account=account,
            source="api" if event_id.startswith("exec_") else "backfill",
            event_id=event_id,
            db_path=db_path,
        )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 4
    voided = _event_by_id(db_path, "backfill_8839775adf29460d:tradecorr:v1")
    assert voided is not None
    assert voided["quantity"] == 0
    assert voided["amount_jpy"] == 0

    import tax_lot as tl

    state = tl.build_lots("META", db_path=db_path)
    open_by_account = {}
    for lot in state.open_lots:
        if lot.is_open:
            open_by_account[lot.account] = open_by_account.get(lot.account, 0.0) + lot.remaining_qty
    assert open_by_account == {"特定": pytest.approx(9.0), "一般": pytest.approx(1.0)}


def test_adds_known_missing_nem_buys_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-06-19T00:53:57",
        ticker="NEM",
        direction="sell",
        quantity=32.0,
        price=99.6623,
        currency="USD",
        fx_rate_usdjpy=161.09800720214844,
        account="特定",
        source="api",
        event_id="exec_NEM_sell_20260609001703",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 2
    assert _event_by_id(db_path, "manual_missing_NEM_buy_20260423_2sh") is not None
    assert _event_by_id(db_path, "manual_missing_NEM_buy_20260507_30sh") is not None

    import tax_lot as tl

    state = tl.build_lots("NEM", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 2


def test_corrects_nvda_account_and_rebuilds_open_general_lot(tmp_path):
    db_path = tmp_path / "ledger.db"
    nvda_events = [
        ("backfill_7fc4217a144147cb", "buy", "2026-02-19T17:08:00", 3.0, 188.0, 154.6929931640625),
        ("backfill_fdd60c8676e4b771", "buy", "2026-02-28T23:37:00", 10.0, 177.19, 155.85899353027344),
        ("backfill_3d57e6bef64258c0", "buy", "2026-03-07T00:24:00", 5.0, 180.5, 157.53399658203125),
        ("backfill_d4937d19ae6f7833", "sell", "2026-03-07T00:25:00", 5.0, 180.5, 157.53399658203125),
        ("backfill_c808586f792c0754", "sell", "2026-04-10T23:54:00", 15.0, 188.28, 159.11199951171875),
        ("backfill_d9f0fd541507c197", "sell", "2026-04-11T01:19:00", 20.0, 188.495, 159.11199951171875),
        ("backfill_733f088b3cc51fea", "sell", "2026-04-28T00:41:00", 5.0, 209.45, 159.35699462890625),
    ]
    for event_id, direction, occurred_at, quantity, price, fx in nvda_events:
        el.append_event(
            event_type="trade",
            occurred_at=occurred_at,
            ticker="NVDA",
            direction=direction,
            quantity=quantity,
            price=price,
            currency="USD",
            fx_rate_usdjpy=fx,
            account="特定",
            source="backfill",
            event_id=event_id,
            db_path=db_path,
        )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 9
    corrected = _event_by_id(db_path, "backfill_c808586f792c0754:tradecorr:v1")
    assert corrected is not None
    assert corrected["account"] == "一般"
    assert _event_by_id(db_path, "manual_missing_NVDA_sell_20260507_25sh") is not None

    import tax_lot as tl

    state = tl.build_lots("NVDA", db_path=db_path)
    open_by_account = {}
    for lot in state.open_lots:
        if lot.is_open:
            open_by_account[lot.account] = open_by_account.get(lot.account, 0.0) + lot.remaining_qty
    assert open_by_account == {"一般": pytest.approx(75.0)}


def test_adds_known_missing_qcom_opening_lot_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    events = [
        ("backfill_2564aa62839e18ed", "buy", "2026-04-23T00:45:00", 1.0, 135.9591, 159.48800659179688),
        ("backfill_9b522c94825d5aa8", "buy", "2026-04-28T00:40:00", 1.0, 148.83, 159.35699462890625),
        ("backfill_f2b3edd2cdb40bea", "sell", "2026-05-14T00:56:00", 2.0, 214.02, 157.8509979248047),
        ("exec_QCOM_sell_20260519010710", "sell", "2026-05-26T20:05:46", 1.0, 203.0, 159.14100646972656),
        ("exec_QCOM_sell_20260619010129", "sell", "2026-06-21T18:46:26", 1.0, 222.22, 161.27499389648438),
    ]
    for event_id, direction, occurred_at, quantity, price, fx in events:
        el.append_event(
            event_type="trade",
            occurred_at=occurred_at,
            ticker="QCOM",
            direction=direction,
            quantity=quantity,
            price=price,
            currency="USD",
            fx_rate_usdjpy=fx,
            account="特定",
            source="api" if event_id.startswith("exec_") else "backfill",
            event_id=event_id,
            db_path=db_path,
        )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1

    import tax_lot as tl

    state = tl.build_lots("QCOM", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 3


def test_adds_known_missing_rcl_opening_lot_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-03-13T00:46:00",
        ticker="RCL",
        direction="sell",
        quantity=12.0,
        price=268.735,
        currency="USD",
        fx_rate_usdjpy=159.20599365234375,
        account="特定",
        source="backfill",
        event_id="backfill_646b7ed4cde81dbe",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1

    import tax_lot as tl

    state = tl.build_lots("RCL", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 1
    assert state.realized_trades[0].realized_jpy == pytest.approx(-2_692.0)


def test_adds_known_missing_sbux_opening_lot_for_tax_lot_rebuild(tmp_path):
    db_path = tmp_path / "ledger.db"
    el.append_event(
        event_type="trade",
        occurred_at="2026-05-13T00:08:00",
        ticker="SBUX",
        direction="sell",
        quantity=1.0,
        price=106.17,
        currency="USD",
        fx_rate_usdjpy=157.67100524902344,
        account="特定",
        source="backfill",
        event_id="backfill_477cba20e683ef53",
        db_path=db_path,
    )

    result = ltc.correct_known_trade_events(apply=True, db_path=db_path)

    assert result["corrected"] == 1

    import tax_lot as tl

    state = tl.build_lots("SBUX", db_path=db_path)
    assert sum(lot.remaining_qty for lot in state.open_lots) == 0
    assert len(state.realized_trades) == 1
    assert state.realized_trades[0].realized_jpy == pytest.approx(-156.08)

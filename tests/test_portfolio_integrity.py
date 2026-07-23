"""
tests/test_portfolio_integrity.py — 内部台帳監査
"""
import json
import sqlite3

import event_ledger as el
import portfolio_integrity as pi


def _write_base(tmp_path, *, account=None, holdings=None, cash_txs=None, executions=None):
    (tmp_path / "account.json").write_text(json.dumps(account or {
        "balance": 100_000,
        "usd_balance": 1_000,
        "fx_rate_usdjpy": 150.0,
    }), encoding="utf-8")
    (tmp_path / "holdings.json").write_text(json.dumps(holdings or {
        "CASH_JPY": {"shares": 100_000},
        "CASH_USD": {"shares": 1_000},
    }), encoding="utf-8")
    (tmp_path / "cash_transactions.json").write_text(json.dumps(cash_txs or {
        "transactions": [],
    }), encoding="utf-8")
    (tmp_path / "action_executions.json").write_text(json.dumps(executions or {
        "executions": [],
    }), encoding="utf-8")


def test_integrity_ok_for_consistent_minimal_state(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path)
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is True
    assert r["issues"] == []


def test_detects_cash_mirror_mismatch(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, holdings={
        "CASH_JPY": {"shares": 90_000},
        "CASH_USD": {"shares": 1_000},
    })
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is False
    assert any(i["check"] == "cash_mirror" for i in r["issues"])


def test_detects_account_cash_derived_total_mismatch(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, account={
        "balance": 100_000,
        "usd_balance": 1_000,
        "fx_rate_usdjpy": 151.25,
        "jpy_equivalent_usd": 149_000,
        "total_cash": 249_000,
    })

    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)

    assert r["ok"] is False
    checks = {i["check"] for i in r["issues"]}
    assert "account_jpy_equivalent_usd" in checks
    assert "account_total_cash" in checks


def test_detects_cash_transaction_without_ledger_event(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, cash_txs={
        "transactions": [{"id": "tx_missing", "amount": 10_000, "type": "deposit"}],
    })
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is False
    assert any(i["check"] == "cash_tx_ledger_link" for i in r["issues"])


def test_cash_transaction_linked_to_cash_flow_is_ok(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, cash_txs={
        "transactions": [{"id": "tx_ok", "amount": 10_000, "type": "deposit"}],
    })
    el.append_event(
        event_type="cash_flow", direction="in",
        quantity=10_000, price=1.0, currency="JPY",
        event_id="tx_ok", db_path=db,
    )
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is True


def test_detects_new_execution_without_ledger_event(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, executions={
        "executions": [{
            "id": "order_1",
            "status": "executed",
            "portfolio_applied": True,
            "event_id": "exec_order_1",
        }],
    })
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is False
    assert any(i["check"] == "execution_ledger_missing" for i in r["issues"])


def test_legacy_execution_without_event_id_is_summary_not_blocker(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, executions={
        "executions": [{
            "id": "legacy_1",
            "status": "executed",
            "portfolio_updated": True,
        }],
    })
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is True
    assert r["summary"]["legacy_executions_without_event_id"] == 1


def test_normalized_legacy_execution_without_event_id_is_summary_not_blocker(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, executions={
        "executions": [{
            "id": "legacy_normalized",
            "status": "executed",
            "portfolio_updated": True,
            "portfolio_applied": True,
            "event_id": None,
        }],
    })
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is True
    assert r["summary"]["legacy_executions_without_event_id"] == 1


def test_detects_executed_execution_not_applied_to_portfolio(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, executions={
        "executions": [{
            "id": "missed_fill",
            "ticker": "7203.T",
            "direction": "sell",
            "status": "executed",
            "quantity": 100,
            "price": 3000,
            "portfolio_applied": False,
            "portfolio_updated": False,
        }],
    })
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is False
    assert r["summary"]["unapplied_executed_count"] == 1
    assert any(i["check"] == "execution_portfolio_not_applied" for i in r["issues"])


def test_intentional_pending_application_is_advisory_not_blocker(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, executions={
        "executions": [{
            "id": "ambiguous_fill",
            "ticker": "XLF",
            "direction": "buy",
            "status": "executed",
            "quantity": 1,
            "price": 56,
            "portfolio_applied": False,
            "portfolio_application_status": "pending",
            "portfolio_application_pending": True,
            "portfolio_application_reasons": [{"code": "holding_scope_ambiguous"}],
        }],
    })
    result = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert result["ok"] is True
    assert result["summary"]["pending_portfolio_application_count"] == 1
    issue = next(i for i in result["issues"] if i["check"] == "execution_portfolio_application_pending")
    assert issue["severity"] == "advisory"


def test_externally_reconciled_execution_without_event_id_is_summary_not_blocker(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, executions={
        "executions": [{
            "id": "csv_confirmed_fill",
            "ticker": "7203.T",
            "direction": "buy",
            "status": "executed",
            "quantity": 100,
            "price": 3000,
            "externally_reconciled": True,
            "external_reconcile_source": "broker_csv:2026-05-17",
        }],
    })
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is True
    assert r["summary"]["externally_reconciled_executions"] == 1


def test_externally_reconciled_execution_requires_source(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path, executions={
        "executions": [{
            "id": "csv_confirmed_without_source",
            "ticker": "7203.T",
            "direction": "buy",
            "status": "executed",
            "externally_reconciled": True,
        }],
    })
    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is False
    assert any(i["check"] == "execution_external_reconcile_source_missing" for i in r["issues"])


def test_detects_ledger_amount_missing_inserted_by_legacy_code(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path)
    el.init_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            INSERT INTO ledger_events
              (event_id, occurred_at, recorded_at, event_type, ticker, direction,
               quantity, price, currency, amount_jpy)
            VALUES
              ('bad_fx', '2026-01-01T00:00:00', '2026-01-01T00:00:00',
               'trade', 'AAPL', 'buy', 1, 100, 'USD', NULL)
            """
        )
        conn.commit()
    finally:
        conn.close()

    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)
    assert r["ok"] is False
    assert any(i["check"] == "ledger_amount_missing" for i in r["issues"])


def test_integrity_flags_amount_missing_when_quantity_null(tmp_path):
    """Codex P1 #7: quantity/price が NULL でも amount 必須 event の amount_jpy=NULL を検出する。"""
    from event_ledger import init_schema
    db = tmp_path / "l.db"
    init_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO ledger_events "
        "(event_id, occurred_at, recorded_at, event_type, direction, quantity, price, currency, amount_jpy) "
        "VALUES ('bad1','2026-05-01T00:00:00','2026-05-01T00:00:00','cash_flow','in',NULL,NULL,'JPY',NULL)"
    )
    conn.commit()
    conn.close()
    issues = []
    pi._check_ledger_amounts(db, issues)
    assert any(i["check"] == "ledger_amount_missing" for i in issues)


def test_integrity_flags_trade_notional_outlier(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path)
    el.append_event(
        event_type="trade",
        direction="buy",
        ticker="AAPL",
        quantity=1,
        price=200_000,
        currency="USD",
        fx_rate_usdjpy=150.0,
        event_id="too_big",
        db_path=db,
    )

    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)

    assert r["ok"] is False
    issue = next(i for i in r["issues"] if i["check"] == "ledger_trade_notional_outlier")
    assert issue["event_id"] == "too_big"
    assert issue["amount_jpy"] == -30_000_000


def test_integrity_flags_domestic_fund_price_not_normalized(tmp_path):
    db = tmp_path / "ledger.db"
    _write_base(tmp_path)
    el.append_event(
        event_type="trade",
        direction="buy",
        ticker="SLIM_SP500",
        quantity=1,
        price=41675.0,
        currency="JPY",
        event_id="fund_unscaled",
        db_path=db,
    )

    r = pi.run_integrity_check(base_dir=tmp_path, db_path=db)

    assert r["ok"] is False
    issue = next(i for i in r["issues"] if i["check"] == "ledger_fund_price_not_normalized")
    assert issue["event_id"] == "fund_unscaled"
    assert issue["expected"] == "ledger price should be NAV / 10000"


def test_integrity_ignores_superseded_sign_bad_row(tmp_path):
    """Codex re-review #3: 訂正イベントに置換された旧行 (旧符号) は critical 検出しない。"""
    from event_ledger import init_schema
    db = tmp_path / "l.db"
    init_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO ledger_events "
        "(event_id, occurred_at, recorded_at, event_type, direction, quantity, price, currency, amount_jpy) "
        "VALUES ('old1','2026-05-01T00:00:00','2026-05-01T00:00:00','trade','margin_buy',2,50,'USD',15000)"
    )
    conn.execute(
        "INSERT INTO ledger_events "
        "(event_id, occurred_at, recorded_at, event_type, direction, quantity, price, currency, amount_jpy, raw_payload) "
        "VALUES ('old1:fxcorr','2026-05-01T00:00:00','2026-05-01T00:00:00','trade','margin_buy',2,50,'USD',-15000,"
        "'{\"supersedes\": \"old1\"}')"
    )
    conn.commit()
    conn.close()
    issues = []
    pi._check_ledger_amounts(db, issues)
    assert not any(i["check"] == "ledger_amount_sign" and i.get("event_id") == "old1" for i in issues)


def test_integrity_ignores_superseded_notional_outlier(tmp_path):
    from event_ledger import init_schema
    db = tmp_path / "l.db"
    init_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO ledger_events "
        "(event_id, occurred_at, recorded_at, event_type, ticker, direction, quantity, price, currency, amount_jpy) "
        "VALUES ('old_big','2026-05-01T00:00:00','2026-05-01T00:00:00','trade','SLIM_SP500','buy',191819,41675,'USD',-1277026547807.26)"
    )
    conn.execute(
        "INSERT INTO ledger_events "
        "(event_id, occurred_at, recorded_at, event_type, ticker, direction, quantity, price, currency, amount_jpy, raw_payload) "
        "VALUES ('old_big:corr','2026-05-01T00:00:00','2026-05-01T00:00:00','trade','SLIM_SP500','buy',191819,4.1675,'JPY',-799405.68,"
        "'{\"supersedes\": \"old_big\"}')"
    )
    conn.commit()
    conn.close()

    issues = []
    pi._check_ledger_amounts(db, issues)

    assert not any(i.get("event_id") == "old_big" for i in issues)

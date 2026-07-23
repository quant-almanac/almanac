"""
tests/test_broker_reconcile.py — P1-19 雛型
"""
from dataclasses import asdict

import pytest

import broker_reconcile as br
import event_ledger as el


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test_recon.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)
    return db


@pytest.fixture
def sample_csv(tmp_path):
    """汎用 parser が読み取れる UTF-8 CSV (日本語ヘッダ含む)。"""
    p = tmp_path / "rakuten_sample.csv"
    p.write_text(
        "約定日,銘柄コード,銘柄名,売買,数量,単価,通貨,口座区分,約定番号\n"
        "2026-05-10,7203,トヨタ,買付,100,3000,JPY,特定,RKT-001\n"
        "2026-05-12,AAPL,Apple,売却,5,180,USD,特定,RKT-002\n"
        "2026-05-15,9999,サンプル企業,買付,100,2500,JPY,持株会,RKT-003\n",
        encoding="utf-8",
    )
    return p


# ────────────────────────────────────────────────────────
# Parser
# ────────────────────────────────────────────────────────

def test_parse_csv_basic(sample_csv):
    trades = br.parse_csv(sample_csv, "rakuten")
    assert len(trades) == 3
    t = trades[0]
    assert t.trade_date == "2026-05-10"
    assert t.ticker == "7203.T"       # 4 桁数字 + JPY → .T 付与
    assert t.direction == "buy"
    assert t.quantity == 100
    assert t.price == 3000
    assert t.currency == "JPY"
    assert t.account == "特定"
    assert t.broker == "rakuten"
    assert t.external_id == "RKT-001"


def test_parse_csv_aapl_no_suffix(sample_csv):
    trades = br.parse_csv(sample_csv, "rakuten")
    aapl = [t for t in trades if t.ticker == "AAPL"][0]
    assert aapl.direction == "sell"
    assert aapl.currency == "USD"
    assert aapl.quantity == 5
    assert aapl.price == 180


def test_parse_csv_unknown_broker():
    with pytest.raises(ValueError):
        br.parse_csv("/dev/null", "unknown_broker")


# ────────────────────────────────────────────────────────
# Reconcile — 完全一致
# ────────────────────────────────────────────────────────

def test_reconcile_matched(tmp_db):
    """同一 trade を ledger と broker 両方に入れたら matched=1。"""
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定",
        occurred_at="2026-05-10T09:30:00",
        db_path=tmp_db,
    )
    broker_trades = [br.BrokerTrade(
        trade_date="2026-05-10", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定",
        broker="rakuten",
    )]
    rep = br.compare_to_ledger(
        broker_trades, date_from="2026-05-01", date_to="2026-05-31",
        db_path=tmp_db,
    )
    assert rep.matched_count == 1
    assert rep.only_in_broker == []
    assert rep.only_in_ledger == []
    assert rep.mismatched == []
    assert not rep.has_discrepancy


def test_reconcile_only_in_broker(tmp_db):
    """ledger に対応がない broker trade → only_in_broker。"""
    broker_trades = [br.BrokerTrade(
        trade_date="2026-05-10", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY",
        broker="rakuten",
    )]
    rep = br.compare_to_ledger(
        broker_trades, date_from="2026-05-01", date_to="2026-05-31",
        db_path=tmp_db,
    )
    assert len(rep.only_in_broker) == 1
    assert rep.matched_count == 0
    assert rep.has_discrepancy


def test_reconcile_only_in_ledger(tmp_db):
    """broker に対応がない ledger trade → only_in_ledger。"""
    el.append_event(
        event_type="trade", ticker="9999.T", direction="buy",
        quantity=10, price=1000, currency="JPY",
        occurred_at="2026-05-10T09:30:00",
        db_path=tmp_db,
    )
    rep = br.compare_to_ledger(
        [], date_from="2026-05-01", date_to="2026-05-31",
        db_path=tmp_db,
    )
    assert len(rep.only_in_ledger) == 1
    assert rep.has_discrepancy


def test_reconcile_mismatched_quantity(tmp_db):
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY",
        occurred_at="2026-05-10T09:30:00",
        db_path=tmp_db,
    )
    broker_trades = [br.BrokerTrade(
        trade_date="2026-05-10", ticker="7203.T", direction="buy",
        quantity=200,  # mismatched
        price=3000, currency="JPY",
        broker="rakuten",
    )]
    rep = br.compare_to_ledger(
        broker_trades, date_from="2026-05-01", date_to="2026-05-31",
        db_path=tmp_db,
    )
    assert len(rep.mismatched) == 1
    assert "quantity" in rep.mismatched[0].differences[0]


def test_reconcile_price_within_tolerance_is_matched(tmp_db):
    """ledger 3000 / broker 3010 (差 0.33%) は 0.5% tolerance 内で matched。"""
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY",
        occurred_at="2026-05-10T09:30:00",
        db_path=tmp_db,
    )
    broker_trades = [br.BrokerTrade(
        trade_date="2026-05-10", ticker="7203.T", direction="buy",
        quantity=100, price=3010, currency="JPY",
        broker="rakuten",
    )]
    rep = br.compare_to_ledger(
        broker_trades, date_from="2026-05-01", date_to="2026-05-31",
        db_path=tmp_db,
    )
    assert rep.matched_count == 1
    assert rep.mismatched == []


def test_reconcile_price_outside_tolerance_is_mismatched(tmp_db):
    """ledger 3000 / broker 3100 (差 3.3%) は tolerance 超え → mismatched。"""
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY",
        occurred_at="2026-05-10T09:30:00",
        db_path=tmp_db,
    )
    broker_trades = [br.BrokerTrade(
        trade_date="2026-05-10", ticker="7203.T", direction="buy",
        quantity=100, price=3100, currency="JPY",
        broker="rakuten",
    )]
    rep = br.compare_to_ledger(
        broker_trades, date_from="2026-05-01", date_to="2026-05-31",
        db_path=tmp_db,
    )
    assert len(rep.mismatched) == 1


def test_reconcile_currency_mismatch_is_mismatched(tmp_db):
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定",
        occurred_at="2026-05-10T09:30:00",
        db_path=tmp_db,
    )
    broker_trades = [br.BrokerTrade(
        trade_date="2026-05-10", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="USD", account="特定",
        broker="rakuten",
    )]
    rep = br.compare_to_ledger(
        broker_trades, date_from="2026-05-01", date_to="2026-05-31",
        db_path=tmp_db,
    )
    assert len(rep.mismatched) == 1
    assert any("currency" in d for d in rep.mismatched[0].differences)


def test_reconcile_account_mismatch_is_mismatched(tmp_db):
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="NISA成長投資枠",
        occurred_at="2026-05-10T09:30:00",
        db_path=tmp_db,
    )
    broker_trades = [br.BrokerTrade(
        trade_date="2026-05-10", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定",
        broker="rakuten",
    )]
    rep = br.compare_to_ledger(
        broker_trades, date_from="2026-05-01", date_to="2026-05-31",
        db_path=tmp_db,
    )
    assert len(rep.mismatched) == 1
    assert any("account" in d for d in rep.mismatched[0].differences)


def test_reconcile_full_flow_from_csv(tmp_db, sample_csv):
    # 楽天 CSV の 3 trade のうち 2 件だけ ledger に入れる
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定",
        occurred_at="2026-05-10T09:30:00",
        db_path=tmp_db,
    )
    el.append_event(
        event_type="trade", ticker="AAPL", direction="sell",
        quantity=5, price=180, currency="USD", account="特定",
        fx_rate_usdjpy=150.0,
        occurred_at="2026-05-12T22:00:00",
        db_path=tmp_db,
    )
    # 9999.T (持株会) は ledger に入れない → only_in_broker に出るはず

    trades = br.parse_csv(sample_csv, "rakuten")
    rep = br.compare_to_ledger(
        trades, date_from="2026-05-01", date_to="2026-05-31",
        db_path=tmp_db,
    )
    assert rep.matched_count == 2
    assert len(rep.only_in_broker) == 1
    assert rep.only_in_broker[0]["ticker"] == "9999.T"
    assert rep.has_discrepancy


# ────────────────────────────────────────────────────────
# Codex P2 #12 — parse 件数/skip 理由 + broker scope + external_id 一致
# ────────────────────────────────────────────────────────

def test_parse_with_report_surfaces_skips(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text(
        "約定日,銘柄コード,銘柄名,売買,数量,単価,通貨,口座区分,約定番号\n"
        "2026-05-10,7203,トヨタ,買付,100,3000,JPY,特定,RKT-001\n"
        "2026-05-11,9999,謎,贈与,10,100,JPY,特定,RKT-X\n"        # 売買解釈不能 → skip
        "2026-05-12,7203,トヨタ,買付,abc,3000,JPY,特定,RKT-002\n",  # 数量 ValueError → skip
        encoding="utf-8",
    )
    trades, rep = br.parse_csv_with_report(p, "rakuten")
    assert len(trades) == 1
    assert rep.rows_total == 3
    assert rep.parsed == 1
    assert rep.skipped == 2
    assert len(rep.skip_reasons) == 2
    assert all("row" in s and "reason" in s for s in rep.skip_reasons)


def test_broker_scope_excludes_other_broker_ledger(tmp_db):
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定",
        occurred_at="2026-05-10T09:30:00", db_path=tmp_db,
    )
    el.append_event(
        event_type="trade", ticker="1489.T", direction="buy",
        quantity=10, price=2000, currency="JPY", account="NISA成長投資枠（妻）",
        occurred_at="2026-05-11T09:30:00", db_path=tmp_db,
    )
    broker_trades = [br.BrokerTrade(
        trade_date="2026-05-10", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定", broker="rakuten",
    )]
    rep = br.compare_to_ledger(
        broker_trades, date_from="2026-05-01", date_to="2026-05-31",
        broker="rakuten", db_path=tmp_db,
    )
    assert rep.matched_count == 1
    assert rep.only_in_ledger == []                 # 妻(SBI) trade は scope 外 → 誤検出なし
    assert rep.scope["broker"] == "rakuten"
    assert rep.scope["ledger_total"] == 2
    assert rep.scope["ledger_in_scope"] == 1


def test_external_id_exact_match_over_key(tmp_db):
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定",
        occurred_at="2026-05-10T09:30:00",
        raw_payload={"external_id": "RKT-001"}, db_path=tmp_db,
    )
    # date が異なる (key 不一致) が external_id 一致 → matched
    broker_trades = [br.BrokerTrade(
        trade_date="2026-05-09", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定", broker="rakuten",
        external_id="RKT-001",
    )]
    rep = br.compare_to_ledger(
        broker_trades, date_from="2026-05-01", date_to="2026-05-31",
        broker="rakuten", db_path=tmp_db,
    )
    assert rep.matched_count == 1
    assert rep.only_in_broker == []
    assert rep.only_in_ledger == []


def test_duplicate_broker_trade_no_fake_mismatch(tmp_db):
    """Codex re-review #12: broker 側が重複し ledger が1件のみのとき、片方 matched・
    もう片方 only_in_broker。使用済み行を再割当てした空 diff 偽 mismatch を作らない。"""
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=3000, currency="JPY", account="特定",
        occurred_at="2026-05-10T09:30:00", db_path=tmp_db,
    )

    def _mk():
        return br.BrokerTrade(trade_date="2026-05-10", ticker="7203.T", direction="buy",
                              quantity=100, price=3000, currency="JPY", account="特定",
                              broker="rakuten")

    rep = br.compare_to_ledger([_mk(), _mk()], date_from="2026-05-01", date_to="2026-05-31",
                               broker="rakuten", db_path=tmp_db)
    assert rep.matched_count == 1
    assert len(rep.only_in_broker) == 1
    assert rep.mismatched == []

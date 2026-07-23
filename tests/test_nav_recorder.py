"""
tests/test_nav_recorder.py — P1-18-C: Modified Dietz TWR
"""
import sqlite3
from pathlib import Path

import pytest

import event_ledger as el
import nav_recorder as nr


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """test 用 DB に差し替え、event_ledger と nav_recorder の DB_PATH を上書き。"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    monkeypatch.setattr(nr, "DB_PATH", db)

    # daily_performance テーブル作成 (本来は data_fetcher.init_db で作るが test 内で直接作る)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE daily_performance (
            date            TEXT PRIMARY KEY,
            portfolio_value REAL,
            daily_pnl_jpy   REAL,
            daily_pnl_pct   REAL,
            monthly_pnl_jpy REAL,
            monthly_pnl_pct REAL,
            drawdown_pct    REAL,
            fx_rate_usdjpy  REAL,
            created_at      TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.commit()
    conn.close()

    el.init_schema(db)
    return db


def _insert_nav(db_path: Path, date_iso: str, nav: float):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO daily_performance(date, portfolio_value) VALUES (?, ?)",
        (date_iso, nav),
    )
    conn.commit()
    conn.close()


# ────────────────────────────────────────────────────────
# TWR — Modified Dietz
# ────────────────────────────────────────────────────────

def test_twr_no_cash_flow_basic(tmp_db):
    """期間内 cash flow なしなら単純リターン (V_end / V_start - 1)。"""
    _insert_nav(tmp_db, "2026-01-01", 10_000_000)
    _insert_nav(tmp_db, "2026-01-31", 10_500_000)
    r = nr.modified_dietz_twr(date_from="2026-01-01", date_to="2026-01-31", db_path=tmp_db, allow_dirty=True)
    assert r["error"] is None
    assert r["twr_pct"] == pytest.approx(5.0, abs=0.01)
    assert r["net_cash_flow"] == 0.0


def test_twr_with_mid_period_deposit(tmp_db):
    """期間中央で 100万入金 → numerator から控除されて正しい TWR が出る。"""
    _insert_nav(tmp_db, "2026-01-01", 10_000_000)
    _insert_nav(tmp_db, "2026-01-31", 11_300_000)
    # 1/16 (T=30, t=15, w=0.5) に +¥1,000,000
    el.append_event(
        event_type="cash_flow", direction="in",
        quantity=1_000_000, price=1.0, currency="JPY",
        occurred_at="2026-01-16T10:00:00",
        db_path=tmp_db,
    )
    r = nr.modified_dietz_twr(date_from="2026-01-01", date_to="2026-01-31", db_path=tmp_db, allow_dirty=True)
    # numerator = 11.3M - 10M - 1M = 300_000
    # denominator = 10M + 0.5 * 1M = 10.5M
    # twr = 300_000 / 10.5M ≈ 2.857%
    assert r["error"] is None
    assert r["twr_pct"] == pytest.approx(2.857, abs=0.01)
    assert r["net_cash_flow"] == 1_000_000.0
    assert len(r["flows"]) == 1


def test_twr_series_uses_one_origin_and_matches_period_result(tmp_db):
    """系列の各点は同一起点で再計算し、最終点は単体 TWR と一致する。"""
    _insert_nav(tmp_db, "2026-01-01", 10_000_000)
    _insert_nav(tmp_db, "2026-01-11", 11_000_000)
    _insert_nav(tmp_db, "2026-01-31", 11_300_000)
    el.append_event(
        event_type="cash_flow", direction="in",
        quantity=1_000_000, price=1.0, currency="JPY",
        occurred_at="2026-01-16T10:00:00",
        db_path=tmp_db,
    )

    series = nr.modified_dietz_twr_series(
        date_from="2026-01-01",
        date_to="2026-01-31",
        allow_dirty=True,
        db_path=tmp_db,
    )
    period = nr.modified_dietz_twr(
        date_from="2026-01-01",
        date_to="2026-01-31",
        allow_dirty=True,
        include_benchmark=False,
        db_path=tmp_db,
    )

    assert series["error"] is None
    assert series["points"] == [
        {"date": "2026-01-01", "twr_pct": 0.0},
        {"date": "2026-01-11", "twr_pct": 10.0},
        {"date": "2026-01-31", "twr_pct": pytest.approx(period["twr_pct"])},
    ]
    assert series["net_cash_flow"] == 1_000_000.0
    assert series["v_start_date"] == "2026-01-01"
    assert series["v_end_date"] == "2026-01-31"


def test_twr_with_withdrawal(tmp_db):
    """期間中に 50万円出金。numerator は出金分を戻して計算される。"""
    _insert_nav(tmp_db, "2026-03-01", 20_000_000)
    _insert_nav(tmp_db, "2026-03-31", 20_700_000)
    # 3/15 に出金 50万 (w=0.5)
    el.append_event(
        event_type="cash_flow", direction="out",
        quantity=500_000, price=1.0, currency="JPY",
        occurred_at="2026-03-16T10:00:00",  # t=15
        db_path=tmp_db,
    )
    r = nr.modified_dietz_twr(date_from="2026-03-01", date_to="2026-03-31", db_path=tmp_db, allow_dirty=True)
    # net_cash_flow = -500_000
    # numerator = 20.7M - 20M - (-0.5M) = 1.2M
    # denominator = 20M + 0.5 * (-0.5M) = 19.75M
    # twr ≈ 6.076%
    assert r["error"] is None
    assert r["twr_pct"] == pytest.approx(6.076, abs=0.01)


def test_twr_no_nav_returns_error(tmp_db):
    r = nr.modified_dietz_twr(date_from="2026-01-01", date_to="2026-01-31", db_path=tmp_db, allow_dirty=True)
    assert r["error"] is not None
    assert "NAV" in r["error"]


def test_twr_invalid_range_returns_error(tmp_db):
    _insert_nav(tmp_db, "2026-01-01", 10_000_000)
    r = nr.modified_dietz_twr(date_from="2026-01-31", date_to="2026-01-01", db_path=tmp_db, allow_dirty=True)
    assert r["error"] is not None
    assert "period" in r["error"].lower() or "日" in r["error"]


def test_twr_uses_latest_nav_on_or_before(tmp_db):
    """date_from / date_to ちょうどに NAV がなくても、直前の NAV を採用。"""
    _insert_nav(tmp_db, "2025-12-31", 10_000_000)  # date_from 直前
    _insert_nav(tmp_db, "2026-01-30", 10_400_000)  # date_to 直前
    r = nr.modified_dietz_twr(date_from="2026-01-01", date_to="2026-01-31", db_path=tmp_db, allow_dirty=True)
    assert r["error"] is None
    assert r["v_start"] == 10_000_000
    assert r["v_end"] == 10_400_000


def test_twr_trade_events_not_in_cash_flow(tmp_db):
    """trade event は cash_flow_sum に含めない (内部 flow 扱い)。"""
    _insert_nav(tmp_db, "2026-04-01", 10_000_000)
    _insert_nav(tmp_db, "2026-04-30", 10_500_000)
    # trade を入れても TWR は通常リターンと同じ
    el.append_event(
        event_type="trade", ticker="AAPL", direction="buy",
        quantity=10, price=170, currency="USD", fx_rate_usdjpy=150,
        occurred_at="2026-04-15T10:00:00",
        db_path=tmp_db,
    )
    r = nr.modified_dietz_twr(date_from="2026-04-01", date_to="2026-04-30", db_path=tmp_db, allow_dirty=True)
    assert r["error"] is None
    assert r["net_cash_flow"] == 0.0  # trade は除外
    assert r["twr_pct"] == pytest.approx(5.0, abs=0.01)


def test_twr_excludes_cash_flow_on_v_start_date(tmp_db):
    """V_start は終日 NAV なので、同日 cash_flow は期内 flow から除外する。"""
    _insert_nav(tmp_db, "2026-05-25", 10_000_000)
    _insert_nav(tmp_db, "2026-05-29", 10_500_000)
    el.append_event(
        event_type="cash_flow", direction="in",
        quantity=1_000_000, price=1.0, currency="JPY",
        occurred_at="2026-05-25T00:00:00",
        db_path=tmp_db,
    )
    r = nr.modified_dietz_twr(
        date_from="2026-05-25",
        date_to="2026-05-29",
        clean_since="2026-05-25",
        min_clean_days=1,
        db_path=tmp_db,
    )
    assert r["error"] is None
    assert r["flows"] == []
    assert r["net_cash_flow"] == 0.0
    assert r["twr_pct"] == pytest.approx(5.0, abs=0.01)


def test_twr_default_clean_gate_suppresses_pre_clean_start(tmp_db):
    """clean_since 未指定でも default gate が効き、clean 前の start NAV は使わない。"""
    _insert_nav(tmp_db, "2026-01-01", 10_000_000)
    _insert_nav(tmp_db, "2026-05-20", 11_000_000)
    _insert_nav(tmp_db, "2026-05-29", 12_000_000)
    r = nr.modified_dietz_twr(date_from="2026-01-01", date_to="2026-05-29", db_path=tmp_db)
    assert r["error"] is not None
    assert r["excess_suppressed_reason"] == "v_start_before_clean_since"
    assert r["v_start_date"] == "2026-05-20"

    dirty = nr.modified_dietz_twr(
        date_from="2026-01-01",
        date_to="2026-05-29",
        db_path=tmp_db,
        allow_dirty=True,
    )
    assert dirty["error"] is None
    assert dirty["v_start_date"] == "2026-01-01"
    assert dirty["twr_pct"] == pytest.approx(20.0, abs=0.01)


def test_cash_flow_ledger_status_requires_scheduled_semantic_match(tmp_db, monkeypatch):
    """件数だけではなく、schedule-derived flow の id/date/broker/amount を照合する。"""
    import contribution_schedule as schedule
    monkeypatch.setattr(schedule, "CONTRIBUTIONS", [
        {
            "id": "sample_monthly",
            "label": "Sample monthly contribution",
            "amount": 10_000,
            "currency": "JPY",
            "cadence": "monthly",
            "day_of_month": 25,
            "broker": "sample",
        },
        {
            "id": "sample_weekly",
            "label": "Sample weekly contribution",
            "amount": 10_000,
            "currency": "JPY",
            "cadence": "weekly",
            "weekday": 0,
            "broker": "sample",
        },
    ])
    el.append_event(
        event_type="cash_flow", direction="in",
        quantity=1, price=1.0, currency="JPY",
        account="other",
        occurred_at="2026-05-25T00:00:00",
        event_id="unrelated_cash_flow",
        db_path=tmp_db,
    )
    status = nr.cash_flow_ledger_status(date_from="2026-05-25", date_to="2026-05-25", db_path=tmp_db)
    assert status["ok"] is False
    assert status["expected_count"] == 2
    assert status["matched_count"] == 0
    assert len(status["missing_ids"]) == 2


def test_get_previous_nav_excludes_estimated(tmp_path):
    """Codex re-review #4: daily_pnl 用の前日 NAV は推定 (estimated=1) を除外する。"""
    import sqlite3
    import nav_recorder as nr
    db = tmp_path / "perf.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE daily_performance "
                 "(date TEXT PRIMARY KEY, portfolio_value REAL, estimated INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO daily_performance(date,portfolio_value,estimated) VALUES ('2026-05-01',100,0)")
    conn.execute("INSERT INTO daily_performance(date,portfolio_value,estimated) VALUES ('2026-05-09',9999,1)")
    conn.commit()
    conn.close()
    assert nr._get_previous_nav("2026-05-10", db_path=db) == 100


def test_compute_max_drawdown_uses_nav_peak_to_trough(tmp_db):
    _insert_nav(tmp_db, "2026-01-01", 100)
    _insert_nav(tmp_db, "2026-01-10", 120)
    _insert_nav(tmp_db, "2026-01-20", 90)

    result = nr.compute_max_drawdown(
        date_from="2026-01-01",
        date_to="2026-01-20",
        min_clean_days=10,
        allow_dirty=True,
        db_path=tmp_db,
    )

    assert result["dd_pct"] == pytest.approx(-25.0)
    assert result["confirmed"] is True
    assert result["period_days_actual"] == 19


def test_compute_max_drawdown_missing_nav_is_fail_safe(tmp_db):
    result = nr.compute_max_drawdown(
        date_from="2026-01-01",
        date_to="2026-01-20",
        allow_dirty=True,
        db_path=tmp_db,
    )

    assert result["dd_pct"] is None
    assert result["confirmed"] is False
    assert result["period_days_actual"] == 0

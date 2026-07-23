"""
tests/test_nav_backfill.py — 過去日 NAV 厳密再構築のテスト

設計検証:
  - anchor 整合性: NAV(today) == anchor_total_jpy
  - 巻き戻し: 過去日の cash_delta_after が D 以降の event amount_jpy の合計と一致
  - 投信 trade は skip
  - outlier event (abs(amount) > anchor × 20%) は skip
  - idempotency: upsert_daily_performance を 2 回呼んでも結果が同じ
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import event_ledger as el
import nav_backfill as nb


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test_nav.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    monkeypatch.setattr(nb, "DB_PATH", db)
    el.init_schema(db)

    # daily_performance テーブルを準備
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_performance (
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
    return db


def _stub_snapshot_and_holdings(monkeypatch, tmp_path, *, total_jpy, holdings):
    """build_portfolio_snapshot と holdings.json をモックする。"""
    h_path = tmp_path / "holdings.json"
    import json
    h_path.write_text(json.dumps(holdings))
    monkeypatch.setattr(nb, "HOLDINGS_FILE", h_path)

    import portfolio_manager as pm
    monkeypatch.setattr(pm, "build_portfolio_snapshot",
                        lambda: {"total_jpy": total_jpy, "fx_rate": 150.0})


def _stub_close_series(monkeypatch, series_by_ticker):
    """parquet 読込をスタブ。{ticker: {date: close}} を返す。"""
    def fake_load(ticker):
        return series_by_ticker.get(ticker)
    monkeypatch.setattr(nb, "_load_close_series", fake_load)


# ────────────────────────────────────────────────────────────────
# anchor 整合性: NAV(today) ≈ anchor_total
# ────────────────────────────────────────────────────────────────

def test_nav_today_equals_anchor_with_no_events(tmp_db, tmp_path, monkeypatch):
    """event ゼロなら NAV = anchor_total が全期間で成立する。"""
    _stub_snapshot_and_holdings(monkeypatch, tmp_path, total_jpy=10_000_000, holdings={
        "AAPL": {"ticker": "AAPL", "shares": 100, "currency": "USD"},
    })
    _stub_close_series(monkeypatch, {
        "AAPL":      {"2026-05-18": 170.0, "2026-05-17": 168.0, "2026-05-16": 165.0},
        "USDJPY=X":  {"2026-05-18": 150.0, "2026-05-17": 150.0, "2026-05-16": 150.0},
    })

    rows = nb.reconstruct_nav_series(
        start_date="2026-05-16", end_date="2026-05-18", db_path=tmp_db,
    )
    assert len(rows) == 3
    # 全行 NAV = 10M ではない (close が日々違うので positions_value が変動)
    # anchor (today=05-18) の整合: anchor_positions_today = 100×170×150 = 2,550,000
    # rest_value = 10M - 2.55M = 7.45M
    # 05-18 NAV = 100×170×150 + 7.45M - 0 = 10M ✓
    today_row = next(r for r in rows if r["date"] == "2026-05-18")
    assert today_row["portfolio_value"] == 10_000_000


def test_nav_changes_with_price_history(tmp_db, tmp_path, monkeypatch):
    """過去日 close が違えば NAV も変わる (rest_value 固定)。"""
    _stub_snapshot_and_holdings(monkeypatch, tmp_path, total_jpy=10_000_000, holdings={
        "AAPL": {"ticker": "AAPL", "shares": 100, "currency": "USD"},
    })
    _stub_close_series(monkeypatch, {
        "AAPL":     {"2026-05-18": 170.0, "2026-05-17": 100.0},
        "USDJPY=X": {"2026-05-18": 150.0, "2026-05-17": 150.0},
    })
    rows = nb.reconstruct_nav_series(
        start_date="2026-05-17", end_date="2026-05-18", db_path=tmp_db,
    )
    # rest_value = 10M - 100×170×150 = 7.45M
    # 05-17 NAV = 100×100×150 + 7.45M = 1.5M + 7.45M = 8.95M
    yesterday = next(r for r in rows if r["date"] == "2026-05-17")
    assert yesterday["portfolio_value"] == 8_950_000


# ────────────────────────────────────────────────────────────────
# 巻き戻し: 過去 trade を含む anchor 整合
# ────────────────────────────────────────────────────────────────

def test_past_trade_rolls_back_shares(tmp_db, tmp_path, monkeypatch):
    """過去に AAPL を 10 株買った → 買う前は AAPL shares=0 で過去 NAV 計算される。"""
    # anchor 100M、trade 240k (anchor の 0.24%) → outlier 閾値 20M 未満なので scope 内
    _stub_snapshot_and_holdings(monkeypatch, tmp_path, total_jpy=100_000_000, holdings={
        "AAPL": {"ticker": "AAPL", "shares": 100, "currency": "USD"},
    })
    _stub_close_series(monkeypatch, {
        "AAPL":     {"2026-05-18": 170.0, "2026-05-15": 160.0, "2026-05-14": 155.0},
        "USDJPY=X": {"2026-05-18": 150.0, "2026-05-15": 150.0, "2026-05-14": 150.0},
    })

    # 2026-05-15 に AAPL 10 株を $160 で BUY (amount_jpy = -10×160×150 = -240,000)
    el.append_event(
        event_type="trade", direction="buy", ticker="AAPL",
        quantity=10, price=160, currency="USD", fx_rate_usdjpy=150,
        occurred_at="2026-05-15T10:00:00",
        event_id="trade_aapl_buy",
        db_path=tmp_db,
    )

    rows = nb.reconstruct_nav_series(
        start_date="2026-05-14", end_date="2026-05-18", db_path=tmp_db,
    )

    # 05-14 (購入前) AAPL shares=90 (現状 100 - buy 10)、cash_delta_after = -240,000
    d14 = next(r for r in rows if r["date"] == "2026-05-14")
    assert d14["future_events"] == 1
    assert d14["cash_delta_after"] == -240_000

    # 05-14 NAV と 05-15 NAV の関係を確認:
    # rest_value = 100M - 100×170×150 = 100M - 2.55M = 97.45M
    # 05-14: positions = 90×155×150 = 2,092,500、NAV = 2,092,500 + 97.45M - (-240k) = 99,782,500
    # 05-15: positions = 100×160×150 = 2,400,000、NAV = 2.4M + 97.45M - 0 = 99,850,000
    # 連続性は厳密には保たれない (close 価格が日々違うため) が、外れ値ではない
    d15 = next(r for r in rows if r["date"] == "2026-05-15")
    assert d15["future_events"] == 0
    assert d15["portfolio_value"] == 99_850_000
    # 05-14 は購入前なので価格変動分だけ NAV が小さい
    assert abs(d14["portfolio_value"] - 99_782_500) < 100  # 丸め誤差許容


# ────────────────────────────────────────────────────────────────
# 投信 / outlier skip
# ────────────────────────────────────────────────────────────────

def test_fund_ticker_event_is_skipped(tmp_db, tmp_path, monkeypatch):
    """SLIM_SP500 などの投信 trade event は cash delta にも shares にも反映されない。"""
    _stub_snapshot_and_holdings(monkeypatch, tmp_path, total_jpy=10_000_000, holdings={
        "AAPL": {"ticker": "AAPL", "shares": 100, "currency": "USD"},
    })
    _stub_close_series(monkeypatch, {
        "AAPL":     {"2026-05-18": 170.0, "2026-05-14": 155.0},
        "USDJPY=X": {"2026-05-18": 150.0, "2026-05-14": 150.0},
    })

    el.append_event(
        event_type="trade", direction="buy", ticker="SLIM_SP500",
        quantity=100000, price=40000, currency="JPY",
        occurred_at="2026-05-15T10:00:00",
        event_id="fund_buy",
        db_path=tmp_db,
    )

    rows = nb.reconstruct_nav_series(
        start_date="2026-05-14", end_date="2026-05-18", db_path=tmp_db,
    )
    d14 = next(r for r in rows if r["date"] == "2026-05-14")
    # 投信 trade は skip
    assert d14["skipped_fund_evts"] == 1
    assert d14["cash_delta_after"] == 0


def test_outlier_event_is_skipped(tmp_db, tmp_path, monkeypatch):
    """abs(amount_jpy) > anchor × 20% の event は scope 外。"""
    _stub_snapshot_and_holdings(monkeypatch, tmp_path, total_jpy=10_000_000, holdings={
        "AAPL": {"ticker": "AAPL", "shares": 100, "currency": "USD"},
    })
    _stub_close_series(monkeypatch, {
        "AAPL":     {"2026-05-18": 170.0, "2026-05-14": 155.0},
        "USDJPY=X": {"2026-05-18": 150.0, "2026-05-14": 150.0},
    })

    # 4M (> 10M × 0.2 = 2M) の異常 trade
    el.append_event(
        event_type="trade", direction="sell", ticker="EWG",
        quantity=100, price=300, currency="USD", fx_rate_usdjpy=150,
        # amount_jpy = 100×300×150 = +4.5M (anchor 10M の 45%)
        occurred_at="2026-05-15T10:00:00",
        event_id="outlier_sell",
        db_path=tmp_db,
    )

    rows = nb.reconstruct_nav_series(
        start_date="2026-05-14", end_date="2026-05-18", db_path=tmp_db,
    )
    d14 = next(r for r in rows if r["date"] == "2026-05-14")
    assert d14["skipped_outlier_evts"] == 1
    # outlier はカウント対象外、cash_delta は 0
    assert d14["cash_delta_after"] == 0


# ────────────────────────────────────────────────────────────────
# upsert idempotency
# ────────────────────────────────────────────────────────────────

def test_upsert_daily_performance_is_idempotent(tmp_db, tmp_path, monkeypatch):
    """同じ row を 2 度 upsert しても daily_performance は重複しない。"""
    _stub_snapshot_and_holdings(monkeypatch, tmp_path, total_jpy=10_000_000, holdings={
        "AAPL": {"ticker": "AAPL", "shares": 100, "currency": "USD"},
    })
    _stub_close_series(monkeypatch, {
        "AAPL":     {"2026-05-18": 170.0, "2026-05-17": 168.0},
        "USDJPY=X": {"2026-05-18": 150.0, "2026-05-17": 150.0},
    })

    rows = nb.reconstruct_nav_series(
        start_date="2026-05-17", end_date="2026-05-18", db_path=tmp_db,
    )
    n1 = nb.upsert_daily_performance(rows, db_path=tmp_db)
    n2 = nb.upsert_daily_performance(rows, db_path=tmp_db)
    assert n1 == n2 == 2

    conn = sqlite3.connect(str(tmp_db))
    cnt = conn.execute("SELECT COUNT(*) FROM daily_performance").fetchone()[0]
    conn.close()
    assert cnt == 2  # 2 日分のみ、重複なし


def test_upsert_records_daily_pnl():
    """upsert 後の daily_performance に daily_pnl_jpy / pct が正しく入る。"""
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = Path(f.name)
    try:
        rows = [
            {"date": "2026-05-16", "portfolio_value": 10_000_000},
            {"date": "2026-05-17", "portfolio_value": 10_100_000},
            {"date": "2026-05-18", "portfolio_value": 10_050_000},
        ]
        nb.upsert_daily_performance(rows, db_path=db)
        conn = sqlite3.connect(str(db))
        rows_db = conn.execute(
            "SELECT date, portfolio_value, daily_pnl_jpy, daily_pnl_pct "
            "FROM daily_performance ORDER BY date"
        ).fetchall()
        conn.close()
        assert rows_db[0] == ("2026-05-16", 10_000_000, 0, 0.0)
        assert rows_db[1] == ("2026-05-17", 10_100_000, 100_000, 1.0)
        assert rows_db[2] == ("2026-05-18", 10_050_000, -50_000, pytest.approx(-0.4950, abs=0.01))
    finally:
        os.unlink(db)


# ────────────────────────────────────────────────────────────────
# helper unit tests
# ────────────────────────────────────────────────────────────────

def test_is_non_timeseries_ticker():
    assert nb._is_non_timeseries_ticker("SLIM_SP500") is True
    assert nb._is_non_timeseries_ticker("SLIM_ORCAN_WIFE") is True
    assert nb._is_non_timeseries_ticker("MNXACT") is True
    assert nb._is_non_timeseries_ticker("IFREE_FANGPLUS") is True
    assert nb._is_non_timeseries_ticker("NOMURA_SEMI") is True
    assert nb._is_non_timeseries_ticker("CASH_JPY") is True
    assert nb._is_non_timeseries_ticker("AAPL") is False
    assert nb._is_non_timeseries_ticker("9999.T") is False


def test_close_on_or_before_handles_weekends():
    series = {"2026-05-17": 100.0, "2026-05-15": 99.0}
    # 18 (月) → 17 (日、まだあるかな) → 直前の close (もし 17 が休場なら 15 へ)
    assert nb._close_on_or_before(series, "2026-05-17") == 100.0
    assert nb._close_on_or_before(series, "2026-05-16") == 99.0  # 15 にフォールバック
    assert nb._close_on_or_before(series, "2026-05-18") == 100.0


def test_detect_outlier_events_threshold():
    events = [
        {"event_id": "small",  "amount_jpy": -1_000_000},  # 1M / 10M = 10% → not outlier
        {"event_id": "large",  "amount_jpy": +3_000_000},  # 3M / 10M = 30% → outlier
        {"event_id": "none",   "amount_jpy": None},
    ]
    out = nb._detect_outlier_events(events, anchor_total_jpy=10_000_000)
    assert out == {"large"}


def test_nav_backfill_marks_rows_estimated(tmp_path):
    """Codex P1 #4: nav_backfill の行は estimated=1 で隔離される。"""
    import sqlite3
    import nav_backfill as nb
    db = tmp_path / "perf.db"
    nb.upsert_daily_performance([{"date": "2026-05-01", "portfolio_value": 1000}], db_path=db)
    conn = sqlite3.connect(str(db))
    est = conn.execute("SELECT estimated FROM daily_performance WHERE date='2026-05-01'").fetchone()[0]
    conn.close()
    assert est == 1


def test_nav_backfill_does_not_overwrite_real_row(tmp_path):
    """Codex re-review #4: 実測行 (estimated=0) を推定で上書きしない。"""
    import sqlite3
    import nav_backfill as nb
    db = tmp_path / "perf.db"
    nb.upsert_daily_performance([{"date": "2026-05-01", "portfolio_value": 12345}],
                                db_path=db, estimated=False)
    nb.upsert_daily_performance([{"date": "2026-05-01", "portfolio_value": 9999}],
                                db_path=db, estimated=True)
    conn = sqlite3.connect(str(db))
    val, est = conn.execute(
        "SELECT portfolio_value, estimated FROM daily_performance WHERE date='2026-05-01'"
    ).fetchone()
    conn.close()
    assert val == 12345
    assert est == 0


def test_real_row_overwrites_estimate(tmp_path):
    """実測行は既存の推定行を上書きできる (実測 > 推定)。"""
    import sqlite3
    import nav_backfill as nb
    db = tmp_path / "perf.db"
    nb.upsert_daily_performance([{"date": "2026-05-02", "portfolio_value": 9999}],
                                db_path=db, estimated=True)
    nb.upsert_daily_performance([{"date": "2026-05-02", "portfolio_value": 12345}],
                                db_path=db, estimated=False)
    conn = sqlite3.connect(str(db))
    val, est = conn.execute(
        "SELECT portfolio_value, estimated FROM daily_performance WHERE date='2026-05-02'"
    ).fetchone()
    conn.close()
    assert val == 12345
    assert est == 0

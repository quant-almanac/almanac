"""
tests/test_benchmark_tracker.py — JPY base benchmark
"""
import sqlite3

import pytest

import benchmark_tracker as bt


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test_bench.db"
    monkeypatch.setattr(bt, "DB_PATH", db)
    bt.init_schema(db)
    return db


def _insert_nav(db, date_iso, nav, config_hash=None):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO benchmark_daily(date, nav, config_hash) VALUES (?, ?, ?)",
        (date_iso, nav, config_hash if config_hash is not None else bt._config_hash()),
    )
    conn.commit()
    conn.close()


def test_init_schema_idempotent(tmp_db):
    bt.init_schema(tmp_db)
    bt.init_schema(tmp_db)
    conn = sqlite3.connect(str(tmp_db))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='benchmark_daily'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_get_benchmark_twr_basic(tmp_db):
    _insert_nav(tmp_db, "2026-01-01", 100.0)
    _insert_nav(tmp_db, "2026-01-31", 103.5)
    r = bt.get_benchmark_twr(date_from="2026-01-01", date_to="2026-01-31", db_path=tmp_db)
    assert r["error"] is None
    assert r["twr_pct"] == pytest.approx(3.5, abs=0.001)


def test_get_benchmark_twr_uses_on_or_before(tmp_db):
    _insert_nav(tmp_db, "2025-12-30", 100.0)
    _insert_nav(tmp_db, "2026-01-30", 105.0)
    r = bt.get_benchmark_twr(date_from="2026-01-01", date_to="2026-01-31", db_path=tmp_db)
    assert r["error"] is None
    assert r["v_start"] == 100.0
    assert r["v_end"] == 105.0
    assert r["twr_pct"] == pytest.approx(5.0, abs=0.001)


def test_get_benchmark_twr_missing_returns_error(tmp_db):
    r = bt.get_benchmark_twr(date_from="2026-01-01", date_to="2026-01-31", db_path=tmp_db)
    assert r["error"] is not None
    assert r["twr_pct"] is None


def test_excess_return_basic():
    assert bt.excess_return(7.0, 5.0) == 2.0
    assert bt.excess_return(3.0, 5.0) == -2.0


def test_excess_return_none_when_missing():
    assert bt.excess_return(None, 5.0) is None
    assert bt.excess_return(5.0, None) is None


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("BENCHMARK_EQUITY_WEIGHT", "0.7")
    monkeypatch.setenv("BENCHMARK_BOND_WEIGHT",   "0.3")
    monkeypatch.setenv("BENCHMARK_EQUITY_TICKER", "ACWI")
    cfg = bt._config()
    assert cfg["equity_weight"] == 0.7
    assert cfg["bond_weight"]   == 0.3
    assert cfg["equity_ticker"] == "ACWI"


def test_two_configs_coexist_and_twr_does_not_mix(tmp_db):
    """Codex P2 #11: composite PK (config_hash, date) で別構成が共存し、
    TWR は同一 config の NAV のみから計算される (構成混在しない)。"""
    _insert_nav(tmp_db, "2026-01-01", 100.0, config_hash="cfgA")
    _insert_nav(tmp_db, "2026-12-31", 110.0, config_hash="cfgA")   # +10%
    _insert_nav(tmp_db, "2026-01-01", 100.0, config_hash="cfgB")   # 同一日付でも衝突しない
    _insert_nav(tmp_db, "2026-12-31", 150.0, config_hash="cfgB")   # +50%

    conn = sqlite3.connect(str(tmp_db))
    n = conn.execute("SELECT COUNT(*) FROM benchmark_daily").fetchone()[0]
    conn.close()
    assert n == 4  # 4 行が共存 (date 単独 PK なら 2 行に潰れていた)

    a = bt.get_benchmark_twr(date_from="2026-01-01", date_to="2026-12-31",
                             config_hash="cfgA", db_path=tmp_db)
    b = bt.get_benchmark_twr(date_from="2026-01-01", date_to="2026-12-31",
                             config_hash="cfgB", db_path=tmp_db)
    assert a["twr_pct"] == pytest.approx(10.0)
    assert b["twr_pct"] == pytest.approx(50.0)


def test_init_schema_migrates_legacy_date_pk(tmp_path, monkeypatch):
    """旧スキーマ (date 単独 PK) の DB を composite PK へ移行し、行を保全する。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE benchmark_daily (date TEXT PRIMARY KEY, nav REAL NOT NULL, "
        "equity_close REAL, bond_close REAL, fx_usdjpy REAL, config_hash TEXT, "
        "created_at TEXT);"
        "INSERT INTO benchmark_daily(date, nav, config_hash) VALUES ('2026-01-01', 100.0, NULL);"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(bt, "DB_PATH", db)
    bt.init_schema(db)  # migrate
    conn = sqlite3.connect(str(db))
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='benchmark_daily'").fetchone()[0]
    cnt = conn.execute("SELECT COUNT(*) FROM benchmark_daily").fetchone()[0]
    conn.close()
    assert "PRIMARY KEY (config_hash, date)" in sql
    assert cnt == 1  # 既存行は保全


def test_legacy_rows_visible_after_migration(tmp_path, monkeypatch):
    """Codex re-review #11: 旧 NULL config_hash 行は移行後、現行 config の TWR から見える。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE benchmark_daily (date TEXT PRIMARY KEY, nav REAL NOT NULL, "
        "equity_close REAL, bond_close REAL, fx_usdjpy REAL, config_hash TEXT, created_at TEXT);"
        "INSERT INTO benchmark_daily(date,nav,config_hash) VALUES ('2026-01-01',100.0,NULL);"
        "INSERT INTO benchmark_daily(date,nav,config_hash) VALUES ('2026-12-31',120.0,NULL);"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(bt, "DB_PATH", db)
    bt.init_schema(db)  # migrate
    res = bt.get_benchmark_twr(date_from="2026-01-01", date_to="2026-12-31", db_path=db)
    assert res["twr_pct"] == pytest.approx(20.0)  # 旧行が現行 hash に割当てられ可視


def test_backfill_empty_config_hash_on_composite_db(tmp_path, monkeypatch):
    """Codex re-re-review #11: 既に composite PK だが config_hash='' の行も現行 hash に移す。"""
    db = tmp_path / "bad.db"
    monkeypatch.setattr(bt, "DB_PATH", db)
    bt.init_schema(db)  # composite schema 作成
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO benchmark_daily(date, nav, config_hash) VALUES ('2026-01-01',100.0,'')")
    conn.execute("INSERT INTO benchmark_daily(date, nav, config_hash) VALUES ('2026-12-31',130.0,'')")
    conn.commit()
    conn.close()
    # get_benchmark_twr 内の init_schema が backfill を実行 → 現行 config で可視に
    res = bt.get_benchmark_twr(date_from="2026-01-01", date_to="2026-12-31", db_path=db)
    assert res["twr_pct"] == pytest.approx(30.0)
    conn = sqlite3.connect(str(db))
    n_empty = conn.execute(
        "SELECT COUNT(*) FROM benchmark_daily WHERE config_hash='' OR config_hash IS NULL"
    ).fetchone()[0]
    conn.close()
    assert n_empty == 0

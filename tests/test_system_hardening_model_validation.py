import json
import sqlite3
from dataclasses import dataclass

import pandas as pd
import pytest

import benchmark_tracker as bt
import broker_reconcile as br
import data_fetcher as df
import event_ledger as el
import risk_model_validation as rmv
import tax_lot
import watchdog
import weekly_report


def test_kupiec_pof_passes_expected_rate_and_rejects_bad_rate():
    passing = rmv.kupiec_pof([True] * 5 + [False] * 95, confidence=0.95)
    failing = rmv.kupiec_pof([True] * 20 + [False] * 80, confidence=0.95)
    assert passing["passed"] is True
    assert failing["passed"] is False
    assert failing["p_value"] < 0.05


def test_var_forecasts_pair_with_next_clean_realized_day(tmp_path):
    db = tmp_path / "var.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE daily_performance "
            "(date TEXT PRIMARY KEY, daily_pnl_pct REAL, estimated INTEGER DEFAULT 0)"
        )
        conn.executemany(
            "INSERT INTO daily_performance(date, daily_pnl_pct, estimated) VALUES (?, ?, ?)",
            [
                ("2026-06-01", -1.0, 0),
                ("2026-06-02", -3.0, 0),
                ("2026-06-03", -0.5, 0),
            ],
        )
    rmv.record_forecast({
        "forecast_date": "2026-06-01",
        "confidence": 0.95,
        "var_pct": 2.0,
        "model": "cornish_fisher_daily_performance",
        "sample_size": 90,
    }, db_path=db)
    observations = rmv.load_backtest_observations(db_path=db)
    assert observations == [{
        "forecast_date": "2026-06-01",
        "realized_date": "2026-06-02",
        "var_pct": 2.0,
        "realized_pnl_pct": -3.0,
        "exception": True,
    }]


def test_var_forecast_estimation_uses_clean_daily_history(tmp_path):
    db = tmp_path / "var_history.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE daily_performance "
            "(date TEXT PRIMARY KEY, daily_pnl_pct REAL, estimated INTEGER DEFAULT 0)"
        )
        conn.executemany(
            "INSERT INTO daily_performance(date, daily_pnl_pct, estimated) VALUES (?, ?, ?)",
            [
                (f"2026-05-{i:02d}", (0.4 if i % 2 else -0.5), 0)
                for i in range(1, 21)
            ],
        )
        conn.execute(
            "INSERT INTO daily_performance(date, daily_pnl_pct, estimated) VALUES (?, ?, ?)",
            ("2026-05-21", -99.0, 1),
        )
    result = rmv.estimate_var_from_daily_history(as_of="2026-05-21", db_path=db)
    assert result["sample_size"] == 20
    assert 0 < result["var_pct"] < 10


def test_price_sanity_flags_are_deterministic_and_deduplicated(tmp_path):
    frame = pd.DataFrame(
        {"Close": [100.0, 131.0, 129.0]},
        index=pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"]),
    )
    flags = df.detect_price_sanity_flags("TEST", frame)
    assert len(flags) == 1
    assert flags[0]["daily_change_pct"] == pytest.approx(31.0)
    path = tmp_path / "flags.jsonl"
    assert df.append_price_sanity_flags(flags, path=path) == 1
    assert df.append_price_sanity_flags(flags, path=path) == 0
    assert len(path.read_text().splitlines()) == 1


def test_price_sanity_review_row_supersedes_review_required(tmp_path, monkeypatch):
    path = tmp_path / "flags.jsonl"
    flags = [{
        "flag_id": "f1",
        "ticker": "1306.T",
        "status": "review_required",
    }]
    assert df.append_price_sanity_flags(flags, path=path) == 1
    df.append_price_sanity_review(
        flag_id="f1",
        ticker="1306.T",
        status="resolved",
        resolution="confirmed_split_adjustment",
        reviewer="test",
        evidence=[{"source": "unit-test"}],
        path=path,
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "price_sanity_flags.jsonl"
    target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(watchdog, "BASE_DIR", tmp_path)

    assert watchdog._check_price_sanity() == []


def test_watchdog_surfaces_single_source_price_review(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "price_sanity_flags.jsonl").write_text(
        json.dumps({"ticker": "TEST", "status": "review_required"}) + "\n"
    )
    monkeypatch.setattr(watchdog, "BASE_DIR", tmp_path)
    assert watchdog._check_price_sanity() == [{
        "ticker": "TEST",
        "status": "review_required",
    }]


@dataclass
class Position:
    ticker: str
    account: str
    quantity: float
    entry_price: float
    currency: str = "JPY"
    broker: str = "楽天証券"


def test_tax_cost_basis_reconcile_detects_total_average_difference(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=1000, currency="JPY", account="特定",
        occurred_at="2026-01-01T09:00:00", db_path=db,
    )
    el.append_event(
        event_type="trade", ticker="7203.T", direction="buy",
        quantity=100, price=1200, currency="JPY", account="特定",
        occurred_at="2026-02-01T09:00:00", db_path=db,
    )
    report = br.compare_tax_cost_basis(
        [Position("7203.T", "特定", 200, 1150)],
        db_path=db,
    )
    assert report["has_discrepancy"] is True
    assert report["discrepancies"][0]["internal_weighted_average_price"] == 1100


def _insert_alpha_data(db, *, beta=1.5, alpha_daily=0.001, days=30):
    bt.init_schema(db)
    nav = 100.0
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE daily_performance "
            "(date TEXT PRIMARY KEY, daily_pnl_pct REAL, estimated INTEGER DEFAULT 0)"
        )
        for i in range(days):
            day = f"2026-01-{i + 1:02d}"
            benchmark_return = 0.001 if i % 2 == 0 else -0.0005
            nav *= 1.0 + benchmark_return
            portfolio_return = alpha_daily + beta * benchmark_return
            conn.execute(
                "INSERT INTO daily_performance(date, daily_pnl_pct, estimated) VALUES (?, ?, 0)",
                (day, portfolio_return * 100.0),
            )
            conn.execute(
                "INSERT INTO benchmark_daily(date, nav, config_hash) VALUES (?, ?, ?)",
                (day, nav, bt._config_hash()),
            )


def test_beta_adjusted_alpha_and_weekly_format(tmp_path, monkeypatch):
    db = tmp_path / "alpha.db"
    _insert_alpha_data(db)
    result = bt.get_beta_adjusted_alpha(
        date_from="2026-01-01",
        date_to="2026-01-30",
        db_path=db,
        allow_dirty=True,
    )
    assert result["beta"] == pytest.approx(1.5, abs=0.02)
    assert result["alpha_pct_annualized"] == pytest.approx(25.2, abs=0.2)

    monkeypatch.setattr(
        bt,
        "get_beta_adjusted_alpha",
        lambda **kwargs: {"n": 25, "beta": 1.2, "alpha_pct_annualized": 3.4, "error": None},
    )
    line = weekly_report._fmt_beta_adjusted_alpha()
    assert "3.40%" in line
    assert "beta=1.20" in line


def test_legacy_wfo_declared_retired():
    import backtest_wfo

    assert backtest_wfo.BACKTEST_STATUS == "retired"

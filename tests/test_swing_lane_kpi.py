"""tests/test_swing_lane_kpi.py — 攻めバックログ 2026-07 項目5(前半): Swingレーン KPI"""
from pathlib import Path

import pytest

import event_ledger as el
import swing_lane_kpi as sk


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test_swing.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)
    return db


def _add_buy(db, *, ticker, qty, price, date, account="特定", event_id=None):
    return el.append_event(
        event_type="trade", direction="buy",
        ticker=ticker, quantity=qty, price=price,
        currency="JPY", account=account,
        occurred_at=date + "T10:00:00",
        event_id=event_id,
        db_path=db,
    )


def _add_sell(db, *, ticker, qty, price, date, account="特定", event_id=None):
    return el.append_event(
        event_type="trade", direction="sell",
        ticker=ticker, quantity=qty, price=price,
        currency="JPY", account=account,
        occurred_at=date + "T10:00:00",
        event_id=event_id,
        db_path=db,
    )


def test_no_closed_trades_is_insufficient_data(tmp_db):
    result = sk.compute_swing_kpis(tickers=frozenset({"TXN", "ANET"}), db_path=tmp_db)
    assert result["n_closed"] == 0
    assert result["verdict"] == "insufficient_data"


def test_single_win_computes_kpis_and_hold_days(tmp_db):
    _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date="2026-04-25")
    _add_sell(tmp_db, ticker="TXN", qty=1, price=150, date="2026-05-05")

    result = sk.compute_swing_kpis(tickers=frozenset({"TXN"}), db_path=tmp_db)
    assert result["n_closed"] == 1
    assert result["win_rate"] == 1.0
    assert result["profit_factor"] is None  # 損失0件のため未定義
    assert result["avg_hold_days"] == 10.0
    assert result["expected_value_jpy"] == 50
    assert result["max_single_loss_jpy"] == 0  # 損失トレードが無いため0 (勝ちトレードを損失表示しない)
    assert result["verdict"] == "insufficient_data"  # n=1 < 20


def test_max_single_loss_is_zero_not_smallest_win_when_all_trades_win(tmp_db):
    # 実データ(TXN/ANET)で発見: 2勝しかないのに小さい方の勝ち額を「最大損失」として
    # 誤表示していたバグの回帰テスト。全勝なら max_single_loss_jpy は 0 であるべき。
    _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date="2026-01-01", event_id="b1")
    _add_sell(tmp_db, ticker="TXN", qty=1, price=200, date="2026-01-11", event_id="s1")  # +100
    _add_buy(tmp_db, ticker="ANET", qty=1, price=100, date="2026-02-01", event_id="b2")
    _add_sell(tmp_db, ticker="ANET", qty=1, price=140, date="2026-02-11", event_id="s2")  # +40 (小さい方の勝ち)

    result = sk.compute_swing_kpis(tickers=frozenset({"TXN", "ANET"}), db_path=tmp_db)
    assert result["n_closed"] == 2
    assert result["max_single_loss_jpy"] == 0


def test_hold_days_none_when_lot_not_found(tmp_db):
    # SELLだけが存在する状況はbuild_lotsがValueErrorを出すため対象外だが、
    # ここでは正常なBUY+SELLペアでhold_daysが必ず算出されることを確認する
    _add_buy(tmp_db, ticker="ANET", qty=1, price=140, date="2026-05-01")
    _add_sell(tmp_db, ticker="ANET", qty=1, price=130, date="2026-05-11")

    result = sk.compute_swing_kpis(tickers=frozenset({"ANET"}), db_path=tmp_db)
    assert result["n_closed"] == 1
    assert result["avg_hold_days"] == 10.0
    assert result["max_single_loss_jpy"] == -10


def test_win_rate_and_profit_factor_mixed_trades(tmp_db):
    # 2勝1敗: +100, +50, -60
    _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date="2026-01-01", event_id="b1")
    _add_sell(tmp_db, ticker="TXN", qty=1, price=200, date="2026-01-11", event_id="s1")
    _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date="2026-02-01", event_id="b2")
    _add_sell(tmp_db, ticker="TXN", qty=1, price=150, date="2026-02-11", event_id="s2")
    _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date="2026-03-01", event_id="b3")
    _add_sell(tmp_db, ticker="TXN", qty=1, price=40, date="2026-03-11", event_id="s3")

    result = sk.compute_swing_kpis(tickers=frozenset({"TXN"}), db_path=tmp_db)
    assert result["n_closed"] == 3
    assert result["win_rate"] == pytest.approx(round(2 / 3, 4))
    assert result["profit_factor"] == pytest.approx(round(150 / 60, 4))
    assert result["max_single_loss_jpy"] == -60


def test_verdict_insufficient_data_below_20_trades(tmp_db):
    for i in range(5):
        _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date=f"2026-01-{i+1:02d}", event_id=f"b{i}")
        _add_sell(tmp_db, ticker="TXN", qty=1, price=200, date=f"2026-01-{i+1:02d}", event_id=f"s{i}")
    result = sk.compute_swing_kpis(tickers=frozenset({"TXN"}), db_path=tmp_db)
    assert result["n_closed"] == 5
    assert result["verdict"] == "insufficient_data"


def test_verdict_promote_when_profit_factor_and_ev_meet_threshold(tmp_db):
    # 20トレード全勝 (+100 each) -> profit_factor は損失0件でNoneになるため、
    # promote判定には損失を最低1件混ぜてprofit_factorを実数にする必要がある。
    # 19勝(+100) + 1敗(-50) -> profit_factor = 1900/50 = 38 >= 1.3, EV > 0
    for i in range(19):
        _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date=f"2026-01-{(i % 28) + 1:02d}", event_id=f"bw{i}")
        _add_sell(tmp_db, ticker="TXN", qty=1, price=200, date=f"2026-02-{(i % 28) + 1:02d}", event_id=f"sw{i}")
    _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date="2026-03-01", event_id="bl0")
    _add_sell(tmp_db, ticker="TXN", qty=1, price=50, date="2026-03-11", event_id="sl0")

    result = sk.compute_swing_kpis(tickers=frozenset({"TXN"}), db_path=tmp_db)
    assert result["n_closed"] == 20
    assert result["verdict"] == "promote"


def test_verdict_demote_when_profit_factor_below_threshold(tmp_db):
    # 5勝(+10) + 15敗(-100) -> profit_factor = 50/1500 = 0.033 < 0.8
    for i in range(5):
        _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date=f"2026-01-{(i % 28) + 1:02d}", event_id=f"bw{i}")
        _add_sell(tmp_db, ticker="TXN", qty=1, price=110, date=f"2026-01-{(i % 28) + 1:02d}", event_id=f"sw{i}")
    for i in range(15):
        _add_buy(tmp_db, ticker="TXN", qty=1, price=100, date=f"2026-02-{(i % 28) + 1:02d}", event_id=f"bl{i}")
        _add_sell(tmp_db, ticker="TXN", qty=1, price=0.01, date=f"2026-02-{(i % 28) + 1:02d}", event_id=f"sl{i}")

    result = sk.compute_swing_kpis(tickers=frozenset({"TXN"}), db_path=tmp_db)
    assert result["n_closed"] == 20
    assert result["verdict"] == "demote"


def test_unknown_ticker_with_no_events_is_skipped_not_error(tmp_db):
    result = sk.compute_swing_kpis(tickers=frozenset({"NONEXISTENT_TICKER_XYZ"}), db_path=tmp_db)
    assert result["n_closed"] == 0
    assert result["verdict"] == "insufficient_data"


def test_default_tickers_and_real_production_data_smoke():
    # 本番DB(引数なし)に対する読み取り専用smoke test。TXN/ANETは実際にクローズ済みのため
    # クラッシュしないこと、かつ n_closed >= 2 (実データ) であることを確認する。
    if not (Path(__file__).resolve().parents[1] / "tickers.json").exists():
        pytest.skip("private production event ledger is intentionally excluded")
    result = sk.compute_swing_kpis()
    assert result["n_closed"] >= 2
    assert result["verdict"] in {"insufficient_data", "promote", "maintain", "demote"}

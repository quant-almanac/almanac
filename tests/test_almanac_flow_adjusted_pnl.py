"""相場暦の日次損益から入出金を除く回帰テスト。

背景 (2026-08-16): behavioral_guard.snapshot_portfolio_pnl() が書く daily_pnl_jpy は
「総評価額の前日差分」で、総評価額には現金が含まれる (portfolio_manager:403)。
そのため ¥100,000 を入金しただけの日が +¥100,000 の「利益」として相場暦に出ていた。

この家計には定期積立が月20万円ほどある (楽天クレカ ¥100,000/月・妻SBI ¥23,076/週)
ため、そのまま出すと実力を大きく過大表示する。実際 2026-08-01 は
¥100,000 の積立に隠れて −¥206,521 の損失が −¥106,521 に見えていた。

event_ledger の cash_flow を同日分だけ差し引いて純粋な売買損益に直す。
"""
from datetime import datetime

from api.routes import today


def test_cash_flow_by_date_groups_and_sums_same_day_events(monkeypatch):
    events = [
        {"occurred_at": "2026-08-01T00:00:00", "amount_jpy": 100000.0},
        {"occurred_at": "2026-08-03T00:00:00", "amount_jpy": 23076.0},
        {"occurred_at": "2026-08-03T09:00:00", "amount_jpy": 1000.0},  # 同日2件は合算
    ]
    monkeypatch.setitem(
        __import__("sys").modules, "event_ledger",
        type("M", (), {"query_events": staticmethod(lambda **_kw: events)})(),
    )

    by_date = today._cash_flow_by_date(datetime(2026, 7, 20), datetime(2026, 8, 16))

    assert by_date["2026-08-01"] == 100000.0
    assert by_date["2026-08-03"] == 24076.0


def test_cash_flow_by_date_fails_open_when_ledger_unreadable(monkeypatch):
    """台帳が読めなくても表示は止めない（差し引き無しで従来どおり）。"""
    def boom(**_kw):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setitem(
        __import__("sys").modules, "event_ledger",
        type("M", (), {"query_events": staticmethod(boom)})(),
    )

    assert today._cash_flow_by_date(datetime(2026, 7, 20), datetime(2026, 8, 16)) == {}


def test_cash_flow_by_date_skips_malformed_rows(monkeypatch):
    events = [
        {"occurred_at": "2026-08-01T00:00:00", "amount_jpy": 100000.0},
        {"occurred_at": None, "amount_jpy": 5000.0},          # 日付なし
        {"occurred_at": "2026-08-02T00:00:00", "amount_jpy": "abc"},  # 金額が数値でない
    ]
    monkeypatch.setitem(
        __import__("sys").modules, "event_ledger",
        type("M", (), {"query_events": staticmethod(lambda **_kw: events)})(),
    )

    by_date = today._cash_flow_by_date(datetime(2026, 7, 20), datetime(2026, 8, 16))

    assert by_date == {"2026-08-01": 100000.0}


def test_deposit_day_is_not_reported_as_profit(monkeypatch):
    """本命: 入金しただけの日を利益として見せない。"""
    monkeypatch.setattr(
        today, "_cash_flow_by_date",
        lambda *_a, **_kw: {"2026-08-01": 100000.0, "2026-08-03": 23076.0},
    )

    guard = {"pnl_history": [
        # 入金 ¥100,000 のみで実際の売買損益はゼロの日
        {"date": "2026-08-01", "pnl_jpy": 100000.0},
        # 入金 ¥23,076 + 実際の売買益 ¥47,169
        {"date": "2026-08-03", "pnl_jpy": 70245.0},
        # 入金なしの日はそのまま
        {"date": "2026-08-04", "pnl_jpy": -31000.0},
    ]}

    almanac = today._build_almanac(
        board=[], analysis={}, currency={}, nisa={},
        now=datetime(2026, 8, 16), guard=guard,
    )
    pnl = almanac["pnl_by_date"]

    assert pnl["2026-08-01"] == 0, "入金だけの日が利益として残っている"
    assert pnl["2026-08-03"] == 47169
    assert pnl["2026-08-04"] == -31000, "入出金の無い日を書き換えてはいけない"


def test_deposit_can_flip_an_apparent_gain_into_the_real_loss(monkeypatch):
    """実データで起きていたケース: 積立に隠れて損失が半分に見えていた。"""
    monkeypatch.setattr(today, "_cash_flow_by_date", lambda *_a, **_kw: {"2026-08-01": 100000.0})

    almanac = today._build_almanac(
        board=[], analysis={}, currency={}, nisa={},
        now=datetime(2026, 8, 16),
        guard={"pnl_history": [{"date": "2026-08-01", "pnl_jpy": -106521.0}]},
    )

    assert almanac["pnl_by_date"]["2026-08-01"] == -206521

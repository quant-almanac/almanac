"""
tests/test_tax_lot.py — P2 Tax Lot solver
"""
import pytest

import event_ledger as el
import tax_lot as tl


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test_lots.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)
    return db


def _add_buy(db, *, ticker, qty, price, date, currency="JPY", fx=None, account="特定", event_id=None):
    return el.append_event(
        event_type="trade", direction="buy",
        ticker=ticker, quantity=qty, price=price,
        currency=currency, fx_rate_usdjpy=fx,
        account=account,
        occurred_at=date + "T10:00:00",
        event_id=event_id,
        db_path=db,
    )


def _add_sell(db, *, ticker, qty, price, date, currency="JPY", fx=None, account="特定", event_id=None):
    return el.append_event(
        event_type="trade", direction="sell",
        ticker=ticker, quantity=qty, price=price,
        currency=currency, fx_rate_usdjpy=fx,
        account=account,
        occurred_at=date + "T10:00:00",
        event_id=event_id,
        db_path=db,
    )


# ────────────────────────────────────────────────────────
# build_lots
# ────────────────────────────────────────────────────────

def test_build_lots_basic(tmp_db):
    """BUY 3 件で 3 lot ができる。"""
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=150, date="2026-01-10")
    _add_buy(tmp_db, ticker="AAPL", qty=5,  price=160, date="2026-02-10")
    _add_buy(tmp_db, ticker="AAPL", qty=8,  price=170, date="2026-03-10")
    st = tl.build_lots("AAPL", db_path=tmp_db)
    assert len(st.open_lots) == 3
    assert st.open_lots[0].purchase_date == "2026-01-10"
    assert st.open_lots[0].remaining_qty == 10
    assert all(l.is_open for l in st.open_lots)


def test_fifo_consumption_on_sell(tmp_db):
    """SELL 12 株 → 古い lot (10株) を全消費、次の lot (5株) から 2 株消費。"""
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=150, date="2026-01-10")
    _add_buy(tmp_db, ticker="AAPL", qty=5,  price=160, date="2026-02-10")
    _add_sell(tmp_db, ticker="AAPL", qty=12, price=180, date="2026-03-15")

    st = tl.build_lots("AAPL", db_path=tmp_db)
    # lot 1 は全消費 (remaining 0)
    assert st.open_lots[0].remaining_qty == pytest.approx(0)
    # lot 2 は 3 株残
    assert st.open_lots[1].remaining_qty == pytest.approx(3)
    # realized 2 件
    assert len(st.realized_trades) == 2
    # 1 件目: 10株 × (180-150) = +300
    assert st.realized_trades[0].realized_jpy == pytest.approx(300)
    # 2 件目: 2 株 × (180-160) = +40
    assert st.realized_trades[1].realized_jpy == pytest.approx(40)


def test_usd_lot_jpy_conversion(tmp_db):
    """USD lot は fx_rate で cost_per_share_jpy が計算される。"""
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=150, date="2026-01-10",
             currency="USD", fx=145)
    st = tl.build_lots("AAPL", db_path=tmp_db)
    lot = st.open_lots[0]
    assert lot.cost_per_share == 150
    assert lot.cost_per_share_jpy == pytest.approx(150 * 145)


def test_usd_buy_without_fx_raises(tmp_db):
    """USD lot は FX 欠損を 0 円や USD 単価のまま処理しない。"""
    with pytest.raises(ValueError, match="fx_rate_usdjpy"):
        _add_buy(tmp_db, ticker="AAPL", qty=10, price=150, date="2026-01-10",
                 currency="USD", fx=None)


def test_usd_sell_without_fx_raises(tmp_db):
    """USD SELL の proceeds は売却時 FX が必須。"""
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=150, date="2026-01-10",
             currency="USD", fx=145)
    with pytest.raises(ValueError, match="fx_rate_usdjpy"):
        _add_sell(tmp_db, ticker="AAPL", qty=10, price=180, date="2026-04-10",
                  currency="USD", fx=None)


def test_usd_realized_uses_sell_fx(tmp_db):
    """USD SELL の proceeds_jpy は売却時 fx を使う (BUY 時 fx と独立)。"""
    _add_buy(tmp_db,  ticker="AAPL", qty=10, price=150, date="2026-01-10",
             currency="USD", fx=145)
    _add_sell(tmp_db, ticker="AAPL", qty=10, price=180, date="2026-04-10",
              currency="USD", fx=155)
    st = tl.build_lots("AAPL", db_path=tmp_db)
    rt = st.realized_trades[0]
    # cost_basis = 10 * 150 * 145 = 217,500
    # proceeds  = 10 * 180 * 155 = 279,000
    # realized   = +61,500
    assert rt.cost_basis_jpy == pytest.approx(217500)
    assert rt.proceeds_jpy == pytest.approx(279000)
    assert rt.realized_jpy == pytest.approx(61500)


# ────────────────────────────────────────────────────────
# recommend_sell_lots
# ────────────────────────────────────────────────────────

def test_recommend_fifo(tmp_db):
    _add_buy(tmp_db, ticker="X", qty=10, price=100, date="2026-01-10")
    _add_buy(tmp_db, ticker="X", qty=10, price=200, date="2026-02-10")
    r = tl.recommend_sell_lots("X", quantity=15, current_price=250,
                               mode="fifo", db_path=tmp_db)
    assert r["mode"] == "fifo"
    assert len(r["plan"]) == 2
    assert r["plan"][0]["purchase_date"] == "2026-01-10"
    assert r["plan"][0]["quantity"] == 10
    assert r["plan"][1]["quantity"] == 5
    # est_realized: 10*(250-100) + 5*(250-200) = 1500 + 250 = 1750
    assert r["total_realized_jpy"] == pytest.approx(1750)


def test_recommend_loss_harvest_picks_biggest_loss_first(tmp_db):
    """含み損が一番大きい lot を先に売る。"""
    _add_buy(tmp_db, ticker="X", qty=5, price=300, date="2026-01-10")   # 含み損大
    _add_buy(tmp_db, ticker="X", qty=5, price=200, date="2026-02-10")   # 含み損中
    _add_buy(tmp_db, ticker="X", qty=5, price=100, date="2026-03-10")   # 含み益
    r = tl.recommend_sell_lots("X", quantity=5, current_price=150,
                               mode="loss_harvest", db_path=tmp_db)
    # 価格 150 → lot1 (cost 300): -750 / lot2 (cost 200): -250 / lot3 (cost 100): +250
    # loss_harvest なので lot1 (最大損) を先に売る
    assert r["plan"][0]["cost_per_share_jpy"] == 300
    assert r["total_realized_jpy"] == pytest.approx(-750)


def test_recommend_account_filter_excludes_nisa(tmp_db):
    """account_filter='特定' → NISA lot を除外。"""
    _add_buy(tmp_db, ticker="X", qty=10, price=100, date="2026-01-10", account="NISA成長投資枠")
    _add_buy(tmp_db, ticker="X", qty=10, price=200, date="2026-02-10", account="特定")
    r = tl.recommend_sell_lots("X", quantity=10, current_price=250,
                               mode="fifo", account_filter="特定", db_path=tmp_db)
    assert len(r["plan"]) == 1
    assert r["plan"][0]["account"] == "特定"
    assert r["plan"][0]["cost_per_share_jpy"] == 200


def test_recommend_insufficient_lots_returns_unfulfilled(tmp_db):
    """売却量 > 保有量 → unfulfilled_qty に残る。"""
    _add_buy(tmp_db, ticker="X", qty=5, price=100, date="2026-01-10")
    r = tl.recommend_sell_lots("X", quantity=10, current_price=150,
                               mode="fifo", db_path=tmp_db)
    assert r["unfulfilled_qty"] == 5
    assert sum(p["quantity"] for p in r["plan"]) == 5


def test_recommend_invalid_mode_raises(tmp_db):
    with pytest.raises(ValueError):
        tl.recommend_sell_lots("X", quantity=1, current_price=100,
                               mode="unknown", db_path=tmp_db)


# ────────────────────────────────────────────────────────
# realized_pnl_in_year
# ────────────────────────────────────────────────────────

def test_realized_pnl_year_aggregation(tmp_db):
    _add_buy(tmp_db,  ticker="A", qty=10, price=100, date="2026-01-10")
    _add_sell(tmp_db, ticker="A", qty=10, price=150, date="2026-04-10")  # +500
    _add_buy(tmp_db,  ticker="B", qty=5,  price=200, date="2026-02-10", account="NISA成長投資枠")
    _add_sell(tmp_db, ticker="B", qty=5,  price=180, date="2026-05-10", account="NISA成長投資枠")  # -100
    # 別年の trade は除外される
    _add_buy(tmp_db,  ticker="C", qty=10, price=100, date="2025-06-10")
    _add_sell(tmp_db, ticker="C", qty=10, price=999, date="2025-12-10")

    r = tl.realized_pnl_in_year(2026, db_path=tmp_db)
    assert r["year"] == 2026
    assert r["realized_jpy"] == pytest.approx(400)  # +500 + (-100)
    assert r["trade_count"] == 2
    assert r["by_ticker"]["A"] == pytest.approx(500)
    assert r["by_ticker"]["B"] == pytest.approx(-100)
    assert "特定" in r["by_account"]
    assert "NISA成長投資枠" in r["by_account"]


def test_realized_pnl_year_v2_separates_nisa_from_taxable(tmp_db):
    """Stage 5B: NISA は除外でなく分離。同じシナリオで v1 は 400 に
    混ざっていた +500 (課税) と -100 (NISA) を v2 は分けて返す
    (プラン記載の canonical 例と同じ数字: 500 / -100 / 400)。"""
    _add_buy(tmp_db,  ticker="A", qty=10, price=100, date="2026-01-10")
    _add_sell(tmp_db, ticker="A", qty=10, price=150, date="2026-04-10")  # +500 (特定)
    _add_buy(tmp_db,  ticker="B", qty=5,  price=200, date="2026-02-10", account="NISA成長投資枠")
    _add_sell(tmp_db, ticker="B", qty=5,  price=180, date="2026-05-10", account="NISA成長投資枠")  # -100 (NISA)

    r = tl.realized_pnl_in_year_v2(2026, db_path=tmp_db)
    assert r["schema_version"] == 2
    assert r["realized_jpy"] == pytest.approx(400)
    assert r["realized_jpy_semantics"] == "legacy_mixed_accounts_deprecated"
    assert r["economic_realized_pnl_jpy"] == pytest.approx(400)
    assert r["taxable_realized_pnl_jpy"] == pytest.approx(500)
    assert r["nisa_realized_pnl_jpy"] == pytest.approx(-100)
    # economic = taxable + nisa (恒等式)
    assert r["taxable_realized_pnl_jpy"] + r["nisa_realized_pnl_jpy"] == pytest.approx(
        r["economic_realized_pnl_jpy"]
    )


def test_realized_pnl_year_v2_all_taxable_has_zero_nisa(tmp_db):
    _add_buy(tmp_db,  ticker="A", qty=10, price=100, date="2026-01-10")
    _add_sell(tmp_db, ticker="A", qty=10, price=150, date="2026-04-10")
    r = tl.realized_pnl_in_year_v2(2026, db_path=tmp_db)
    assert r["nisa_realized_pnl_jpy"] == 0.0
    assert r["taxable_realized_pnl_jpy"] == pytest.approx(500)


def test_realized_pnl_year_v2_does_not_change_legacy_function(tmp_db):
    """既存 consumer (api/routes/performance.py) への破壊的変更を避けるため、
    realized_pnl_in_year() 自体の戻り値 shape は変えていないこと。"""
    _add_buy(tmp_db,  ticker="A", qty=10, price=100, date="2026-01-10")
    _add_sell(tmp_db, ticker="A", qty=10, price=150, date="2026-04-10")
    legacy = tl.realized_pnl_in_year(2026, db_path=tmp_db)
    assert "schema_version" not in legacy
    assert set(legacy.keys()) == {"year", "realized_jpy", "by_account", "by_ticker", "trade_count"}


# ────────────────────────────────────────────────────────
# portfolio_lot_snapshot
# ────────────────────────────────────────────────────────

def test_portfolio_lot_snapshot(tmp_db):
    _add_buy(tmp_db, ticker="A", qty=10, price=100, date="2026-01-10")
    _add_buy(tmp_db, ticker="B", qty=5,  price=200, date="2026-02-10")
    _add_sell(tmp_db, ticker="A", qty=3, price=120, date="2026-03-10")
    snap = tl.portfolio_lot_snapshot(db_path=tmp_db)
    assert "A" in snap["lots"]
    assert "B" in snap["lots"]
    # A は SELL 後 7 株残
    a_lots = snap["lots"]["A"]
    assert len(a_lots) == 1
    assert a_lots[0]["remaining_qty"] == pytest.approx(7)
    # B は無 SELL なので 5 株残
    b_lots = snap["lots"]["B"]
    assert b_lots[0]["remaining_qty"] == pytest.approx(5)


# ────────────────────────────────────────────────────────
# Codex P1 #1 — 口座をまたいだ lot 消費の禁止 + 不足時 fail-loud
# ────────────────────────────────────────────────────────

def test_sell_does_not_cross_accounts_and_raises_on_shortfall(tmp_db):
    """特定の SELL は NISA lot を消費せず、同一口座で不足なら整合性エラー。"""
    _add_buy(tmp_db,  ticker="X", qty=5, price=100, date="2026-01-10", account="特定")
    _add_buy(tmp_db,  ticker="X", qty=5, price=100, date="2026-02-10", account="NISA成長投資枠")
    _add_sell(tmp_db, ticker="X", qty=8, price=150, date="2026-03-10", account="特定")
    with pytest.raises(ValueError, match="賄えません"):
        tl.build_lots("X", db_path=tmp_db)


def test_sell_consumes_only_same_account(tmp_db):
    """同一口座内で足りる SELL は他口座 lot を一切触らない。"""
    _add_buy(tmp_db,  ticker="X", qty=5, price=100, date="2026-01-10", account="特定")
    _add_buy(tmp_db,  ticker="X", qty=5, price=100, date="2026-02-10", account="NISA成長投資枠")
    _add_sell(tmp_db, ticker="X", qty=3, price=150, date="2026-03-10", account="特定")
    st = tl.build_lots("X", db_path=tmp_db)
    by_acct = {l.account: l for l in st.open_lots}
    assert by_acct["特定"].remaining_qty == pytest.approx(2)            # 5-3
    assert by_acct["NISA成長投資枠"].remaining_qty == pytest.approx(5)  # 無傷
    assert all(rt.account == "特定" for rt in st.realized_trades)


# ────────────────────────────────────────────────────────
# 株式分割 / 併合 (Codex P1 #1 follow-up: split/merge cost-basis)
# ────────────────────────────────────────────────────────

def _add_split(db, *, ticker, ratio, date, account="特定", event_id=None):
    import event_ledger as el
    el.append_event(
        event_type="split" if ratio >= 1 else "merge",
        occurred_at=f"{date}T08:00:00",
        ticker=ticker,
        raw_payload={"split_ratio": ratio},
        account=account,
        event_id=event_id,
        db_path=db,
    )


def test_forward_split_adjusts_lot_keeping_cost_basis(tmp_db):
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=150, date="2026-01-10")
    _add_split(tmp_db, ticker="AAPL", ratio=2.0, date="2026-02-01")  # 2:1 forward
    st = tl.build_lots("AAPL", db_path=tmp_db)
    lot = st.open_lots[0]
    assert lot.remaining_qty == pytest.approx(20)
    assert lot.cost_per_share == pytest.approx(75)
    assert lot.cost_per_share_jpy == pytest.approx(75)
    assert lot.remaining_qty * lot.cost_per_share == pytest.approx(1500)  # 取得総額不変


def test_split_then_sell_realized_correct(tmp_db):
    _add_buy(tmp_db,  ticker="AAPL", qty=10, price=150, date="2026-01-10")
    _add_split(tmp_db, ticker="AAPL", ratio=2.0, date="2026-02-01")
    _add_sell(tmp_db, ticker="AAPL", qty=20, price=80, date="2026-03-01")
    st = tl.build_lots("AAPL", db_path=tmp_db)
    rt = st.realized_trades[0]
    # cost basis = 20*75 = 1500 / proceeds = 20*80 = 1600 / realized = +100
    assert rt.cost_basis_jpy == pytest.approx(1500)
    assert rt.proceeds_jpy == pytest.approx(1600)
    assert rt.realized_jpy == pytest.approx(100)


def test_reverse_split_merge_adjusts_lot(tmp_db):
    _add_buy(tmp_db, ticker="X", qty=10, price=100, date="2026-01-10")
    _add_split(tmp_db, ticker="X", ratio=0.5, date="2026-02-01")  # 1:2 reverse (併合)
    st = tl.build_lots("X", db_path=tmp_db)
    lot = st.open_lots[0]
    assert lot.remaining_qty == pytest.approx(5)
    assert lot.cost_per_share == pytest.approx(200)


def test_split_missing_ratio_raises(tmp_db):
    import event_ledger as el
    _add_buy(tmp_db, ticker="X", qty=10, price=100, date="2026-01-10")
    el.append_event(event_type="split", occurred_at="2026-02-01T08:00:00",
                    ticker="X", account="特定", event_id="bad_split", db_path=tmp_db)
    with pytest.raises(ValueError, match="split_ratio"):
        tl.build_lots("X", db_path=tmp_db)


def test_split_ratio_accepts_ratio_key(tmp_db):
    """canonical key 'ratio' でも split を解釈する (別実装の規約も受理)。"""
    import event_ledger as el
    _add_buy(tmp_db, ticker="Z", qty=10, price=150, date="2026-01-10")
    el.append_event(event_type="split", occurred_at="2026-02-01T08:00:00",
                    ticker="Z", raw_payload={"ratio": 2.0}, account="特定",
                    event_id="r1", db_path=tmp_db)
    st = tl.build_lots("Z", db_path=tmp_db)
    lot = st.open_lots[0]
    assert lot.remaining_qty == pytest.approx(20)
    assert lot.cost_per_share == pytest.approx(75)


def test_split_with_only_quantity_raises(tmp_db):
    """Codex P1: quantity を比率に流用しない。ratio 未指定の split は raise (lot を壊さない)。"""
    import event_ledger as el
    _add_buy(tmp_db, ticker="X", qty=10, price=100, date="2026-01-10")
    el.append_event(event_type="split", occurred_at="2026-02-01T08:00:00",
                    ticker="X", quantity=100, account="特定",
                    event_id="qonly", db_path=tmp_db)
    with pytest.raises(ValueError, match="split_ratio"):
        tl.build_lots("X", db_path=tmp_db)


# ────────────────────────────────────────────────────────
# Stage 5A: CostBasisEstimate / 総平均法に準ずる方法 dual-run
# ────────────────────────────────────────────────────────


def test_total_average_single_buy_equals_buy_price(tmp_db):
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=150, date="2026-01-10")
    result = tl.compute_total_average_cost_basis("AAPL", db_path=tmp_db)
    est = result["特定"]
    assert est.amount_jpy == pytest.approx(1500)  # 10 * 150
    assert est.method == "total_average_like"
    assert est.source == "event_ledger"
    assert est.reconciled is False


def test_total_average_two_buys_are_weighted(tmp_db):
    """(10*100 + 10*200) / 20 = 150"""
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=100, date="2026-01-10")
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=200, date="2026-02-10")
    result = tl.compute_total_average_cost_basis("AAPL", db_path=tmp_db)
    est = result["特定"]
    assert est.amount_jpy == pytest.approx(20 * 150)  # 3000


def test_total_average_sell_does_not_change_average(tmp_db):
    """総平均法の定義そのもの: 売却は平均単価を動かさない。10株@100 →
    4株売却 → 残6株の平均は依然100 (FIFOのように「残ったlotの単価」が
    変わるわけではない)。"""
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=100, date="2026-01-10")
    _add_sell(tmp_db, ticker="AAPL", qty=4, price=999, date="2026-02-01")
    result = tl.compute_total_average_cost_basis("AAPL", db_path=tmp_db)
    est = result["特定"]
    assert est.amount_jpy == pytest.approx(6 * 100)  # 600, 単価100のまま


def test_total_average_recomputes_only_on_next_buy(tmp_db):
    """買い→売り→買いの順で、平均は2回目の買い時点でのみ更新される。
    10株@100 → 6株売却(残4株@100) → 6株@200購入 →
    新平均 = (4*100 + 6*200)/10 = 160"""
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=100, date="2026-01-10")
    _add_sell(tmp_db, ticker="AAPL", qty=6, price=999, date="2026-02-01")
    _add_buy(tmp_db, ticker="AAPL", qty=6, price=200, date="2026-03-01")
    result = tl.compute_total_average_cost_basis("AAPL", db_path=tmp_db)
    est = result["特定"]
    assert est.amount_jpy == pytest.approx(10 * 160)  # 1600


def test_total_average_excludes_account_with_insufficient_balance(tmp_db):
    """total_average 計算内でのみ賄えない SELL (5株買って10株売却) は
    例外を投げず、その account を結果から除外し issue を記録する
    (1銘柄1口座のデータ不備で他の集計まで壊さない)。"""
    _add_buy(tmp_db, ticker="AAPL", qty=5, price=100, date="2026-01-10")
    _add_sell(tmp_db, ticker="AAPL", qty=10, price=999, date="2026-02-01")
    result = tl.compute_total_average_cost_basis("AAPL", db_path=tmp_db)
    assert "特定" not in result


def test_total_average_applies_split_ratio(tmp_db):
    """2:1 split で数量2倍・平均単価半分 (取得総額は不変)。"""
    import event_ledger as el
    _add_buy(tmp_db, ticker="Z", qty=10, price=150, date="2026-01-10")
    el.append_event(event_type="split", occurred_at="2026-02-01T08:00:00",
                    ticker="Z", raw_payload={"ratio": 2.0}, account="特定",
                    event_id="r1", db_path=tmp_db)
    result = tl.compute_total_average_cost_basis("Z", db_path=tmp_db)
    est = result["特定"]
    assert est.amount_jpy == pytest.approx(10 * 150)  # 総額不変 (1500)


def test_total_average_separates_accounts(tmp_db):
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=100, date="2026-01-10", account="特定")
    _add_buy(tmp_db, ticker="AAPL", qty=5, price=300, date="2026-01-15", account="持株会")
    result = tl.compute_total_average_cost_basis("AAPL", db_path=tmp_db)
    assert result["特定"].amount_jpy == pytest.approx(1000)
    assert result["持株会"].amount_jpy == pytest.approx(1500)


def test_total_average_actionable_scope_requires_owner_and_broker(tmp_db):
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=100, date="2026-01-10")
    assert tl.compute_total_average_cost_basis(
        "AAPL", db_path=tmp_db, require_position_identity=True,
    ) == {}


def test_total_average_actionable_scope_accepts_execution_identity(tmp_db):
    el.append_event(
        event_type="trade", direction="buy", ticker="AAPL",
        quantity=10, price=100, currency="JPY", account="一般",
        occurred_at="2026-01-10T10:00:00", event_id="identity-buy",
        raw_payload={
            "execution_owner": "husband",
            "execution_broker": "rakuten",
        },
        db_path=tmp_db,
    )
    result = tl.compute_total_average_cost_basis(
        "AAPL", db_path=tmp_db, require_position_identity=True,
    )
    assert result["一般"].amount_jpy == pytest.approx(1_000)


def test_total_average_rounds_unit_cost_up_and_includes_trade_fees(tmp_db):
    el.append_event(
        event_type="trade", direction="buy", ticker="FEE",
        quantity=3, price=100.1, currency="JPY", account="特定",
        occurred_at="2026-01-10T10:00:00", event_id="fee-buy",
        raw_payload={
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "fee_jpy": 2,
        },
        db_path=tmp_db,
    )
    el.append_event(
        event_type="trade", direction="sell", ticker="FEE",
        quantity=1, price=120, currency="JPY", account="特定",
        occurred_at="2026-02-10T10:00:00", event_id="fee-sell",
        raw_payload={
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "fee_jpy": 1,
        },
        db_path=tmp_db,
    )
    report = tl.realized_pnl_in_year_total_average(
        2026, tickers=["FEE"], db_path=tmp_db,
    )
    # (3*100.1 + 2) / 3 = 100.766.. -> 1 unit is rounded up to 101.
    # Sale proceeds 120 - 1 fee, acquisition cost 101 => gain 18.
    assert report["complete"] is True
    assert report["taxable_realized_pnl_jpy"] == pytest.approx(18)
    assert report["by_account"]["husband|rakuten|特定"] == pytest.approx(18)


def test_total_average_v2_can_be_authoritative_with_raw_payload_identity(tmp_db):
    for event_id, direction, price in (
        ("nisa-buy", "buy", 100),
        ("nisa-sell", "sell", 90),
    ):
        el.append_event(
            event_type="trade", direction=direction, ticker="NISAID",
            quantity=1, price=price, currency="JPY", account="NISA成長投資枠",
            occurred_at=(
                "2026-01-10T10:00:00"
                if direction == "buy" else "2026-02-10T10:00:00"
            ),
            event_id=event_id,
            raw_payload={
                "execution_owner": "wife",
                "execution_broker": "sbi",
            },
            db_path=tmp_db,
        )
    report = tl.realized_pnl_in_year_v2(
        2026, tickers=["NISAID"], db_path=tmp_db, mode="total_average",
    )
    assert report["authoritative_basis"] == "total_average_like"
    assert report["economic_realized_pnl_jpy"] == pytest.approx(-10)
    assert report["taxable_realized_pnl_jpy"] == 0
    assert report["nisa_realized_pnl_jpy"] == pytest.approx(-10)


def test_fifo_audit_never_consumes_same_account_lot_from_other_broker(tmp_db):
    for event_id, owner, broker in (
        ("buy-rakuten", "husband", "rakuten"),
        ("buy-sbi", "husband", "sbi"),
    ):
        el.append_event(
            event_type="trade", direction="buy", ticker="AAPL",
            quantity=10, price=100, currency="JPY", account="一般",
            occurred_at="2026-01-10T10:00:00", event_id=event_id,
            raw_payload={"execution_owner": owner, "execution_broker": broker},
            db_path=tmp_db,
        )
    el.append_event(
        event_type="trade", direction="sell", ticker="AAPL",
        quantity=10, price=120, currency="JPY", account="一般",
        occurred_at="2026-02-10T10:00:00", event_id="sell-sbi",
        raw_payload={"execution_owner": "husband", "execution_broker": "sbi"},
        db_path=tmp_db,
    )
    state = tl.build_lots("AAPL", db_path=tmp_db)
    remaining = {(lot.broker, lot.remaining_qty) for lot in state.open_lots}
    assert ("rakuten", 10) in remaining
    assert ("sbi", 0) in remaining


def test_compare_old_new_matches_when_no_sells(tmp_db):
    """売却が無ければ FIFO と総平均法は (単一lotなので) 一致する。"""
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=150, date="2026-01-10")
    cmp_result = tl.compare_old_new_cost_basis("AAPL", db_path=tmp_db)
    row = cmp_result["rows"][0]
    assert row["fifo_cost_basis_jpy"] == pytest.approx(1500)
    assert row["total_average_cost_basis_jpy"] == pytest.approx(1500)
    assert row["diff_jpy"] == pytest.approx(0)


def test_compare_old_new_diverges_with_mixed_lots_and_partial_sell(tmp_db):
    """本題: FIFO と総平均法が実際に異なる数値を出すケース。
    10株@100 + 10株@200 → 5株売却。
    FIFO: 古いlot(100円)から5株消費 → 残り 5株@100 + 10株@200 = 500+2000=2500
    総平均法: 平均=(10*100+10*200)/20=150 → 売却後も平均不変、残15株 → 15*150=2250
    差分 = 2250 - 2500 = -250"""
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=100, date="2026-01-01")
    _add_buy(tmp_db, ticker="AAPL", qty=10, price=200, date="2026-02-01")
    _add_sell(tmp_db, ticker="AAPL", qty=5, price=999, date="2026-03-01")

    cmp_result = tl.compare_old_new_cost_basis("AAPL", db_path=tmp_db)
    row = cmp_result["rows"][0]
    assert row["fifo_cost_basis_jpy"] == pytest.approx(2500)
    assert row["total_average_cost_basis_jpy"] == pytest.approx(2250)
    assert row["diff_jpy"] == pytest.approx(-250)
    assert "証券会社の年間取引報告書" in cmp_result["note"]


def test_compare_old_new_is_side_effect_free(tmp_db, tmp_path, monkeypatch):
    """本題 (Stage 5A の合格条件): 比較関数は action_state.json も
    通知も一切触らない — 純粋関数であること。"""
    import action_state_tracker as ast

    state_path = tmp_path / "action_state.json"
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    _add_buy(tmp_db, ticker="AAPL", qty=10, price=100, date="2026-01-10")
    tl.compare_old_new_cost_basis("AAPL", db_path=tmp_db)
    tl.compute_total_average_cost_basis("AAPL", db_path=tmp_db)

    assert not state_path.exists()  # 一切書き込まれていない

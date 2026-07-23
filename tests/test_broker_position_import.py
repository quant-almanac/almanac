"""
tests/test_broker_position_import.py — 楽天 assetbalance 保有証券同期
"""
import json

import broker_position_import as bpi


def _sample_csv(tmp_path):
    p = tmp_path / "assetbalance(all)_20260517_131924.csv"
    p.write_text(
        '"種別","銘柄コード・ティッカー","銘柄","口座","保有数量","［単位］","平均取得価額","［単位］","現在値","［単位］","現在値(更新日)","(参考為替)","前日比","［単位］","時価評価額[円]","時価評価額[外貨]","評価損益[円]","評価損益[％]"\n'
        '"国内株式","6762","ＴＤＫ","特定","100","株","2,449.00","円","2,993.0","円","","","-79.0","円","299,300","-","+54,400","+22.21"\n'
        '"米国株式","GLD","SPDR ゴールド・シェア","特定","26","株","309.5461","USD","417.2900","USD","","","-9.9200","USD","1,722,581","10,849.54 USD","+530,143","+44.45"\n'
        '"米国株式","GLD","SPDR ゴールド・シェア","NISA成長投資枠","5","株","430.6200","USD","417.2900","USD","","","-9.9200","USD","331,265","2,086.45 USD","-9,312","-2.73"\n'
        '"米国株式","ABNB","エアビーアンドビー","特定","2","株","135.3550","USD","132.8500","USD","","","-0.8200","USD","42,185","265.70 USD","-635","-1.48"\n'
        '"投資信託","","eMAXIS Slim 全世界株式(オール・カントリー)(オルカン)","NISAつみたて投資枠","479,679","口","29,029.83","円","37,136","円","","","+362","円","1,781,336","-","+388,836","+27.92"\n'
        '"外貨建MMF","","GS米ドルファンド","特定","401,252","口","16,026.08","円","158.77","円/USD","","","-","-","638,060","4,018.77 USD","-4,990","-0.77"\n',
        encoding="cp932",
    )
    return p


def test_parse_rakuten_positions(tmp_path):
    positions = bpi.parse_rakuten_positions(_sample_csv(tmp_path))
    assert len(positions) == 6
    tdk = positions[0]
    assert tdk.ticker == "6762.T"
    assert tdk.quantity == 100
    assert tdk.entry_price == 2449.0
    assert tdk.currency == "JPY"
    orcan = [p for p in positions if p.ticker == "SLIM_ORCAN"][0]
    assert orcan.quantity == 479679
    assert orcan.current_price == 37136
    mmf = [p for p in positions if p.ticker == "GS_MMF_USD"][0]
    assert mmf.quantity == 4018.77
    assert mmf.entry_price == 1.0


def test_build_reconciled_holdings_updates_and_adds(tmp_path):
    holdings = tmp_path / "holdings.json"
    holdings.write_text(json.dumps({
        "6762.T": {"ticker": "6762.T", "account": "特定", "shares": 200, "entry_price": 2448.55, "currency": "JPY"},
        "GLD": {"ticker": "GLD", "account": "特定", "shares": 26, "entry_price": 309.5461, "currency": "USD"},
        "SLIM_ORCAN": {"ticker": "SLIM_ORCAN", "account": "NISAつみたて投資枠", "shares": 474785, "entry_price": 28949.95, "currency": "JPY", "unit": "口"},
        "GS_MMF_USD": {"ticker": "GS_MMF_USD", "account": "特定", "shares": 4016.7, "entry_price": 1.0, "currency": "USD", "unit": "口"},
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 1000},
        "1489_WIFE": {"ticker": "1489.T", "account": "NISA成長投資枠", "broker": "SBI証券（妻）", "shares": 150},
    }), encoding="utf-8")

    positions = bpi.parse_rakuten_positions(_sample_csv(tmp_path))
    next_holdings, diff = bpi.build_reconciled_holdings(
        positions=positions,
        holdings_path=holdings,
        as_of="2026-05-17",
    )

    assert next_holdings["6762.T"]["shares"] == 100
    assert next_holdings["SLIM_ORCAN"]["shares"] == 479679
    assert next_holdings["GS_MMF_USD"]["shares"] == 4018.77
    assert "GLD_NISA" in next_holdings
    assert next_holdings["GLD_NISA"]["account"] == "NISA成長投資枠"
    assert "ABNB" in next_holdings
    assert next_holdings["CASH_JPY"]["shares"] == 1000
    assert next_holdings["1489_WIFE"]["shares"] == 150
    assert len(diff["adds"]) == 2
    assert any(u["key"] == "6762.T" for u in diff["updates"])


# Codex P1 #8 — 完全スナップショットなら CSV に無い楽天保有 (売却済み) を 0 化する

def test_full_snapshot_zeroes_stale_rakuten_holding(tmp_path):
    holdings = tmp_path / "holdings.json"
    holdings.write_text(json.dumps({
        "6762.T": {"ticker": "6762.T", "account": "特定", "shares": 200, "entry_price": 2448.55, "currency": "JPY"},
        "SOLD.T": {"ticker": "9999.T", "account": "特定", "shares": 50, "entry_price": 1000, "currency": "JPY"},
    }), encoding="utf-8")
    positions = bpi.parse_rakuten_positions(_sample_csv(tmp_path))
    next_holdings, diff = bpi.build_reconciled_holdings(
        positions=positions, holdings_path=holdings, as_of="2026-05-17", full_snapshot=True)
    assert next_holdings["SOLD.T"]["shares"] == 0
    assert next_holdings["SOLD.T"]["reconcile_zeroed"] is True
    assert any(z["key"] == "SOLD.T" for z in diff["zeroed"])


def test_partial_snapshot_keeps_stale(tmp_path):
    holdings = tmp_path / "holdings.json"
    holdings.write_text(json.dumps({
        "SOLD.T": {"ticker": "9999.T", "account": "特定", "shares": 50, "entry_price": 1000, "currency": "JPY"},
    }), encoding="utf-8")
    positions = bpi.parse_rakuten_positions(_sample_csv(tmp_path))
    next_holdings, diff = bpi.build_reconciled_holdings(
        positions=positions, holdings_path=holdings, as_of="2026-05-17", full_snapshot=False)
    assert next_holdings["SOLD.T"]["shares"] == 50
    assert diff["zeroed"] == []
    assert any(s["key"] == "SOLD.T" for s in diff["stale"])


def test_full_snapshot_empty_positions_raises(tmp_path):
    import pytest
    holdings = tmp_path / "holdings.json"
    holdings.write_text(json.dumps({
        "SOLD.T": {"ticker": "9999.T", "account": "特定", "shares": 50, "entry_price": 1000, "currency": "JPY"},
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        bpi.build_reconciled_holdings(
            positions=[], holdings_path=holdings, as_of="2026-05-17", full_snapshot=True)

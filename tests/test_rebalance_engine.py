"""
H1契約テスト: rebalanceの通貨/セクター判定はLong tier限定であるべき。

背景:
  rebalance_engine.calculate_rebalance_actions() は受け取った snapshot をそのまま
  analyze_currency_balance/analyze_sector_balance に渡していたため、呼び出し元が
  long以外のtier(medium/swing)を含む全保有のsnapshotを渡すと、通貨/セクター判定が
  全tier合算で行われてしまっていた。一方 api/routes/rebalance.py は long限定で
  再集計したsnapshotを使っており、2経路の挙動が食い違っていた。

  本テストは「medium/swingを増減してもcurrency_result/sector_result/buy_candidatesの
  gapが変化しないこと」「geographic_result/NISA保護は全体スコープを維持すること」
  「api/routes/rebalance.py経路とcalculate_rebalance_actions経路が同じlong限定ロジック
  になること」を固定する。
"""
import pytest

import rebalance_engine as re


def _position(ticker, currency, sector, investment_type, value_jpy, account="特定"):
    return {
        "ticker": ticker,
        "key": ticker,
        "name": ticker,
        "currency": currency,
        "sector": sector,
        "investment_type": investment_type,
        "value_jpy": value_jpy,
        "account": account,
    }


def _snapshot(positions):
    total = sum(p["value_jpy"] for p in positions)
    cur_vals: dict = {}
    sec_vals: dict = {}
    for p in positions:
        cur_vals[p["currency"]] = cur_vals.get(p["currency"], 0) + p["value_jpy"]
        sec_vals[p["sector"]] = sec_vals.get(p["sector"], 0) + p["value_jpy"]
    currency_breakdown = {
        c: {"value_jpy": v, "ratio": round(v / total, 4) if total else 0}
        for c, v in cur_vals.items()
    }
    sector_breakdown = {
        s: {"value_jpy": v, "ratio": round(v / total, 4) if total else 0}
        for s, v in sec_vals.items()
    }
    return {
        "positions": positions,
        "total_jpy": total,
        "currency_breakdown": currency_breakdown,
        "sector_breakdown": sector_breakdown,
    }


# Long tier: USD偏重 + Technology偏重 + Energy underweight を意図的に作る
_L_TECH   = _position("TECH1", "USD", "Technology", "long", 6_000_000)
_L_ENERGY = _position("NRG1",  "JPY", "Energy",      "long",   200_000)
_L_HEALTH = _position("HLT1",  "JPY", "Healthcare",  "long", 1_800_000)
_LONG_POSITIONS = [_L_TECH, _L_ENERGY, _L_HEALTH]
_LONG_TOTAL = 8_000_000

# medium/swing: long側より大きい金額をぶつけて、誤って合算されたら必ず数値が動くようにする
_M_BIG   = _position("AAPL_M", "USD", "Technology",  "medium", 20_000_000)
_S_SMALL = _position("TXN_S",  "JPY", "Industrials", "swing",   3_000_000)


def test_currency_and_sector_result_unaffected_by_medium_swing():
    snap_long_only = _snapshot(_LONG_POSITIONS)
    snap_with_medium_swing = _snapshot(_LONG_POSITIONS + [_M_BIG, _S_SMALL])

    result_a = re.calculate_rebalance_actions(snap_long_only)
    result_b = re.calculate_rebalance_actions(snap_with_medium_swing)

    assert result_a["currency_result"] == result_b["currency_result"]
    assert result_a["sector_result"] == result_b["sector_result"]
    assert result_a["buy_candidates"] == result_b["buy_candidates"]


def test_core_total_is_long_only_while_total_jpy_stays_portfolio_wide():
    snap_long_only = _snapshot(_LONG_POSITIONS)
    snap_with_medium_swing = _snapshot(_LONG_POSITIONS + [_M_BIG, _S_SMALL])

    result_a = re.calculate_rebalance_actions(snap_long_only)
    result_b = re.calculate_rebalance_actions(snap_with_medium_swing)

    assert result_a["summary"]["core_total_jpy"] == _LONG_TOTAL
    assert result_b["summary"]["core_total_jpy"] == _LONG_TOTAL
    assert result_a["summary"]["core_position_count"] == 3
    assert result_b["summary"]["core_position_count"] == 3

    # total_jpy(全体総額)はtier構成が違えば legitimately 異なる
    assert result_a["summary"]["total_jpy"] == _LONG_TOTAL
    assert result_b["summary"]["total_jpy"] == _LONG_TOTAL + 20_000_000 + 3_000_000


def test_currency_actions_reflect_long_only_imbalance():
    snap_with_medium_swing = _snapshot(_LONG_POSITIONS + [_M_BIG, _S_SMALL])
    result = re.calculate_rebalance_actions(snap_with_medium_swing)

    currencies = result["currency_result"]["currencies"]
    # USD 75% (long限定) は max 70% を超過、JPY 25% は min 30% を下回る
    assert currencies["USD"]["ratio"] == pytest.approx(0.75)
    assert currencies["JPY"]["ratio"] == pytest.approx(0.25)

    buy_currencies = {c["currency"]: c for c in result["buy_candidates"]["currencies"]}
    assert "JPY" in buy_currencies
    # gap_jpy は long総額(8,000,000)基準: (0.35-0.25)*8,000,000
    assert buy_currencies["JPY"]["gap_jpy"] == pytest.approx(800_000)


def test_buy_sectors_gap_uses_long_total_not_portfolio_total():
    snap_with_medium_swing = _snapshot(_LONG_POSITIONS + [_M_BIG, _S_SMALL])
    result = re.calculate_rebalance_actions(snap_with_medium_swing)

    buy_sectors = {s["sector"]: s for s in result["buy_candidates"]["sectors"]}
    assert "Energy" in buy_sectors
    # gap_jpy は long総額(8,000,000)基準: (0.05-0.025)*8,000,000
    assert buy_sectors["Energy"]["gap_jpy"] == pytest.approx(200_000)


# ── geo分析 / NISA保護は全体スコープを維持すべき ──────────────────────────

_M_EWG  = _position("EWG",          "USD", "Other", "medium", 5_000_000, account="特定")
_M_NISA = _position("NISA_FUND",    "JPY", "Other", "medium", 1_000_000, account="つみたてNISA")


def test_geographic_concentration_sees_medium_tier_positions():
    snap_no_eu = _snapshot(_LONG_POSITIONS)
    snap_with_eu = _snapshot(_LONG_POSITIONS + [_M_EWG, _M_NISA])

    result_no_eu = re.calculate_rebalance_actions(snap_no_eu)
    result_with_eu = re.calculate_rebalance_actions(snap_with_eu)

    # medium tier の EWG が地理的集中度に反映される = geo分析はlong限定ではなく全体スコープ
    assert result_no_eu["geographic_result"]["european_ratio"] == 0
    assert result_with_eu["geographic_result"]["european_ratio"] == pytest.approx(
        5_000_000 / 14_000_000, abs=1e-4
    )
    assert result_no_eu["geographic_result"]["status"] == "ok"
    assert result_with_eu["geographic_result"]["status"] == "warning"


def test_nisa_sell_protection_sees_medium_tier_nisa_positions():
    snap_no_nisa = _snapshot(_LONG_POSITIONS)
    snap_with_nisa = _snapshot(_LONG_POSITIONS + [_M_EWG, _M_NISA])

    result_no_nisa = re.calculate_rebalance_actions(snap_no_nisa)
    result_with_nisa = re.calculate_rebalance_actions(snap_with_nisa)

    def _technology_reduce(actions):
        for a in actions:
            if a.get("type") == "reduce" and a.get("sector") == "Technology":
                return a
        return None

    reduce_no_nisa = _technology_reduce(result_no_nisa["action_plan"])
    reduce_with_nisa = _technology_reduce(result_with_nisa["action_plan"])

    assert reduce_no_nisa is not None and reduce_with_nisa is not None
    # long側にNISAは無いが、medium側にNISA口座ポジションがあれば警告が付くべき
    # = NISA保護はlong限定ではなく全体スコープ
    assert "nisa_warning" not in reduce_no_nisa
    assert "nisa_warning" in reduce_with_nisa


# ── api/routes/rebalance.py 経路との一致 ──────────────────────────────────

def test_api_rebalance_route_matches_engine_core_filter(monkeypatch):
    snapshot = _snapshot(_LONG_POSITIONS + [_M_BIG, _S_SMALL])

    import api.routes.portfolio as portfolio_mod
    import currency_policy
    monkeypatch.setattr(portfolio_mod, "get_cached_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        currency_policy,
        "resolve_effective_targets",
        lambda *, static: (static, {"source": "test_static"}),
    )

    from api.routes.rebalance import _calc_rebalance

    api_result = _calc_rebalance()
    engine_result = re.calculate_rebalance_actions(snapshot)

    assert api_result["currency"]["data"] == engine_result["currency_result"]["currencies"]
    assert api_result["sector"]["data"] == engine_result["sector_result"]["sectors"]
    assert api_result["core_total_jpy"] == engine_result["summary"]["core_total_jpy"]
    assert api_result["core_position_count"] == engine_result["summary"]["core_position_count"]

"""Short universe 基盤の受入 E2E(test-first)。

実装前に「安全に short候補が surface するまで」の契約を固定する。未実装の挙動は
strict xfail で固定し、builder(step B)/screener切替(C)/3レーン分離(D)が
入ったら XPASS で顕在化させる(assertion を弱めて green にしない)。

確定スキーマ short_universe.json:
{
  "as_of": "<iso>",
  "tickers": {
    "<TICKER>": {
      "ticker","market"(JP|US),"name",
      # tradeability(借りて売れるか。どれか unknown/NG → shortable=false=fail-closed)
      "shortable": bool,                         # evaluate_shortability の導出結果
      "jpx_loanable": true|false|None,           # JP 貸借銘柄
      "rakuten_general_short","sbi_general_short": true|false|None,
      "broker": ["rakuten","sbi"],
      # cost
      "borrow_cost_annual_pct": float|None, "reverse_daily_fee": bool|None,
      "reverse_daily_fee_bps": float|None,
      # squeeze / 規制
      "short_ratio","days_to_cover": float, "jsf_loan_balance_ratio": float|None,
      "squeeze_guard_status": "ok|warn|blocked",  # unknown/stale → blocked
      "regulation": "none|caution|margin_up|short_ban",
      # liquidity / compliance / 実行
      "adv_jpy": float, "liquidity_ok": bool,
      "insider_restricted": bool, "earnings_blackout": bool,
      "human_execution_only": true,              # 常に true
      "lane_eligibility": ["overheat","event","bear"],
      # 出所/鮮度(fail-closed の根拠)
      "sources": {"jpx_loanable":{"as_of","status"},"jsf":{"as_of","status"},"broker":{"as_of","status"}},
      "last_human_verified": "<date|null>"
    }
  }
}

想定 interface(step B 実装):
  short_universe.evaluate_shortability(row: dict, *, now: datetime|None=None,
                                       jsf_stale_days: int=2) -> dict
      → {"shortable": bool, "squeeze_guard_status": str, "reasons": [str], "lane_eligibility": [str],
         "borrow_cost_annual_pct": float|None, "human_execution_only": True}
  short_universe.classify_short_lane(signal: dict) -> "overheat"|"event"|"bear"|None
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NOW = datetime(2026, 6, 24, 9, 0, 0)


def _fresh(status="ok", age_days=0):
    return {"as_of": (NOW - timedelta(days=age_days)).isoformat(), "status": status}


def _jp_row(**over):
    row = {
        "ticker": "7203.T", "market": "JP", "name": "Toyota",
        "jpx_loanable": True, "rakuten_general_short": True, "sbi_general_short": True,
        "broker": ["rakuten", "sbi"],
        "borrow_cost_annual_pct": 0.011, "reverse_daily_fee": False,
        "short_ratio": 2.0, "days_to_cover": 1.5, "jsf_loan_balance_ratio": 1.6,
        "regulation": "none", "adv_jpy": 5e10, "liquidity_ok": True,
        "insider_restricted": False, "earnings_blackout": False,
        "human_execution_only": True,
        "sources": {"jpx_loanable": _fresh(), "jsf": _fresh(), "broker": _fresh(status="manual")},
    }
    row.update(over)
    return row


def _us_row(**over):
    row = {
        "ticker": "TSLA", "market": "US", "name": "Tesla",
        "rakuten_general_short": None, "sbi_general_short": None, "broker": [],
        "borrow_cost_annual_pct": None, "short_ratio": 3.0, "days_to_cover": 2.0,
        "regulation": "none", "adv_jpy": 1e11, "liquidity_ok": True,
        "insider_restricted": False, "earnings_blackout": False,
        "human_execution_only": True,
        "sources": {"broker": _fresh(status="manual")},
    }
    row.update(over)
    return row


def _evaluate(row):
    import short_universe
    return short_universe.evaluate_shortability(row, now=NOW)


# ── JP shortability / fail-closed ────────────────────────

def test_jp_loanable_jsf_ok_liquidity_insider_false_is_shortable():
    v = _evaluate(_jp_row())
    assert v["shortable"] is True
    assert v["squeeze_guard_status"] == "ok"
    assert v["human_execution_only"] is True


def test_jp_loanable_unknown_fails_closed():
    v = _evaluate(_jp_row(jpx_loanable=None,
                          sources={"jpx_loanable": _fresh(status="missing"),
                                   "jsf": _fresh(), "broker": _fresh()}))
    assert v["shortable"] is False
    assert any("loanab" in r.lower() for r in v["reasons"])


def test_jp_jsf_stale_blocks_squeeze_guard():
    v = _evaluate(_jp_row(sources={"jpx_loanable": _fresh(), "broker": _fresh(),
                                   "jsf": _fresh(status="ok", age_days=5)}))  # >2d stale
    assert v["squeeze_guard_status"] == "blocked"
    assert v["shortable"] is False


def test_jp_reverse_daily_fee_blocks():
    v = _evaluate(_jp_row(reverse_daily_fee=True))
    assert v["squeeze_guard_status"] in ("warn", "blocked")
    assert v["shortable"] is False


def test_jp_short_ban_excluded():
    v = _evaluate(_jp_row(regulation="short_ban"))
    assert v["shortable"] is False
    assert any("ban" in r.lower() or "規制" in r or "short_ban" in r for r in v["reasons"])


def test_jp_low_liquidity_not_shortable():
    v = _evaluate(_jp_row(liquidity_ok=False, adv_jpy=1e6))
    assert v["shortable"] is False


# ── US shortability(broker CSV 明示のみ)─────────────────

def test_us_explicit_broker_shortable():
    v = _evaluate(_us_row(rakuten_general_short=True, broker=["rakuten"],
                          borrow_cost_annual_pct=0.05))
    assert v["shortable"] is True


def test_us_unknown_borrow_fails_closed():
    v = _evaluate(_us_row())  # broker unknown/None
    assert v["shortable"] is False
    assert any("borrow" in r.lower() or "broker" in r.lower() for r in v["reasons"])


# ── insider(short でも除外)──────────────────────────────

def test_insider_restricted_excluded_even_if_otherwise_shortable():
    # 9999.T(勤務先株)は他条件が揃っても shortable=false
    v = _evaluate(_jp_row(ticker="9999.T", name="employer", insider_restricted=True))
    assert v["shortable"] is False
    assert any("insider" in r.lower() for r in v["reasons"])


# ── cost metadata が verdict に載る ──────────────────────

def test_cost_metadata_present_in_verdict():
    v = _evaluate(_jp_row())
    assert v.get("borrow_cost_annual_pct") is not None


# ── 3レーン分離(overheat / event / bear)────────────────

def test_lane_classification_overheat_event_bear():
    import short_universe as su
    assert su.classify_short_lane({"rsi": 86, "ma50_diff_pct": 25.0}) == "overheat"
    assert su.classify_short_lane({"dilution_flag": True}) == "event"
    assert su.classify_short_lane({"regime": "BEAR", "trend": "down"}) == "bear"


# ── observe_only-first 不変 ──────────────────────────────

def test_shortable_row_is_human_execution_only_never_auto():
    v = _evaluate(_jp_row())
    # shortable=true でも自動発注フラグは持たず human_execution_only=True 固定
    assert v["human_execution_only"] is True
    assert "auto_execute" not in v and v.get("executable") is not True

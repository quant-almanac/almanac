"""Step C 受入 E2E: short_universe builder + screener tradeability gate。

outcome 契約(端から端):
- builder は data source(JPX loanable / JSF / US broker CSV)から row を組み立て、
  evaluate_shortability の verdict を ledger(short_universe.json)に書く。
- **live データ無し**(loanable=None / JSF 不在 / US broker 不在)では全 fail-closed。
  候補を黙って消さず、shortable=false + reason を ledger に残す。
- 完全データの JP は shortable=true・borrow_cost/lane 付き。
- screener gate: shortable=false の technical 候補は observe_only のまま
  executable に昇格しない(自動発注フラグを持たない)。未収載 ticker も fail-closed。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import short_universe as su

NOW = datetime(2026, 6, 24, 9, 0, 0)
PINNED = NOW.isoformat()


def _jsf(*tickers_fresh, generated=None):
    """fresh な JSF state を作る。tickers_fresh = [(ticker, loan_ratio, reverse_fee), ...]"""
    return {
        "generated_at": (generated or NOW).isoformat(),
        "tickers": {
            t: {"loan_ratio": lr, "reverse_daily_fee": rf}
            for (t, lr, rf) in tickers_fresh
        },
    }


# ── builder: live データ無し → 全 fail-closed(silently drop しない)──

def test_builder_no_live_data_is_all_fail_closed_but_listed():
    led = su.build_short_universe(
        ["7203.T", "AAPL"], now=NOW,
        sources={"loanable_map": {}, "jsf_state": {}, "broker_us_map": {},
                 "squeeze_map": {}, "pinned_at": PINNED},
    )
    assert set(led["tickers"]) == {"7203.T", "AAPL"}, "候補を黙って消さない"
    for t in ("7203.T", "AAPL"):
        assert led["tickers"][t]["shortable"] is False
        assert led["tickers"][t]["reasons"], "fail-closed の理由を残す"
    assert led["shortable_count"] == 0
    assert "as_of" in led


# ── builder: 完全データの JP は shortable ──

def test_builder_full_jp_data_is_shortable_with_cost():
    led = su.build_short_universe(
        ["7203.T"], now=NOW,
        sources={
            "loanable_map": {"7203.T": True},
            "jsf_state": _jsf(("7203.T", 1.6, False)),
            "broker_us_map": {}, "squeeze_map": {}, "pinned_at": PINNED,
        },
    )
    v = led["tickers"]["7203.T"]
    assert v["shortable"] is True
    assert v["squeeze_guard_status"] == "ok"
    assert v["borrow_cost_annual_pct"] is not None, "cost metadata が ledger に載る"
    assert v["human_execution_only"] is True


# ── builder: loanable None / JSF 欠落 / insider はそれぞれ fail-closed ──

def test_builder_loanable_none_fails_closed():
    led = su.build_short_universe(
        ["7203.T"], now=NOW,
        sources={"loanable_map": {"7203.T": None},
                 "jsf_state": _jsf(("7203.T", 1.6, False)),
                 "broker_us_map": {}, "squeeze_map": {}, "pinned_at": PINNED},
    )
    v = led["tickers"]["7203.T"]
    assert v["shortable"] is False
    assert any("loanab" in r.lower() for r in v["reasons"])


def test_builder_jsf_missing_blocks_squeeze():
    led = su.build_short_universe(
        ["7203.T"], now=NOW,
        sources={"loanable_map": {"7203.T": True}, "jsf_state": {},
                 "broker_us_map": {}, "squeeze_map": {}, "pinned_at": PINNED},
    )
    v = led["tickers"]["7203.T"]
    assert v["squeeze_guard_status"] == "blocked"
    assert v["shortable"] is False


def test_builder_insider_excluded_even_with_full_data():
    led = su.build_short_universe(
        ["9999.T"], now=NOW,
        sources={"loanable_map": {"9999.T": True},
                 "jsf_state": _jsf(("9999.T", 1.6, False)),
                 "broker_us_map": {}, "squeeze_map": {}, "pinned_at": PINNED},
    )
    v = led["tickers"]["9999.T"]
    assert v["shortable"] is False
    assert any("insider" in r.lower() for r in v["reasons"])


# ── builder: US broker CSV 明示で shortable ──

def test_builder_us_broker_csv_makes_shortable():
    led = su.build_short_universe(
        ["TSLA"], now=NOW,
        sources={"loanable_map": {}, "jsf_state": {},
                 "broker_us_map": {"TSLA": {"rakuten": True, "borrow_cost_annual_pct": 0.05}},
                 "squeeze_map": {}, "pinned_at": PINNED},
    )
    v = led["tickers"]["TSLA"]
    assert v["shortable"] is True
    assert v["borrow_cost_annual_pct"] == 0.05


def test_builder_us_unknown_broker_fails_closed():
    led = su.build_short_universe(
        ["AAPL"], now=NOW,
        sources={"loanable_map": {}, "jsf_state": {}, "broker_us_map": {},
                 "squeeze_map": {}, "pinned_at": PINNED},
    )
    assert led["tickers"]["AAPL"]["shortable"] is False


# ── builder: atomic write ──

def test_builder_writes_ledger_atomically(tmp_path):
    out = tmp_path / "short_universe.json"
    su.build_short_universe(
        ["7203.T"], now=NOW, write=True, output_path=out,
        sources={"loanable_map": {"7203.T": True},
                 "jsf_state": _jsf(("7203.T", 1.6, False)),
                 "broker_us_map": {}, "squeeze_map": {}, "pinned_at": PINNED},
    )
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "as_of" in data and "7203.T" in data["tickers"]


def test_builder_write_merges_existing_ledger_tickers(tmp_path):
    out = tmp_path / "short_universe.json"
    out.write_text(json.dumps({
        "as_of": "2026-06-23T09:00:00",
        "shortable_count": 1,
        "tickers": {
            "AAPL": {
                "ticker": "AAPL",
                "market": "US",
                "shortable": True,
                "human_execution_only": True,
            }
        },
    }), encoding="utf-8")

    led = su.build_short_universe(
        ["7203.T"], now=NOW, write=True, output_path=out,
        sources={"loanable_map": {"7203.T": True},
                 "jsf_state": _jsf(("7203.T", 1.6, False)),
                 "broker_us_map": {}, "squeeze_map": {}, "pinned_at": PINNED},
    )

    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data["tickers"]) == {"AAPL", "7203.T"}
    assert set(led["tickers"]) == {"AAPL", "7203.T"}
    assert data["shortable_count"] == 2


# ── screener gate: shortable=false は observe_only のまま、昇格しない ──

def _universe(**verdicts):
    return {"as_of": PINNED, "tickers": verdicts}


def test_gate_non_shortable_stays_observe_only_never_executable():
    uni = _universe(**{"7203.T": {"shortable": False, "squeeze_guard_status": "blocked",
                                  "reasons": ["jpx_loanable unknown"], "lane_eligibility": []}})
    out = su.apply_shortability_gate(
        {"ticker": "7203.T", "observe_only": True, "rsi": 82}, uni)
    assert out["shortable"] is False
    assert out["observe_only"] is True
    assert out["human_execution_only"] is True
    assert "auto_execute" not in out and out.get("executable") is not True
    assert out["shortability"]["reasons"], "理由を候補に残す"


def test_gate_shortable_marks_tradeable_but_human_only():
    uni = _universe(**{"TSLA": {"shortable": True, "squeeze_guard_status": "ok",
                               "reasons": [], "lane_eligibility": ["overheat"],
                               "borrow_cost_annual_pct": 0.05}})
    out = su.apply_shortability_gate({"ticker": "TSLA", "observe_only": True}, uni)
    assert out["shortable"] is True
    assert out["shortability"]["borrow_cost_annual_pct"] == 0.05
    # shortable=true でも human_execution_only 固定・自動発注しない
    assert out["human_execution_only"] is True
    assert "auto_execute" not in out and out.get("executable") is not True


def test_gate_unknown_ticker_fails_closed():
    out = su.apply_shortability_gate({"ticker": "ZZZZ"}, _universe())
    assert out["shortable"] is False
    assert out["shortability"]["reasons"], "未収載は fail-closed の理由付き"

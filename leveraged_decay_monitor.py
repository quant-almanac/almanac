"""
leveraged_decay_monitor.py (Part E-7)
======================================

3× / 2× Leveraged ETF のコンパウンディング・ディケイを監視し、
長期保有（6ヶ月超）が不利な状態を検知して、原資産 ETF への
部分乗換提案を生成する。

Leverage ETF → Underlying mapping:
  SOXL  (3×) → SMH
  TQQQ  (3×) → QQQ
  FNGU  (3×) → FNGS or QQQ
  SPXL  (3×) → SPY
  LABU  (3×) → XBI
  UPRO  (3×) → SPY
  TMF   (3×) → TLT
  QLD   (2×) → QQQ
  SSO   (2×) → SPY

判定ロジック:
  - 保有期間 >= 180 日
  - decay_proxy = (target_index_total_return × leverage) - actual_etf_return
  - decay_proxy > abs(actual_etf_return) × 0.15  → 乗換推奨

出力: leveraged_decay_signals.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT   = BASE_DIR / "leveraged_decay_signals.json"
HOLDINGS = BASE_DIR / "holdings.json"

LEV_MAP: dict[str, dict] = {
    "SOXL": {"underlying": "SMH",  "leverage": 3.0},
    "TQQQ": {"underlying": "QQQ",  "leverage": 3.0},
    "FNGU": {"underlying": "QQQ",  "leverage": 3.0},
    "SPXL": {"underlying": "SPY",  "leverage": 3.0},
    "LABU": {"underlying": "XBI",  "leverage": 3.0},
    "UPRO": {"underlying": "SPY",  "leverage": 3.0},
    "TMF":  {"underlying": "TLT",  "leverage": 3.0},
    "QLD":  {"underlying": "QQQ",  "leverage": 2.0},
    "SSO":  {"underlying": "SPY",  "leverage": 2.0},
    "URTY": {"underlying": "IWM",  "leverage": 3.0},
}

MIN_HOLD_DAYS        = 180   # 6 ヶ月
DECAY_RATIO_TRIG     = 0.15  # 実リターンの ±15% より decay が大きければ
REVIEW_HOLD_DAYS     = 30    # 1 ヶ月以上の全 leverage ETF は monitor（decay 未判定でも）


def _load_leveraged_positions() -> list[dict]:
    if not HOLDINGS.exists():
        return []
    try:
        h = json.loads(HOLDINGS.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = h.get("positions") if isinstance(h, dict) and "positions" in h else (
        list(h.values()) if isinstance(h, dict) else (h if isinstance(h, list) else [])
    )
    out = []
    today = datetime.now().date()
    for r in rows:
        if not isinstance(r, dict):
            continue
        tk = (r.get("ticker") or "").strip().upper()
        if tk not in LEV_MAP:
            continue
        try:
            sh = float(r.get("shares") or 0)
        except Exception:
            sh = 0.0
        if sh <= 0:
            continue
        entry_date = r.get("entry_date")
        hold_days = 0
        if entry_date:
            try:
                ed = datetime.fromisoformat(str(entry_date)).date()
                hold_days = (today - ed).days
            except Exception:
                pass
        out.append({
            "ticker":       tk,
            "shares":       sh,
            "entry_price":  float(r.get("entry_price") or 0),
            "entry_date":   entry_date,
            "hold_days":    hold_days,
            "underlying":   LEV_MAP[tk]["underlying"],
            "leverage":     LEV_MAP[tk]["leverage"],
        })
    return out


def _fetch_total_return(ticker: str, start: date) -> float | None:
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(ticker)
        days = (datetime.now().date() - start).days + 5
        hist = t.history(period=f"{max(days,30)}d", interval="1d", auto_adjust=True)
        if hist.empty:
            return None
        # start より後で最古の行 → 最新
        hist = hist[hist.index.date >= start]
        if hist.empty or len(hist) < 2:
            return None
        p0 = float(hist["Close"].iloc[0])
        p1 = float(hist["Close"].iloc[-1])
        if p0 <= 0:
            return None
        return (p1 / p0) - 1.0
    except Exception as e:
        print(f"[decay] {ticker} fetch error: {e}", file=sys.stderr)
        return None


def scan(dry_run: bool = False) -> dict:
    positions = _load_leveraged_positions()
    print(f"[decay] scanning {len(positions)} leveraged positions…")
    results: list[dict] = []
    for p in positions:
        start = None
        if p["entry_date"]:
            try:
                start = datetime.fromisoformat(str(p["entry_date"])).date()
            except Exception:
                pass
        if start is None:
            start = datetime.now().date() - timedelta(days=max(p["hold_days"], 30))

        etf_ret = _fetch_total_return(p["ticker"], start)
        und_ret = _fetch_total_return(p["underlying"], start)
        if etf_ret is None or und_ret is None:
            continue
        expected = und_ret * p["leverage"]
        decay = expected - etf_ret
        abs_etf = abs(etf_ret) if etf_ret != 0 else 1e-9
        decay_ratio = decay / abs_etf

        flag = False
        action = "hold"
        rationale = f"{p['ticker']} holdingDays {p['hold_days']}d; underlying {p['underlying']} ret {und_ret*100:+.2f}% × L{p['leverage']} = 期待 {expected*100:+.2f}% / 実 {etf_ret*100:+.2f}%; decay {decay*100:+.2f}%."
        if p["hold_days"] >= MIN_HOLD_DAYS and decay > 0 and decay_ratio > DECAY_RATIO_TRIG:
            flag = True
            action = "switch_to_underlying"
            rationale += f" decay_ratio {decay_ratio*100:+.1f}% > {DECAY_RATIO_TRIG*100:.0f}% threshold → 50% 乗換推奨."
        elif p["hold_days"] >= REVIEW_HOLD_DAYS and decay > 0 and decay_ratio > DECAY_RATIO_TRIG * 2:
            flag = True
            action = "partial_switch"
            rationale += f" 保有 {p['hold_days']}d だが decay 顕著、25% 乗換 検討."

        results.append({
            "ticker":         p["ticker"],
            "underlying":     p["underlying"],
            "leverage":       p["leverage"],
            "hold_days":      p["hold_days"],
            "underlying_return_pct": round(und_ret * 100, 2),
            "etf_return_pct":        round(etf_ret * 100, 2),
            "expected_return_pct":   round(expected * 100, 2),
            "decay_pct":             round(decay * 100, 2),
            "decay_ratio_pct":       round(decay_ratio * 100, 2),
            "flag":                  flag,
            "recommended_action":    action,
            "rationale":             rationale,
        })
    results.sort(key=lambda r: r["decay_pct"], reverse=True)
    out = {
        "generated_at":     time.strftime("%Y-%m-%d %H:%M:%S"),
        "positions_total":  len(positions),
        "positions_flagged": sum(1 for r in results if r["flag"]),
        "positions":        results,
    }
    if not dry_run:
        OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[decay] wrote {OUTPUT.name}: {out['positions_flagged']} flagged / {len(results)} total")
    return out


def format_for_prompt(max_entries: int = 5) -> str:
    if not OUTPUT.exists():
        return ""
    try:
        if time.time() - OUTPUT.stat().st_mtime > 30 * 24 * 3600:  # 30d TTL
            return ""
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return ""
    flagged = [p for p in data.get("positions", []) if p.get("flag")][:max_entries]
    if not flagged:
        return ""
    lines = ["## ⚠️ Leveraged ETF Decay Monitor", ""]
    for f in flagged:
        lines.append(
            f"- **{f['ticker']}** ({f['hold_days']}d hold, L{f['leverage']}) "
            f"→ {f['underlying']} {f['underlying_return_pct']:+.2f}% | ETF {f['etf_return_pct']:+.2f}% "
            f"| decay {f['decay_pct']:+.2f}% → {f['recommended_action']}"
        )
    lines.append("")
    lines.append("→ 6 ヶ月超 + decay > 実リターンの 15% は **50% を原資産 ETF に乗換**。"
                 "新規買いは原資産優先、レバレッジ枠は短期スイング目的に限定。")
    return "\n".join(lines)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = scan(dry_run=dry)
    if dry:
        print(json.dumps(out, indent=2, ensure_ascii=False))

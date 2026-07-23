"""
pair_screener.py (Part E-4)
============================

Long-Short Pair Trading シグナル検出。

判定ロジック:
  - 同業種ペアの 60 日 rolling correlation >= 0.70
  - 20 日リターン相対差 (spread) の z-score >= +2.0 (または <= -2.0)
  - divergence: 強い方を short / 弱い方を long

出力: pair_trade_candidates.json → Opus 合成に pair_opportunities として注入
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT   = BASE_DIR / "pair_trade_candidates.json"

CANDIDATE_PAIRS = [
    ("NVDA", "AMD"),
    ("META", "GOOGL"),
    ("JPM",  "GS"),
    ("V",    "MA"),
    ("TSLA", "RIVN"),
    ("XOM",  "CVX"),
    ("LLY",  "PFE"),
    ("MSFT", "GOOGL"),
    ("AAPL", "MSFT"),
    ("COST", "WMT"),
]

CORR_MIN  = 0.70
Z_THRESH  = 2.0
CORR_WIN  = 60
SPREAD_WIN = 20


def _fetch(tickers: list[str], days: int = 150):
    try:
        import yfinance as yf
        import pandas as pd
    except Exception as e:
        print(f"[pair] deps missing: {e}", file=sys.stderr)
        return None
    df = yf.download(" ".join(tickers), period=f"{days}d", interval="1d",
                     auto_adjust=True, progress=False, threads=True)
    if df is None or df.empty:
        return None
    # 'Close' level は MultiIndex (日経 1-col と single 別対応)
    try:
        close = df["Close"]
    except Exception:
        close = df
    if hasattr(close, "columns"):
        return close.dropna(how="all")
    return None


def _evaluate_pair(a: str, b: str, close):
    import numpy as np
    if a not in close.columns or b not in close.columns:
        return None
    sa = close[a].dropna()
    sb = close[b].dropna()
    common = sa.index.intersection(sb.index)
    if len(common) < CORR_WIN + SPREAD_WIN:
        return None
    sa = sa.loc[common].tail(CORR_WIN + SPREAD_WIN)
    sb = sb.loc[common].tail(CORR_WIN + SPREAD_WIN)
    ra = sa.pct_change().dropna()
    rb = sb.pct_change().dropna()
    rets = rb.index.intersection(ra.index)
    ra = ra.loc[rets]; rb = rb.loc[rets]
    if len(ra) < CORR_WIN:
        return None
    corr = float(ra.tail(CORR_WIN).corr(rb.tail(CORR_WIN)))
    if corr < CORR_MIN:
        return None
    # 20 日リターン相対差の z-score
    lookback = SPREAD_WIN
    # cumulative rets over lookback
    ca = (1 + ra.tail(lookback)).cumprod().iloc[-1] - 1
    cb = (1 + rb.tail(lookback)).cumprod().iloc[-1] - 1
    spread_latest = ca - cb
    # 過去 spreads (60d) の分布
    hist_spreads = []
    for i in range(lookback, len(ra)):
        window_a = (1 + ra.iloc[i - lookback:i]).cumprod().iloc[-1] - 1
        window_b = (1 + rb.iloc[i - lookback:i]).cumprod().iloc[-1] - 1
        hist_spreads.append(window_a - window_b)
    if len(hist_spreads) < 5:
        return None
    import statistics as _st
    mu = _st.mean(hist_spreads)
    sigma = _st.stdev(hist_spreads) if len(hist_spreads) > 1 else 0.0
    if sigma <= 1e-9:
        return None
    z = (spread_latest - mu) / sigma
    if abs(z) < Z_THRESH:
        return None
    # divergence → 強い方を short, 弱い方を long（mean-reversion）
    if spread_latest > 0:
        long_tk, short_tk = b, a
    else:
        long_tk, short_tk = a, b
    return {
        "pair":           f"{a}/{b}",
        "correlation":    round(corr, 3),
        "spread_pct":     round(spread_latest * 100, 2),
        "z_score":        round(z, 2),
        "long":           long_tk,
        "short":          short_tk,
        "rationale": (
            f"60d corr={corr:.2f}, 20d cumret spread {spread_latest*100:+.2f}% "
            f"(z={z:+.2f}σ). Mean-revert: Long {long_tk} / Short {short_tk}"
        ),
    }


def scan(dry_run: bool = False) -> dict:
    from insider_restrictions import filter_allowed_tickers, signal_record_is_restricted
    pairs = [
        pair for pair in CANDIDATE_PAIRS
        if len(filter_allowed_tickers(pair)) == 2
    ]
    tickers = sorted({t for pair in pairs for t in pair})
    close = _fetch(tickers)
    if close is None:
        out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "candidates": [], "error": "price fetch failed"}
    else:
        candidates = []
        for a, b in pairs:
            try:
                r = _evaluate_pair(a, b, close)
                if r and not signal_record_is_restricted(r):
                    candidates.append(r)
            except Exception as e:
                print(f"[pair] {a}-{b}: {e}", file=sys.stderr)
        candidates.sort(key=lambda x: abs(x["z_score"]), reverse=True)
        out = {
            "generated_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
            "pairs_tested":  len(pairs),
            "candidate_count": len(candidates),
            "candidates":    candidates,
        }
    if not dry_run:
        OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[pair] wrote {OUTPUT.name}: {len(out.get('candidates', []))} candidates")
    return out


def format_for_prompt(max_entries: int = 5) -> str:
    if not OUTPUT.exists():
        return ""
    try:
        if time.time() - OUTPUT.stat().st_mtime > 24 * 3600:
            return ""
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return ""
    from insider_restrictions import filter_signal_records
    cand = filter_signal_records(data.get("candidates", []))[:max_entries]
    if not cand:
        return ""
    lines = ["## 🔁 Long-Short Pair Divergence (|z| ≥ 2σ)", ""]
    for c in cand:
        lines.append(
            f"- **{c['pair']}** corr {c['correlation']} z {c['z_score']:+.2f} "
            f"→ Long {c['long']} / Short {c['short']}  ({c['spread_pct']:+.2f}% 20d spread)"
        )
    lines.append("")
    lines.append("→ pair_opportunities として priority_actions に投入可能。"
                 "両足同時実行が原則 — 片足のみ約定は回避。Market-neutral α。")
    return "\n".join(lines)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = scan(dry_run=dry)
    if dry:
        print(json.dumps(out, indent=2, ensure_ascii=False))

"""
squeeze_detector.py (Part E-5)
===============================

Short Squeeze 予兆検出:
  - yfinance info の shortPercentOfFloat > 20%
  - shortRatio (days-to-cover) > 7
  - RSI(14) が直近 5 営業日以内に 20 → 45 へ反発
  - ( Reddit mention spike は v1 では skip )

候補 ticker:
  - 保有銘柄すべて
  - + 短期スクリーナー候補 (screener が出した short_candidates.json の ticker)

出力: squeeze_candidates.json
副作用: "squeeze 候補は空売り対象から強制除外" するための negative list を併記。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT   = BASE_DIR / "squeeze_candidates.json"

SPF_THRESH     = 0.20
D2C_THRESH     = 7.0
RSI_LOW_DAYS   = 5
RSI_REVERSED_FROM = 25
RSI_REVERSED_TO   = 45


def _load_universe() -> list[str]:
    """holdings + short_candidates.json のユニオン"""
    uni: set[str] = set()
    h_file = BASE_DIR / "holdings.json"
    if h_file.exists():
        try:
            h = json.loads(h_file.read_text(encoding="utf-8"))
            # holdings.json: dict keyed by slot_id, or {positions: [...]}
            rows = h.get("positions") if isinstance(h, dict) and "positions" in h else (
                list(h.values()) if isinstance(h, dict) else (h if isinstance(h, list) else [])
            )
            for pos in rows:
                if not isinstance(pos, dict):
                    continue
                tk = (pos.get("ticker") or "").strip()
                if not tk or "." in tk:
                    continue
                if tk.startswith(("SLIM", "MNX", "IFREE", "NOMURA", "CASH", "GS_MMF")):
                    continue
                if pos.get("investment_type") == "cash":
                    continue
                uni.add(tk)
        except Exception:
            pass
    for f in ("short_candidates.json", "screener_candidates.json"):
        sf = BASE_DIR / f
        if not sf.exists():
            continue
        try:
            arr = json.loads(sf.read_text(encoding="utf-8"))
            items = arr if isinstance(arr, list) else arr.get("candidates", [])
            for it in items:
                tk = (it.get("ticker") or "").strip()
                if tk and "." not in tk:
                    uni.add(tk)
        except Exception:
            pass
    # fallback: a hand-picked high-short watchlist（候補が 0 件のとき）
    if not uni:
        uni = {"GME", "BBBY", "AMC", "PLTR", "RIVN", "UPST", "CVNA", "BYND"}
    from insider_restrictions import filter_allowed_tickers
    return sorted(filter_allowed_tickers(uni))


def _rsi(series, period: int = 14):
    import pandas as pd
    delta = series.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    dn = (-delta).clip(lower=0).rolling(period).mean()
    rs = up / dn.replace(0, 1e-12)
    return 100 - 100 / (1 + rs)


def _check(tk: str) -> dict | None:
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(tk)
        info = getattr(t, "info", {}) or {}
        spf = info.get("shortPercentOfFloat")
        d2c = info.get("shortRatio")
        if spf is None or d2c is None:
            return None
        if spf < SPF_THRESH or d2c < D2C_THRESH:
            return None
        hist = t.history(period="30d", interval="1d", auto_adjust=True)
        if hist.empty or len(hist) < 20:
            return None
        rsi = _rsi(hist["Close"]).dropna()
        if rsi.empty:
            return None
        rsi_latest = float(rsi.iloc[-1])
        rsi_min_recent = float(rsi.tail(RSI_LOW_DAYS + 3).min())
        # 反発判定: 直近 min が 25 以下 && 現在 45 以上 && diff 20+pt
        reversed_ok = (
            rsi_min_recent <= RSI_REVERSED_FROM
            and rsi_latest >= RSI_REVERSED_TO
            and (rsi_latest - rsi_min_recent) >= 20
        )
        if not reversed_ok:
            return None
        return {
            "ticker":             tk,
            "short_pct_of_float": round(float(spf), 4),
            "days_to_cover":      round(float(d2c), 2),
            "rsi_latest":         round(rsi_latest, 1),
            "rsi_min_5d":         round(rsi_min_recent, 1),
            "last_price":         round(float(hist["Close"].iloc[-1]), 2),
            "rationale": (
                f"SPF {spf*100:.1f}% / D2C {d2c:.1f}d / "
                f"RSI {rsi_min_recent:.1f}→{rsi_latest:.1f} 反発 (+{rsi_latest-rsi_min_recent:.1f}pt)"
            ),
        }
    except Exception as e:
        print(f"[squeeze] {tk}: {e}", file=sys.stderr)
        return None


def scan(dry_run: bool = False) -> dict:
    universe = _load_universe()
    print(f"[squeeze] scanning {len(universe)} tickers…")
    candidates = []
    for tk in universe:
        r = _check(tk)
        if r:
            candidates.append(r)
    from insider_restrictions import filter_signal_records
    candidates = filter_signal_records(candidates)
    candidates.sort(key=lambda c: c["short_pct_of_float"], reverse=True)
    out = {
        "generated_at":      time.strftime("%Y-%m-%d %H:%M:%S"),
        "universe_size":     len(universe),
        "candidate_count":   len(candidates),
        "candidates":        candidates,
        "exclude_from_short": [c["ticker"] for c in candidates],
    }
    if not dry_run:
        OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[squeeze] wrote {OUTPUT.name}: {len(candidates)} candidates")
    return out


def format_for_prompt(max_entries: int = 5) -> str:
    if not OUTPUT.exists():
        return ""
    try:
        if time.time() - OUTPUT.stat().st_mtime > 36 * 3600:
            return ""
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return ""
    from insider_restrictions import filter_signal_records
    cand = filter_signal_records(data.get("candidates", []))[:max_entries]
    if not cand:
        return ""
    lines = ["## 🚀 Short Squeeze Early Signals", ""]
    for c in cand:
        lines.append(
            f"- **{c['ticker']}** SPF {c['short_pct_of_float']*100:.1f}% "
            f"D2C {c['days_to_cover']:.1f}d RSI→{c['rsi_latest']:.1f} — {c['rationale']}"
        )
    lines.append("")
    lines.append("→ 上記 ticker は空売り対象から除外。保有済みなら trail-up / 買い増し候補。"
                 "新規なら momentum long として swing 枠で検討。")
    return "\n".join(lines)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = scan(dry_run=dry)
    if dry:
        print(json.dumps(out, indent=2, ensure_ascii=False))

"""
overnight_gap_scanner.py (Part E-3)
====================================

平日 5:45 JST に走る軽量スキャナ。

1. us_japan_arb_map.json 記載の US ティッカーの AH（pre-market）価格と前日 Close を
   yfinance 1-minute feed から取得し、|gap| >= 3% かつ volume >= 100K の銘柄を抽出。
2. 日本寄り前（9:00）に売買可能なマップ先銘柄を列挙し、bias と beta に応じて
   Japan 銘柄側の「先回り買い / 先回り売り候補」を返す。
3. Opus 合成 (analyst/__init__.py) に overnight_gap_context として注入。

出力: overnight_gap_signals.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
MAP_FILE = BASE_DIR / "us_japan_arb_map.json"
OUTPUT   = BASE_DIR / "overnight_gap_signals.json"

GAP_PCT_THRESHOLD   = 0.03
VOLUME_THRESHOLD    = 100_000
MAX_ENTRIES_OUT     = 20


def _load_map() -> list[dict]:
    if not MAP_FILE.exists():
        return []
    try:
        return json.loads(MAP_FILE.read_text(encoding="utf-8")).get("mappings", [])
    except Exception as e:
        print(f"[gap] arb_map load error: {e}", file=sys.stderr)
        return []


def _fetch_gaps(us_tickers: list[str]) -> list[dict]:
    """
    yfinance でプレ/ポスト価格と前日 close を取得。
    AH 価格の取得ルート:
      - Ticker(us).fast_info.last_price は RTH close のことが多い
      - history(period='5d', interval='1m', prepost=True) で AH bar を含む時系列を取得
    """
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[gap] yfinance missing: {e}", file=sys.stderr)
        return []

    out: list[dict] = []
    for tk in us_tickers:
        try:
            t = yf.Ticker(tk)
            # 1-minute bars (5d, prepost=True) を使う
            hist = t.history(period="5d", interval="1m", prepost=True, auto_adjust=False)
            if hist.empty or len(hist) < 2:
                continue
            # 前日 RTH close = 最新 1-min bar より前の（米時間 16:00 直前）regular close
            last_row = hist.iloc[-1]
            last_price = float(last_row.get("Close", 0) or 0)
            # 前日 regular close を取得: daily を副で
            daily = t.history(period="5d", interval="1d", prepost=False)
            if daily.empty:
                continue
            prev_close = float(daily["Close"].iloc[-2]) if len(daily) >= 2 else float(daily["Close"].iloc[-1])
            if prev_close <= 0 or last_price <= 0:
                continue
            gap_pct = (last_price - prev_close) / prev_close
            # 出来高（1m bar の直近 30 bar 合計を AH 出来高と近似）
            vol = int(hist["Volume"].iloc[-30:].sum()) if len(hist) >= 30 else int(hist["Volume"].sum())

            if abs(gap_pct) < GAP_PCT_THRESHOLD:
                continue
            if vol < VOLUME_THRESHOLD:
                continue
            out.append({
                "us_ticker":  tk,
                "prev_close": round(prev_close, 3),
                "last_price": round(last_price, 3),
                "gap_pct":    round(gap_pct, 4),
                "ah_volume":  vol,
                "direction":  "up" if gap_pct > 0 else "down",
            })
        except Exception as e:
            print(f"[gap] {tk}: fetch error {e}", file=sys.stderr)
    out.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)
    return out


def _project_to_japan(gaps: list[dict], mappings: list[dict]) -> list[dict]:
    by_us: dict[str, list[dict]] = {}
    for m in mappings:
        by_us.setdefault(m["us"].upper(), []).append(m)

    signals: list[dict] = []
    for g in gaps:
        us = g["us_ticker"].upper()
        for m in by_us.get(us, []):
            bias = m.get("bias", "positive")
            beta = float(m.get("beta", 0.5) or 0.5)
            # positive-bias: 同方向、negative-bias: 逆方向
            jp_expected_move = g["gap_pct"] * beta * (1.0 if bias == "positive" else -1.0)
            if abs(jp_expected_move) < 0.005:
                continue
            action = "buy" if jp_expected_move > 0 else "sell"
            signals.append({
                "us_ticker":           us,
                "us_gap_pct":          g["gap_pct"],
                "jp_ticker":           m.get("jp"),
                "jp_name":             m.get("name_jp"),
                "theme":               m.get("theme"),
                "expected_jp_move":    round(jp_expected_move, 4),
                "recommended_action":  action,
                "urgency":             "high" if abs(jp_expected_move) > 0.02 else "medium",
                "rationale": (
                    f"US {us} がプレ/ポストで {g['gap_pct']*100:+.2f}% ギャップ "
                    f"(AH vol {g['ah_volume']:,}). beta={beta} bias={bias} "
                    f"→ JP {m.get('jp')} 寄り {jp_expected_move*100:+.2f}% 想定"
                ),
            })
    signals.sort(key=lambda x: abs(x["expected_jp_move"]), reverse=True)
    return signals[:MAX_ENTRIES_OUT]


def scan(dry_run: bool = False) -> dict:
    mappings = _load_map()
    us_set = sorted({m["us"].upper() for m in mappings})
    print(f"[gap] scanning {len(us_set)} US tickers against {len(mappings)} JP mappings…")
    gaps = _fetch_gaps(us_set)
    jp_signals = _project_to_japan(gaps, mappings)
    out = {
        "generated_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
        "us_gap_count":   len(gaps),
        "jp_signal_count": len(jp_signals),
        "us_gaps":        gaps,
        "jp_signals":     jp_signals,
    }
    if not dry_run:
        OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[gap] wrote {OUTPUT.name}: {len(jp_signals)} JP signals")
    return out


def format_for_prompt(max_entries: int = 8) -> str:
    if not OUTPUT.exists():
        return ""
    try:
        if time.time() - OUTPUT.stat().st_mtime > 8 * 3600:  # 8h TTL（1日1回）
            return ""
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return ""
    signals = data.get("jp_signals", [])[:max_entries]
    if not signals:
        return ""
    lines = ["## 🌙 Overnight Gap Candidates (US AH → JP 寄り)", ""]
    for s in signals:
        lines.append(
            f"- **{s['jp_ticker']}** ({s['jp_name']}) {s['recommended_action']} "
            f"urgency={s.get('urgency','medium')} — {s['rationale']}"
        )
    lines.append("")
    lines.append("→ Japan 寄り（9:00）の指値として priority_actions に注入検討。"
                 "高 beta x 高 gap の組合せは urgency=high とすること。")
    return "\n".join(lines)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = scan(dry_run=dry)
    if dry:
        print(json.dumps(out, indent=2, ensure_ascii=False))

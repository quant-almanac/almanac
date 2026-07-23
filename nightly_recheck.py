#!/usr/bin/env python3
"""
nightly_recheck.py — 毎日 0:00 JST の差分チェック & 必要に応じた AI 再分析

Context:
  欧州市場 close 直後（〜23:30 JST）・米国市場 open 2-3 時間前（米東部 22:30 前）
  のタイミングで、朝 06:00 に生成した ai_portfolio_analysis.json の前提
  （VIX / SPY / QQQ / USDJPY / US10Y）が大きくぶれていないか確認する。
  有意な変動があれば `run_analysis(force=True)` を実行 → send_to_telegram で
  最新指示を Telegram に流す。

発火閾値（いずれか 1 つで発火）:
    VIX:    ±3.0 ポイント
    SPY:    ±2.0 %
    QQQ:    ±2.5 %
    USDJPY: ±1.0 円
    US10Y:  ±0.2 ポイント

コスト: Opus 再分析 1 回 ≒ $0.5-0.8、想定週 2-3 回発火 → 月 $5-10。

使い方:
    python nightly_recheck.py          # 1 回チェック（LaunchAgent から起動）
    python nightly_recheck.py --force  # 閾値無視で強制再分析
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from utils import load_json, heartbeat  # noqa: E402

CACHE_PATH = BASE / "ai_portfolio_analysis.json"

THRESH = {
    "vix_delta":    3.0,   # VIX ポイント
    "spy_pct":      2.0,   # %
    "qqq_pct":      2.5,   # %
    "usdjpy_delta": 1.0,   # 円
    "yield_delta":  0.2,   # ポイント
}


def _get_live_price(ticker: str) -> float | None:
    """yfinance で直近 close を取得。失敗したら None。"""
    try:
        import yfinance as yf  # type: ignore
        hist = yf.Ticker(ticker).history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
        # フォールバック: 5 分足 intraday
        hist = yf.Ticker(ticker).history(period="1d", interval="5m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        print(f"  ⚠️ yfinance {ticker} 取得失敗: {e}")
    return None


def _get_current_macro() -> dict:
    """macro_fetcher.get_macro_context() を呼び出して現在のマクロ指標を取得。"""
    try:
        from macro_fetcher import get_macro_context  # type: ignore
        return get_macro_context() or {}
    except Exception as e:
        print(f"  ⚠️ macro_fetcher 取得失敗: {e}")
        return {}


def check_delta() -> tuple[bool, dict, dict]:
    """
    前回分析の snapshot vs 現在の市場指標を比較。
    Returns: (should_reanalyze, triggers, deltas)
    """
    prev = load_json(CACHE_PATH, default={})
    prev_mm = (prev.get("synthesis", {}) or {}).get("market_meta_snapshot") \
              or prev.get("market_meta", {}) \
              or {}

    cur_macro = _get_current_macro()
    cur = {
        "vix":      cur_macro.get("vix"),
        "spy":      _get_live_price("SPY"),
        "qqq":      _get_live_price("QQQ"),
        "usdjpy":   _get_live_price("JPY=X"),
        "yield10y": (cur_macro.get("us10y_yield") or {}).get("value") if isinstance(cur_macro.get("us10y_yield"), dict)
                    else cur_macro.get("us10y_yield"),
    }
    prv = {
        "vix":      prev_mm.get("vix"),
        "spy":      prev_mm.get("spy_price"),
        "qqq":      prev_mm.get("qqq_price"),
        "usdjpy":   prev_mm.get("usdjpy"),
        "yield10y": prev_mm.get("us10y_yield"),
    }

    deltas: dict = {}
    triggers: dict = {}

    def _num(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    cv, pv = _num(cur["vix"]), _num(prv["vix"])
    if cv is not None and pv is not None:
        deltas["vix"] = round(abs(cv - pv), 2)
        triggers["vix_spike"] = deltas["vix"] >= THRESH["vix_delta"]

    cs, ps = _num(cur["spy"]), _num(prv["spy"])
    if cs and ps:
        deltas["spy_pct"] = round(abs((cs - ps) / ps * 100), 2)
        triggers["spy_move"] = deltas["spy_pct"] >= THRESH["spy_pct"]

    cq, pq = _num(cur["qqq"]), _num(prv["qqq"])
    if cq and pq:
        deltas["qqq_pct"] = round(abs((cq - pq) / pq * 100), 2)
        triggers["qqq_move"] = deltas["qqq_pct"] >= THRESH["qqq_pct"]

    cf, pf = _num(cur["usdjpy"]), _num(prv["usdjpy"])
    if cf and pf:
        deltas["usdjpy"] = round(abs(cf - pf), 2)
        triggers["fx_move"] = deltas["usdjpy"] >= THRESH["usdjpy_delta"]

    cy, py = _num(cur["yield10y"]), _num(prv["yield10y"])
    if cy is not None and py is not None:
        deltas["yield"] = round(abs(cy - py), 3)
        triggers["yield_shift"] = deltas["yield"] >= THRESH["yield_delta"]

    return any(triggers.values()), triggers, deltas


def main(force: bool = False) -> int:
    print(f"[nightly_recheck] 開始 {datetime.now().isoformat(timespec='seconds')}")
    should, triggers, deltas = check_delta()
    print(f"[nightly_recheck] triggers={triggers} deltas={deltas}")

    if not should and not force:
        print("[nightly_recheck] 有意な変動なし — 再分析スキップ")
        try:
            heartbeat("nightly_recheck", "ok")
        except Exception:
            pass
        return 0

    trigger_names = [k for k, v in triggers.items() if v] or ["manual_force"]

    # 差分検出を Telegram に事前通知
    try:
        # ALMANAC: telegram disabled — ai_analysis only
        # from alert import send_telegram
        # send_telegram(
        #     f"🌙 0時差分チェック: 有意変動検出\n"
        #     f"triggers: {', '.join(trigger_names)}\n"
        #     f"deltas: {deltas}\n"
        #     f"→ AI 再分析を実行中…"
        # )
        pass
    except Exception as e:
        print(f"  ⚠️ 事前通知失敗: {e}")

    # 再分析実行
    try:
        from analyst import run_analysis, send_to_telegram as send_ai_telegram
        result = run_analysis(force=True)
        if result and "synthesis" in result:
            send_ai_telegram(result)
            print("[nightly_recheck] ✅ 再分析 + Telegram 配信完了")
            try:
                heartbeat("nightly_recheck", "ok")
            except Exception:
                pass
            return 0
        else:
            raise RuntimeError("run_analysis returned no synthesis")
    except Exception as e:
        print(f"[nightly_recheck] ❌ 再分析失敗: {e}")
        try:
            # ALMANAC: telegram disabled — ai_analysis only
            # from alert import send_telegram
            # send_telegram(f"❌ 0 時再分析失敗: {e}")
            pass
        except Exception:
            pass
        try:
            heartbeat("nightly_recheck", "error", error=str(e))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ALMANAC nightly recheck")
    parser.add_argument("--force", action="store_true", help="閾値無視で強制再分析")
    args = parser.parse_args()
    sys.exit(main(force=args.force))

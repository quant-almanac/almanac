"""
earnings_proximity_manager.py (Part E-6)
========================================

保有 US 銘柄の決算 7 営業日前をスキャンし、
  - Option Chain から ATM Straddle を取得
  - implied_move_pct = (ATM Call + ATM Put) / Stock * 0.85
  - |position_size_pct × implied_move_pct| > 1.5% of portfolio → hedge 推奨
  - 過去 beat_rate < 50% なら前日 trim 強制

出力: earnings_hedge_suggestions.json
      Opus 合成に earnings_hedge_context として注入
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pseudo_tickers import is_non_earnings_ticker

BASE_DIR = Path(__file__).parent
OUTPUT   = BASE_DIR / "earnings_hedge_suggestions.json"
HOLDINGS = BASE_DIR / "holdings.json"
ANALYSIS = BASE_DIR / "ai_portfolio_analysis.json"
EARNINGS_OVERRIDES = BASE_DIR / "earnings_calendar_overrides.json"

PROX_DAYS              = 10    # 決算 10 営業日前から監視 (IV 膨張は 5-7 日前で顕在化だが、AAPL 等 8bd も救済)
IMPL_MOVE_FUDGE        = 0.85  # ATM straddle → implied move 係数 (Bachelier/近似)
DAMAGE_PCT_THRESHOLD   = 0.015 # total_portfolio の 1.5% で hedge 推奨
BEAT_RATE_FORCE_TRIM   = 0.50
YFIN_RETRY_ATTEMPTS    = 3     # yfinance .calendar の intermittent 404/rate-limit 対策リトライ
OUTPUT_SCHEMA_VERSION  = 2


def _business_days_until(target: date) -> int:
    """todayからtargetまで営業日 (simple: weekday only, 祝日無視)"""
    today = datetime.now().date()
    if target < today:
        return -1
    days = 0
    cur = today
    while cur < target:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def _load_holdings() -> list[dict]:
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
    for r in rows:
        if not isinstance(r, dict):
            continue
        tk = (r.get("ticker") or "").strip()
        if not tk or "." in tk:   # US 株のみ (JP は option 流動性低)
            continue
        if is_non_earnings_ticker(tk) or tk.startswith(("SLIM", "MNX", "IFREE", "NOMURA", "CASH", "GS_MMF")):
            continue
        if r.get("investment_type") == "cash":
            continue
        try:
            sh = float(r.get("shares") or 0)
        except Exception:
            sh = 0.0
        if sh <= 0:
            continue
        out.append({"ticker": tk, "shares": sh, "currency": r.get("currency", "USD")})
    # 同 ticker を aggregate
    agg: dict[str, dict] = {}
    for r in out:
        k = r["ticker"]
        if k not in agg:
            agg[k] = {"ticker": k, "shares": 0.0, "currency": r["currency"]}
        agg[k]["shares"] += r["shares"]
    return list(agg.values())


def _total_portfolio_jpy() -> float:
    """ai_portfolio_analysis.json の portfolio_total をベースに"""
    if ANALYSIS.exists():
        try:
            data = json.loads(ANALYSIS.read_text(encoding="utf-8"))
            pt = data.get("portfolio_total")
            if isinstance(pt, dict):
                return float(pt.get("total_jpy") or pt.get("total") or 0)
            if isinstance(pt, (int, float)):
                return float(pt)
        except Exception:
            pass
    return 30_000_000.0  # fallback


def _official_earnings_override(tk: str, *, today: date | None = None) -> dict | None:
    """Return a time-bounded issuer-source override, never a stale past date."""
    today = today or datetime.now().date()
    try:
        raw = json.loads(EARNINGS_OVERRIDES.read_text(encoding="utf-8"))
        row = (raw.get("overrides") or {}).get(str(tk).upper())
        if not isinstance(row, dict):
            return None
        earnings_date = date.fromisoformat(str(row.get("earnings_date")))
        valid_until = date.fromisoformat(str(row.get("valid_until") or row.get("earnings_date")))
        if earnings_date < today or today > valid_until:
            return None
        return {
            "date": earnings_date,
            "source": str(row.get("source") or "issuer_override"),
            "verified_at": row.get("verified_at"),
        }
    except Exception:
        return None


def snapshot_is_current(*, today: date | None = None) -> bool:
    """True only when today's snapshot includes every active issuer override."""
    today = today or datetime.now().date()
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(str(data.get("generated_at"))).date()
        if data.get("schema_version") != OUTPUT_SCHEMA_VERSION or generated != today:
            return False
        rows = {
            str(row.get("ticker") or "").upper(): row
            for row in list(data.get("suggestions") or []) + list(data.get("skipped") or [])
            if isinstance(row, dict)
        }
        overrides = json.loads(EARNINGS_OVERRIDES.read_text(encoding="utf-8")).get("overrides") or {}
        for ticker in overrides:
            override = _official_earnings_override(ticker, today=today)
            if not override:
                continue
            row = rows.get(str(ticker).upper())
            if (
                not row
                or str(row.get("earnings") or row.get("earnings_date") or "") != override["date"].isoformat()
                or str(row.get("earnings_source") or "") != override["source"]
            ):
                return False
        return True
    except Exception:
        return False


def _next_earnings_from_yfinance(tk: str):
    """
    次回決算日を取得。yfinance は intermittent 404/rate-limit を返すため、
    (a) .calendar dict / DataFrame の両方を許容
    (b) フォールバック: t.earnings_dates Future (より安定)
    (c) 最大 YFIN_RETRY_ATTEMPTS 回リトライ (指数バックオフ)
    """
    try:
        import yfinance as yf
    except Exception:
        return None
    last_err: Exception | None = None
    for attempt in range(YFIN_RETRY_ATTEMPTS):
        try:
            t = yf.Ticker(tk)
            # --- Path 1: .calendar (primary) ---
            try:
                cal = t.calendar
                if cal is not None and not (hasattr(cal, "empty") and cal.empty):
                    if isinstance(cal, dict):
                        ed = cal.get("Earnings Date") or cal.get("earnings_date")
                        if isinstance(ed, list) and ed:
                            ed = ed[0]
                    else:
                        ed = cal.iloc[0, 0]
                    if hasattr(ed, "date"):
                        ed = ed.date()
                    if isinstance(ed, datetime):
                        ed = ed.date()
                    if isinstance(ed, date):
                        return ed
            except Exception as e1:
                last_err = e1
            # --- Path 2: .earnings_dates (fallback) ---
            try:
                ed_df = t.earnings_dates
                if ed_df is not None and hasattr(ed_df, "index") and len(ed_df) > 0:
                    import pandas as _pd
                    now = _pd.Timestamp.now(tz=ed_df.index.tz) if ed_df.index.tz else _pd.Timestamp.now()
                    fut = ed_df[ed_df.index > now]
                    if len(fut) > 0:
                        next_ts = fut.index.min()
                        return next_ts.date() if hasattr(next_ts, "date") else next_ts
            except Exception as e2:
                last_err = e2
            # 両経路失敗 → リトライ（指数バックオフ）
            if attempt < YFIN_RETRY_ATTEMPTS - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            return None
        except Exception as e:
            last_err = e
            if attempt < YFIN_RETRY_ATTEMPTS - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            print(f"[earnings] {tk} calendar error: {e}", file=sys.stderr)
            return None
    if last_err:
        print(f"[earnings] {tk} calendar error (after {YFIN_RETRY_ATTEMPTS} retries): {last_err}", file=sys.stderr)
    return None


def _next_earnings_with_source(tk: str) -> dict | None:
    override = _official_earnings_override(tk)
    if override:
        return override
    value = _next_earnings_from_yfinance(tk)
    if value:
        return {"date": value, "source": "yfinance"}
    return None


def _next_earnings(tk: str):
    """Compatibility wrapper returning only the date."""
    result = _next_earnings_with_source(tk)
    return result.get("date") if result else None


def _atm_straddle(tk: str, target_date: date) -> dict | None:
    """target_date 直後の expiry で ATM call + put の mid を合算"""
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(tk)
        spot = float(t.fast_info.last_price or 0)
        if spot <= 0:
            hist = t.history(period="5d")
            if hist.empty:
                return None
            spot = float(hist["Close"].iloc[-1])
        exps = getattr(t, "options", None) or []
        if not exps:
            return None
        # target_date 以降で最も近い expiry
        candidate_exp = None
        for e in exps:
            try:
                ed = datetime.strptime(e, "%Y-%m-%d").date()
            except Exception:
                continue
            if ed >= target_date:
                candidate_exp = e
                break
        if candidate_exp is None:
            candidate_exp = exps[0]
        chain = t.option_chain(candidate_exp)
        calls = chain.calls
        puts  = chain.puts
        if calls is None or calls.empty or puts is None or puts.empty:
            return None
        # ATM: |strike - spot| 最小
        calls = calls.copy(); puts = puts.copy()
        calls["diff"] = (calls["strike"] - spot).abs()
        puts["diff"]  = (puts["strike"]  - spot).abs()
        c_row = calls.sort_values("diff").iloc[0]
        p_row = puts.sort_values("diff").iloc[0]
        c_mid = (float(c_row.get("bid", 0)) + float(c_row.get("ask", 0))) / 2 or float(c_row.get("lastPrice", 0))
        p_mid = (float(p_row.get("bid", 0)) + float(p_row.get("ask", 0))) / 2 or float(p_row.get("lastPrice", 0))
        if c_mid <= 0 or p_mid <= 0:
            return None
        straddle = c_mid + p_mid
        implied_move_pct = (straddle / spot) * IMPL_MOVE_FUDGE
        return {
            "spot":              round(spot, 2),
            "expiry":            candidate_exp,
            "call_mid":          round(c_mid, 2),
            "put_mid":           round(p_mid, 2),
            "straddle":          round(straddle, 2),
            "implied_move_pct":  round(implied_move_pct, 4),
        }
    except Exception as e:
        print(f"[earnings] {tk} option chain error: {e}", file=sys.stderr)
        return None


def _historical_beat_rate(tk: str) -> float | None:
    """yfinance.earnings_history から EPS surprise を参照"""
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(tk)
        eh = getattr(t, "earnings_history", None)
        if eh is None or (hasattr(eh, "empty") and eh.empty):
            return None
        # epsEstimate vs epsActual
        beats = 0; total = 0
        cols = set(eh.columns) if hasattr(eh, "columns") else set()
        act_col = "epsActual" if "epsActual" in cols else ("actual" if "actual" in cols else None)
        est_col = "epsEstimate" if "epsEstimate" in cols else ("estimate" if "estimate" in cols else None)
        if not act_col or not est_col:
            return None
        for _, row in eh.iterrows():
            try:
                a = float(row[act_col]); e = float(row[est_col])
            except Exception:
                continue
            if a is None or e is None:
                continue
            total += 1
            if a > e:
                beats += 1
        if total == 0:
            return None
        return beats / total
    except Exception:
        return None


def scan(dry_run: bool = False) -> dict:
    holdings = _load_holdings()
    total_jpy = _total_portfolio_jpy()
    print(f"[earnings] scanning {len(holdings)} US holdings, portfolio=¥{total_jpy:,.0f}")
    # 為替 USD→JPY
    try:
        import yfinance as yf
        fx = float(yf.Ticker("JPY=X").fast_info.last_price or 150.0)
    except Exception:
        fx = 150.0

    suggestions: list[dict] = []
    skipped: list[dict] = []  # 観測性: なぜ hedge 対象から外されたかを記録
    for h in holdings:
        tk = h["ticker"]; sh = h["shares"]
        earnings_record = _next_earnings_with_source(tk)
        if not earnings_record:
            skipped.append({"ticker": tk, "reason": "no_earnings_date"})
            continue
        ed = earnings_record["date"]
        earnings_source = earnings_record.get("source")
        bdays = _business_days_until(ed)
        if bdays < 0 or bdays > PROX_DAYS:
            skipped.append({
                "ticker": tk, "reason": "out_of_window", "earnings": ed.isoformat(),
                "earnings_source": earnings_source, "bdays": bdays,
            })
            continue
        info = _atm_straddle(tk, ed)
        if not info:
            skipped.append({
                "ticker": tk, "reason": "no_option_chain", "earnings": ed.isoformat(),
                "earnings_source": earnings_source, "bdays": bdays,
            })
            continue
        # USD position value
        pos_usd = info["spot"] * sh
        pos_jpy = pos_usd * fx if h.get("currency", "USD") == "USD" else pos_usd
        pos_pct = pos_jpy / total_jpy if total_jpy > 0 else 0.0
        # damage = 現在ポジションの JPY × implied_move_pct
        damage_jpy  = pos_jpy * info["implied_move_pct"]
        damage_pct  = damage_jpy / total_jpy if total_jpy > 0 else 0.0

        beat_rate = _historical_beat_rate(tk)
        force_trim = beat_rate is not None and beat_rate < BEAT_RATE_FORCE_TRIM

        needs_hedge = damage_pct > DAMAGE_PCT_THRESHOLD
        if not (needs_hedge or force_trim):
            skipped.append({
                "ticker": tk, "reason": "damage_below_threshold",
                "earnings": ed.isoformat(), "earnings_source": earnings_source, "bdays": bdays,
                "damage_pct": round(damage_pct * 100, 3),
                "threshold_pct": round(DAMAGE_PCT_THRESHOLD * 100, 2),
                "implied_move_pct": round(info["implied_move_pct"] * 100, 2),
                "position_pct": round(pos_pct * 100, 2),
            })
            continue

        if force_trim:
            action = "force_trim_50pct"
        elif info["implied_move_pct"] > 0.07:
            action = "buy_atm_put"
        else:
            action = "trim_50pct"

        suggestions.append({
            "ticker":            tk,
            "earnings_date":     ed.isoformat(),
            "earnings_source":   earnings_source,
            "business_days":     bdays,
            "shares":            sh,
            "spot":              info["spot"],
            "position_usd":      round(pos_usd, 0),
            "position_pct":      round(pos_pct * 100, 2),
            "expiry":            info["expiry"],
            "atm_straddle_usd":  info["straddle"],
            "implied_move_pct":  round(info["implied_move_pct"] * 100, 2),
            "damage_jpy":        int(damage_jpy),
            "damage_pct":        round(damage_pct * 100, 2),
            "beat_rate":         round(beat_rate, 2) if beat_rate is not None else None,
            "recommended_action": action,
            "rationale": (
                f"{tk} 決算 {ed.isoformat()} (T-{bdays}bd). Straddle {info['straddle']}USD "
                f"→ implied move {info['implied_move_pct']*100:.1f}%. "
                f"Position {pos_pct*100:.1f}% ⇒ damage {damage_pct*100:.2f}% of port. "
                f"Beat rate {beat_rate if beat_rate is not None else 'n/a'}"
            ),
        })

    suggestions.sort(key=lambda s: s["damage_pct"], reverse=True)
    out = {
        "schema_version":    OUTPUT_SCHEMA_VERSION,
        "generated_at":      time.strftime("%Y-%m-%d %H:%M:%S"),
        "holdings_scanned":  len(holdings),
        "portfolio_jpy":     total_jpy,
        "usd_jpy":           round(fx, 2),
        "prox_days":         PROX_DAYS,
        "damage_threshold_pct": round(DAMAGE_PCT_THRESHOLD * 100, 2),
        "suggestion_count":  len(suggestions),
        "suggestions":       suggestions,
        "skipped":           skipped,  # 観測性: なぜ 0 suggestion かを Opus に伝えるため保持
    }
    if not dry_run:
        try:
            from utils import atomic_write_json
            atomic_write_json(OUTPUT, out)
        except Exception:
            OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[earnings] wrote {OUTPUT.name}: {len(suggestions)} suggestions, {len(skipped)} skipped")
        if skipped:
            reason_counts: dict[str, int] = {}
            for s in skipped:
                reason_counts[s["reason"]] = reason_counts.get(s["reason"], 0) + 1
            print(f"[earnings] skip reasons: {reason_counts}")
    return out


def format_for_prompt(max_entries: int = 6) -> str:
    if not OUTPUT.exists():
        return ""
    try:
        if time.time() - OUTPUT.stat().st_mtime > 24 * 3600:
            return ""
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return ""
    sug = data.get("suggestions", [])[:max_entries]
    skipped = data.get("skipped", []) or []
    # 観測性: suggestion も skipped も無ければ静黙（scan 未実行）
    if not sug and not skipped:
        return ""

    lines = ["## 🎯 Earnings Proximity Hedge / Trim 候補", ""]
    if sug:
        for s in sug:
            lines.append(
                f"- **{s['ticker']}** T-{s['business_days']}bd ({s['earnings_date']}) "
                f"impl-move {s['implied_move_pct']:.1f}% / damage {s['damage_pct']:.2f}% "
                f"→ {s['recommended_action']}"
            )
        lines.append("")
        lines.append(f"→ damage_pct > {data.get('damage_threshold_pct', 1.5)}% の銘柄は priority_actions に hedge/trim として確実に注入。"
                     "beat_rate < 0.5 の銘柄は前日 trim_50pct を強制採用。")
    else:
        lines.append(f"*閾値超の hedge 対象は現在なし (damage>{data.get('damage_threshold_pct', 1.5)}% 条件)。*")

    # 決算週間近だが閾値下 or option chain 無しの銘柄も Opus に伝える（hedge ではなく monitor として）
    in_window_skips = [s for s in skipped
                       if s.get("reason") in ("damage_below_threshold", "no_option_chain")
                       and s.get("bdays") is not None and 0 <= s["bdays"] <= 10]
    if in_window_skips:
        lines.append("")
        lines.append("### 📅 決算接近銘柄（hedge 閾値下 or option chain 未取得）")
        for s in in_window_skips[:8]:
            if s["reason"] == "damage_below_threshold":
                lines.append(
                    f"- {s['ticker']} T-{s['bdays']}bd ({s['earnings']}) "
                    f"impl-move {s.get('implied_move_pct','?')}% / damage {s.get('damage_pct','?')}% "
                    f"(pos {s.get('position_pct','?')}%) → monitor のみ"
                )
            else:
                lines.append(f"- {s['ticker']} T-{s['bdays']}bd ({s['earnings']}) option chain 取得失敗 → 決算前日 trim_25pct 検討")
        lines.append("→ priority_actions には含めず hold_notes / risk_warnings で言及すること。")
    return "\n".join(lines)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = scan(dry_run=dry)
    if dry:
        print(json.dumps(out, indent=2, ensure_ascii=False))

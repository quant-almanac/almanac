"""
GET /api/today — /today オブシディアン・コンソール v5 用の合成エンドポイント。

既存 JSON を読み取り専用で合成して返す（LLM コールなし）:
  - ai_portfolio_analysis.json  (synthesis / long / medium / margin / short lanes / redteam)
  - action_state.json           (推奨→指値→約定のライフサイクル join)
  - agent_reliability.json      (自己計測スコアカード)
  - macro_state.json            (VIX / 金利)
  - currency_policy_state.json  (通貨目標)
  - guard_state.json            (行動ガードレール)
  - nisa_portfolio.json         (NISA 枠残)
"""
import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from api.routes.scenario import build_scenario_summary
from nav_recorder import modified_dietz_twr_series

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


def _load(name: str) -> dict:
    try:
        with open(BASE_DIR / name, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        # "2026-07-03 06:12" 形式
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
    return None


# ── action_state ライフサイクル join ──────────────────────────

def _match_lifecycle(action: dict, states: dict, as_of: datetime | None) -> dict | None:
    """synthesis の action と action_state.json のエントリを突合する。
    正規化directionと構造化scopeが一致する場合だけ結合する。"""
    try:
        from action_state_tracker import normalize_action_type
        atype = normalize_action_type(action.get("type") or action.get("action_type"))
    except Exception:
        atype = str(action.get("type") or action.get("action_type") or "")
    ticker = action.get("ticker")
    scope_fields = (
        ("execution_owner", "execution_owner"),
        ("execution_broker", "execution_broker"),
        ("execution_account", "execution_account"),
        ("execution_investment_type", "execution_investment_type"),
    )
    position_keys = tuple(sorted(str(k) for k in (action.get("execution_position_keys") or [])))
    pool = []
    for value in states.values():
        if value.get("ticker") != ticker:
            continue
        try:
            from action_state_tracker import normalize_action_type
            state_type = normalize_action_type(value.get("action_type") or value.get("type"))
        except Exception:
            state_type = str(value.get("action_type") or value.get("type") or "")
        if state_type != atype:
            continue
        mismatch = False
        for action_key, state_key in scope_fields:
            expected = action.get(action_key)
            actual = value.get(state_key)
            if expected is not None or actual is not None:
                if str(expected or "") != str(actual or ""):
                    mismatch = True
                    break
        if mismatch:
            continue
        state_keys = tuple(sorted(str(k) for k in (value.get("execution_position_keys") or [])))
        if (position_keys or state_keys) and position_keys != state_keys:
            continue
        pool.append(value)
    if not pool:
        return None
    if as_of:
        same_run = [
            v for v in pool
            if (dt := _parse_dt(v.get("recommended_at"))) and abs((dt - as_of).total_seconds()) < 3600
        ]
        if same_run:
            pool = same_run
    return max(pool, key=lambda v: v.get("recommended_at") or "")


def _lifecycle_view(entry: dict | None, expiry_minutes: int | None) -> dict:
    if not entry:
        return {"status": "proposed"}
    status = entry.get("status") or "proposed"
    from execution_safety import execution_expiry_at

    expiry_deferred = bool(entry.get("expiry_deferred_until_reprice"))
    expiry_dt = execution_expiry_at(entry, expiry_minutes)
    expiry_at = expiry_dt.isoformat() if expiry_dt is not None else None
    if expiry_dt is not None:
        if status == "pending":
            if expiry_dt <= datetime.now(expiry_dt.tzinfo):
                # This endpoint is deliberately read-only. Persistent cleanup
                # will catch up later, but an elapsed limit must never render
                # as an active pending order in the meantime.
                status = "expired"
    return {
        "id": entry.get("id"),
        "status": status,
        "recommended_at": entry.get("recommended_at"),
        "placed_at": entry.get("placed_at"),
        "filled_at": entry.get("filled_at"),
        "expiry_at": expiry_at,
        "expiry_starts_at": entry.get("expiry_starts_at"),
        "expiry_ends_at": entry.get("expiry_ends_at"),
        "expiry_deferred_until_reprice": expiry_deferred,
        "market_reprice_after": entry.get("market_reprice_after"),
        "note": entry.get("note"),
    }


# ── アナリストレポートの whitelist ────────────────────────────

_TIER_FIELDS = [
    "health", "health_reason", "summary", "overall_stance", "stance_reason",
    "weekly_theme", "nisa_strategy", "news_impact", "signals_quality",
    "risk_warnings", "stop_loss_alerts", "hold_notes", "new_candidates",
    "new_entries", "profit_taking", "opportunity_highlights",
    "high_return_opportunity", "medium_high_return_strategy",
    "watchlist_alert", "geopolitical_note", "crisis_strategy",
    "loss_management", "recovery_scenario", "optimization_insight",
    "rebalance_summary", "short_not_recommended", "margin_health",
    "margin_summary", "model_used",
]


def _tier_report(tier: dict) -> dict:
    return {k: tier[k] for k in _TIER_FIELDS if tier.get(k)}


# ── v7 相場暦: charts / almanac / delta ───────────────────────

_ohlcv_cache: dict[str, tuple[float, list[dict]]] = {}


def _ticker_closes(ticker: str, days: int = 60) -> list[dict] | None:
    """data/ohlcv/{ticker}.parquet から直近 N 営業日の終値を返す。

    parquet の mtime が同じ間だけプロセス内キャッシュを利用する。data_fetcher の
    書き換え直後は mtime が変わるため、TTL待ちなしで次のリクエストから再読込する。
    """
    path = BASE_DIR / "data" / "ohlcv" / f"{ticker}.parquet"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    cached = _ohlcv_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1][-days:]
    try:
        import pandas as pd

        df = pd.read_parquet(path, columns=None)
        closes = df["Close"]
        if hasattr(closes, "columns"):  # MultiIndex (Price, Ticker)
            closes = closes.iloc[:, 0]
        series = [
            {"d": idx.strftime("%m/%d"), "c": round(float(v), 2)}
            for idx, v in closes.dropna().items()
        ]
        _ohlcv_cache[key] = (mtime, series)
        return series[-days:]
    except Exception:
        return None


def _ticker_prefix(s: str) -> str | None:
    """hold_notes / stop_loss_alerts の行頭ティッカーを抽出（例: 'AVGO（特定+一般）: ...' → AVGO）"""
    m = re.match(r"\s*([A-Z0-9][A-Z0-9._]{0,11})", s or "")
    return m.group(1) if m else None


def _build_holdings_intel(long_a: dict, medium_a: dict, synthesis: dict) -> dict:
    """銘柄ごとの AI 見解を集約: 保有ノート + ストップロス + GINN ボラ。"""
    intel: dict[str, dict] = {}
    for tier_key, tier in (("long", long_a), ("medium", medium_a)):
        for note in tier.get("hold_notes") or []:
            t = _ticker_prefix(note)
            if t:
                intel.setdefault(t, {})["note"] = note
                intel[t]["tier"] = tier_key
    for s in synthesis.get("stop_loss_alerts") or long_a.get("stop_loss_alerts") or []:
        t = _ticker_prefix(s)
        if t:
            intel.setdefault(t, {})["stop_loss"] = s
    for t, v in (long_a.get("ginn_vol") or {}).items():
        if isinstance(v, (int, float)):
            intel.setdefault(t, {})["ginn_vol"] = v
    return intel


_BENCH_CACHE = BASE_DIR / "data" / "benchmark_series_cache.json"


def _fetch_benchmark_closes() -> dict:
    """^GSPC / ^N225 / USDJPY の日次終値（3ヶ月、12h ファイルキャッシュ）。"""
    cached_series: dict = {}
    try:
        if _BENCH_CACHE.exists():
            c = json.loads(_BENCH_CACHE.read_text())
            cached_series = c.get("series") or {}
            age = (datetime.now() - datetime.fromisoformat(c["fetched_at"])).total_seconds()
            if age < 12 * 3600 and c.get("version") == 2 and cached_series:
                return cached_series
    except Exception:
        pass
    series: dict = {}
    try:
        import yfinance as yf

        df = yf.download(
            ["^GSPC", "^N225", "JPY=X"],
            period="3mo",
            interval="1d",
            progress=False,
            auto_adjust=False,
        )["Close"]
        for col, key in (("^GSPC", "sp500"), ("^N225", "nikkei"), ("JPY=X", "usdjpy")):
            if col in df.columns:
                s = df[col].dropna()
                series[key] = {i.strftime("%Y-%m-%d"): float(v) for i, v in s.items()}
    except Exception:
        series = {}
    if series:
        try:
            _BENCH_CACHE.write_text(
                json.dumps({"version": 2, "fetched_at": datetime.now().isoformat(), "series": series})
            )
        except Exception:
            pass
    return series or cached_series


def _build_benchmark(guard: dict) -> dict | None:
    """入出金調整済み TWR を S&P500 / 日経平均と同じ 0% 起点で比較する。"""
    ph = guard.get("pnl_history") or []
    dates = sorted({str(r.get("date"))[:10] for r in ph if r.get("date")})
    if len(dates) < 5:
        return None

    try:
        twr = modified_dietz_twr_series(date_from=dates[0], date_to=dates[-1])
    except Exception:
        return None
    points = twr.get("points") or []
    if twr.get("error") is not None or len(points) < 2:
        return None

    series_dates = [str(p["date"]) for p in points]
    portfolio = [round(float(p["twr_pct"]), 3) for p in points]
    out: dict = {
        "dates": [d[5:] for d in series_dates],
        "portfolio": portfolio,
        "outperf": {},
        "method": "modified_dietz",
        "confirmed": bool(twr.get("confirmed")),
        "clean_ok": bool(twr.get("clean_ok")),
        "clean_since": twr.get("clean_since"),
        "start_date": twr.get("v_start_date"),
        "end_date": twr.get("v_end_date"),
        "period_days_actual": twr.get("period_days_actual"),
        "net_cash_flow": twr.get("net_cash_flow"),
        "basis": {
            "portfolio": "jpy_modified_dietz_twr",
            "sp500": "jpy_unhedged_price_return",
            "nikkei": "jpy_price_return",
        },
    }
    closes_by_key = _fetch_benchmark_closes()
    for key in ("sp500", "nikkei"):
        closes = closes_by_key.get(key)
        if not closes:
            continue
        sorted_dates = sorted(closes)
        fx_closes = closes_by_key.get("usdjpy") if key == "sp500" else None
        if key == "sp500" and not fx_closes:
            continue
        sorted_fx_dates = sorted(fx_closes) if fx_closes else []
        vals: list[float | None] = []
        base = None
        for d in series_dates:
            c = None
            for sd in reversed(sorted_dates):  # 直近の営業日終値へ forward-fill
                if sd <= d:
                    c = closes[sd]
                    break
            if c is not None and fx_closes:
                fx = None
                for fd in reversed(sorted_fx_dates):
                    if fd <= d:
                        fx = fx_closes[fd]
                        break
                c = c * fx if fx is not None else None
            if c is None:
                vals.append(None)
                continue
            if base is None:
                base = c
            vals.append(round((c / base - 1) * 100, 3))
        if any(v is not None for v in vals):
            out[key] = vals
            last_bench = next((v for v in reversed(vals) if v is not None), None)
            if last_bench is not None:
                out["outperf"][key] = round(portfolio[-1] - last_bench, 2)
    return out


# crontab / 取引時間 由来の定時イベント（JST・平日）。時刻は crontab の実設定値。
_SYSTEM_EVENTS = [
    ("05:45", "ギャップスキャン"),
    ("06:00", "US 朝スクリーナー"),
    ("06:15", "決算接近チェック"),
    ("07:25", "DCA 評価"),
    ("07:30", "デルタ分析"),
    ("15:30", "JP スクリーナー"),
    ("17:30", "データ更新"),
    ("17:50", "貸借銘柄同期"),
    ("18:00", "夕スクリーナー群"),
    ("19:30", "シグナル追跡"),
    ("22:00", "インサイダー追跡"),
]

_JST = ZoneInfo("Asia/Tokyo")
_NEW_YORK = ZoneInfo("America/New_York")


def _market_sessions(now: datetime) -> list[dict]:
    """Return today's market clock in JST, including US extended hours.

    NY local clock conversion keeps the display correct across US daylight
    saving changes.  A cross-midnight regular session is intentionally kept as
    one interval; the frontend splits it across the 24-hour canvas.
    """
    now_jst = now.replace(tzinfo=_JST) if now.tzinfo is None else now.astimezone(_JST)
    ny_now = now_jst.astimezone(_NEW_YORK)
    ny_date = ny_now.date()

    def et_clock(hour: int, minute: int = 0, *, day_offset: int = 0) -> str:
        local = datetime(
            ny_date.year,
            ny_date.month,
            ny_date.day,
            hour,
            minute,
            tzinfo=_NEW_YORK,
        ) + timedelta(days=day_offset)
        return local.astimezone(_JST).strftime("%H:%M")

    try:
        from execution_safety import market_session_context

        jp_context = market_session_context("1306.T", now_jst)
        us_context = market_session_context("SPY", now_jst)
    except Exception:
        jp_context = {"status": "unresolved", "reason": "calendar_unavailable"}
        us_context = {"status": "unresolved", "reason": "calendar_unavailable"}
    jp_open = jp_context.get("status") == "trading_day"
    us_open = us_context.get("status") == "trading_day"

    def session_meta(context: dict) -> dict:
        return {
            "calendar_status": context.get("status"),
            "calendar_reason": context.get("reason"),
            "next_market_open": context.get("next_market_open"),
        }
    return [
        {
            "id": "jpx-am",
            "label": "東証 前場",
            "market": "JP",
            "phase": "regular",
            "start": "09:00",
            "end": "11:30",
            "timezone": "Asia/Tokyo",
            "is_open_day": jp_open,
            **session_meta(jp_context),
        },
        {
            "id": "jpx-pm",
            "label": "東証 後場",
            "market": "JP",
            "phase": "regular",
            "start": "12:30",
            "end": "15:30",
            "timezone": "Asia/Tokyo",
            "is_open_day": jp_open,
            **session_meta(jp_context),
        },
        {
            "id": "us-pre",
            "label": "米国 プレ",
            "market": "US",
            "phase": "pre",
            "start": et_clock(4),
            "end": et_clock(9, 30),
            "timezone": "America/New_York",
            "is_open_day": us_open,
            **session_meta(us_context),
        },
        {
            "id": "us-regular",
            "label": "米国 通常",
            "market": "US",
            "phase": "regular",
            "start": et_clock(9, 30),
            "end": et_clock(16),
            "timezone": "America/New_York",
            "is_open_day": us_open,
            **session_meta(us_context),
        },
        {
            "id": "us-after",
            "label": "米国 アフター",
            "market": "US",
            "phase": "after",
            "start": et_clock(16),
            "end": et_clock(20),
            "timezone": "America/New_York",
            "is_open_day": us_open,
            **session_meta(us_context),
        },
    ]


def _build_almanac(board: list[dict], analysis: dict, currency: dict, nisa: dict, now: datetime, guard: dict) -> dict:
    is_weekday = now.weekday() < 5

    today_events: list[dict] = []
    if is_weekday:
        today_events = [{"t": t, "label": l, "kind": "system"} for t, l in _SYSTEM_EVENTS]

    as_of_dt = _parse_dt(analysis.get("as_of"))
    if as_of_dt and as_of_dt.date() == now.date():
        today_events.append({"t": as_of_dt.strftime("%H:%M"), "label": "統合分析（Opus 合成）", "kind": "analysis"})

    for b in board:
        exp = _parse_dt(b.get("lifecycle", {}).get("expiry_at"))
        if exp and exp.date() == now.date():
            today_events.append({"t": exp.strftime("%H:%M"), "label": f"{b.get('ticker')} 指値失効", "kind": "order"})
    today_events.sort(key=lambda e: e["t"])

    # ── 今後の暦（70日以内） ──
    upcoming: list[dict] = []
    horizon = now + timedelta(days=70)

    def add(date_str: str | None, label: str, kind: str, ticker: str | None = None):
        d = _parse_dt(date_str)
        if d and now.date() <= d.date() <= horizon.date():
            upcoming.append({"date": d.strftime("%Y-%m-%d"), "label": label, "kind": kind, "ticker": ticker})

    # 決算日（earnings_hedge_suggestions.json の scan 結果）
    earn = _load("earnings_hedge_suggestions.json")
    seen = set()
    for row in (earn.get("suggestions") or []) + (earn.get("skipped") or []):
        t, ed = row.get("ticker"), row.get("earnings")
        if t and ed and t not in seen:
            seen.add(t)
            add(ed, f"{t} 決算", "earnings", t)

    # 指値失効（本日以外）
    for b in board:
        exp = _parse_dt(b.get("lifecycle", {}).get("expiry_at"))
        if exp and exp.date() > now.date():
            add(exp.strftime("%Y-%m-%d"), f"{b.get('ticker')} 指値失効", "order", b.get("ticker"))

    # 通貨ポリシー期限
    if currency.get("valid_until"):
        add(currency["valid_until"], f"通貨ポリシー期限（USD {currency.get('usd_target_pct')}%）", "policy")

    # 妻NISA 週次積立（毎週月曜）
    wife = (nisa.get("wife") or {}).get("tsumitate_schedule") or {}
    if wife.get("type") == "weekly":
        amt = wife.get("amount_per_week")
        d = now + timedelta(days=(7 - now.weekday()) % 7 or 7)  # 次の月曜
        for _ in range(4):
            add(d.strftime("%Y-%m-%d"), f"妻NISA積立 ¥{amt:,.0f}" if amt else "妻NISA積立", "nisa")
            d += timedelta(days=7)

    # 月初リマインド（cron: クレカ積立 毎月1日 / 損出しスキャン 1・4・7・10月1日）
    for i in range(1, 4):
        m = now.month + i
        y = now.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        first = f"{y:04d}-{m:02d}-01"
        add(first, "クレカ積立リマインド", "reminder")
        if m in (1, 4, 7, 10):
            add(first, "損出しスキャン（四半期）", "reminder")

    upcoming.sort(key=lambda e: e["date"])

    notes = []
    husband = (nisa.get("husband") or {}).get("tsumitate_schedule") or {}
    if husband.get("type") == "daily" and husband.get("amount_per_day"):
        notes.append(f"本人NISA: 毎営業日 ¥{husband['amount_per_day']:,.0f} 積立（{husband.get('fund', '')}）")

    # ── 過去（トレード実績 + 日次損益）: カレンダーを取引ジャーナル化 ──
    past_cutoff = now - timedelta(days=45)
    past: list[dict] = []
    seen_trades = set()
    executions = _load("action_executions.json").get("executions") or []
    for e in executions:
        if e.get("status") not in ("executed", "filled", "partial"):
            continue
        raw = e.get("filled_at") or e.get("saved_at")
        d = _parse_dt(raw)
        if not d or d < past_cutoff or d.date() > now.date():
            continue
        direction = (e.get("direction") or "").lower()
        side = "sell" if direction in ("sell", "short") else "buy"
        key = (d.strftime("%Y-%m-%d"), e.get("ticker"), side)
        if key in seen_trades:
            continue
        seen_trades.add(key)
        past.append({
            "date": d.strftime("%Y-%m-%d"),
            "kind": "trade",
            "ticker": e.get("ticker"),
            "side": side,
            "detail": e.get("action") or e.get("action_detail") or "",
        })

    # 日次損益（guard pnl_history）→ {date: pnl_jpy}
    pnl_by_date: dict[str, float] = {}
    for row in guard.get("pnl_history") or []:
        dt = _parse_dt(row.get("date"))
        if dt and dt >= past_cutoff:
            pnl_by_date[dt.strftime("%Y-%m-%d")] = row.get("pnl_jpy") or 0

    return {
        "today": today_events,
        "sessions": _market_sessions(now),
        "upcoming": upcoming,
        "past": past,
        "pnl_by_date": pnl_by_date,
        "notes": notes,
        "is_weekday": is_weekday,
        "today_str": now.strftime("%Y-%m-%d"),
    }


def _build_delta(analysis: dict, board: list[dict]) -> dict | None:
    """ai_analysis_history.json と現在の分析を突合して変化を返す。"""
    hist = _load("ai_analysis_history.json")
    items = hist if isinstance(hist, list) else hist.get("history") or []
    if not items:
        return None
    cur_as_of = analysis.get("as_of")
    prev = None
    for e in reversed(items):
        if e.get("as_of") != cur_as_of:
            prev = e
            break
    if not prev:
        return None

    cur_set = {(b.get("ticker"), b.get("type")) for b in board}
    prev_actions = prev.get("priority_actions") or []
    prev_set = {(a.get("ticker"), a.get("type")) for a in prev_actions}

    def fmt(pairs):
        return [{"ticker": t, "type": ty} for t, ty in sorted(pairs, key=lambda x: str(x[0]))]

    synthesis = analysis.get("synthesis") or {}
    return {
        "prev_as_of": prev.get("as_of"),
        "stance_prev": prev.get("overall_stance"),
        "stance_now": synthesis.get("overall_stance"),
        "added": fmt(cur_set - prev_set),
        "removed": fmt(prev_set - cur_set),
        "kept": fmt(cur_set & prev_set),
    }


def _execution_plan_reason_code(reason: str | None) -> str:
    if not reason:
        return "other"
    text = str(reason)
    for code in (
        "plan_consumed_by_open_order",
        "plan_wait_for_better_candidate",
        "plan_over_budget",
        "execution_plan_existing_guard",
        "plan_unmatched_no_override",
    ):
        if text.startswith(code) or code in text:
            return code
    if "already_executed" in text:
        return "already_executed"
    return "other"


def _fmt_plan_item_label(item: dict) -> str:
    obj = item.get("objective") or item.get("plan_item_id") or "plan item"
    labels = {
        "wife_nisa_growth_capacity": "妻NISA 成長枠",
        "add_currency_usd": "USD不足の補正",
    }
    if obj in labels:
        return labels[obj]
    if isinstance(obj, str) and obj.startswith("add_sector_"):
        return "セクター補正: " + obj.removeprefix("add_sector_").replace("-", " ")
    return str(obj).replace("_", " ")


def _normalize_no_action_rationale(value: object) -> list[dict[str, str]]:
    """Return a stable JSON shape for execution-plan rationale rows.

    The execution-plan engine emits ``{reason_code, message}`` objects, while
    older state files may still contain plain strings.  Normalizing both here
    keeps the Today API safe for mixed-version state during deployments.
    """
    if not isinstance(value, list):
        return []

    rows: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            reason_code = str(item.get("reason_code") or "other").strip() or "other"
            message = str(item.get("message") or item.get("reason") or reason_code).strip()
        elif item is not None:
            reason_code = "legacy"
            message = str(item).strip()
        else:
            continue
        if message:
            rows.append({"reason_code": reason_code, "message": message})
    return rows


def _build_order_intent_review(synthesis: dict) -> dict:
    """Expose deferred order intents as review-only rows for Today.

    These are deliberately separate from ``board``: an existing order may be
    kept or amended, but the row is not a new order and must never receive an
    execution control in the UI.
    """
    deferred = synthesis.get("order_intent_deferred_actions") or []
    items: list[dict] = []
    summary: dict[str, int] = {}
    labels = {
        "keep_existing_order": "既存注文を維持",
        "amend_existing_order": "既存注文の訂正を確認",
        "stale_order_requires_confirmation": "証券会社の注文状態を確認",
        "near_minimum_notional": "最小発注額付近・数量を確認",
    }
    if isinstance(deferred, list):
        for action in deferred[:10]:
            if not isinstance(action, dict):
                continue
            decision = str(action.get("order_intent_decision") or "review_existing_order")
            summary[decision] = summary.get(decision, 0) + 1
            items.append({
                "ticker": action.get("ticker"),
                "type": action.get("type"),
                "action": action.get("action"),
                "decision": decision,
                "label": labels.get(decision, decision.replace("_", " ")),
                "reason": action.get("non_executable_reason") or action.get("filtered_reason"),
                "existing_order_id": action.get("existing_order_id"),
                "existing_order_status": action.get("existing_order_status"),
                "existing_order_notional_jpy": action.get("existing_order_notional_jpy"),
                "recommended_notional_jpy": action.get("recommended_notional_jpy"),
                "incremental_notional_jpy": action.get("incremental_notional_jpy"),
                "material_change": bool(action.get("material_change")),
                "non_executable": True,
            })
    return {"count": len(items), "summary": summary, "items": items}


def _build_execution_plan_view(plan: dict, board: list[dict], synthesis: dict, now: datetime) -> dict:
    order_intent_review = _build_order_intent_review(synthesis)
    post_filter = synthesis.get("post_filter") if isinstance(synthesis, dict) else {}
    gate_observation = (
        post_filter.get("execution_plan_gate")
        if isinstance(post_filter, dict) and isinstance(post_filter.get("execution_plan_gate"), dict)
        else {}
    )
    if not isinstance(plan, dict) or not plan:
        return {
            "status": "missing",
            "as_of": None,
            "age_hours": None,
            "budgets": {},
            "consumption": {
                "normal_plan_budget_consumed_jpy": None,
                "normal_plan_budget_consumed_pct": None,
                "normal_matched_notional_jpy": None,
                "normal_open_order_matched_notional_jpy": None,
                "normal_filled_matched_notional_jpy": None,
                "opportunity_matched_notional_jpy": None,
                "monthly_attribution_incomplete": False,
                "unattributed_monthly_buy_total_notional_jpy": None,
                "unattributed_monthly_sell_total_notional_jpy": None,
                "unattributed_monthly_unpriced_count": 0,
            },
            "items": [],
            "summary": {"active_items": 0, "covered_items": 0, "items_total": 0, "board_count": len(board), "plan_filtered_count": 0},
            "today_decision": {
                "code": "missing",
                "label": "実行計画なし",
                "reason": "execution_plan_state.json が見つからないため、計画枠による説明は未表示です。",
            },
            "filtered_summary": {},
            "filtered_examples": [],
            "order_intent_review": order_intent_review,
            "gate_observation": gate_observation,
            "warnings": [],
            "no_action_rationale": [],
        }

    budgets = plan.get("budgets") or {}
    consumption = plan.get("consumption_summary") or {}
    raw_items = plan.get("items") or []
    items = []
    for item in raw_items[:8]:
        if not isinstance(item, dict):
            continue
        consumed_by = item.get("consumed_by") or []
        items.append({
            "plan_item_id": item.get("plan_item_id"),
            "label": _fmt_plan_item_label(item),
            "objective": item.get("objective"),
            "status": item.get("status"),
            "priority": item.get("priority"),
            "normal_budget_jpy": item.get("normal_budget_jpy"),
            "requested_jpy": item.get("requested_jpy"),
            "shared_pool_jpy": item.get("shared_pool_jpy"),
            "consumed_jpy": item.get("consumed_jpy"),
            "remaining_jpy": item.get("remaining_jpy"),
            "preferred_tickers": item.get("preferred_tickers") or [],
            "consumed_by_count": len(consumed_by) if isinstance(consumed_by, list) else 0,
            "source_reasons": (item.get("source_reasons") or [])[:3],
            "today_decision": item.get("today_decision") or {},
        })

    filtered_actions = synthesis.get("_filtered_actions") or []
    filtered_summary: dict[str, int] = {}
    filtered_examples = []
    if isinstance(filtered_actions, list):
        for action in filtered_actions:
            if not isinstance(action, dict):
                continue
            code = action.get("execution_plan_decision") or _execution_plan_reason_code(action.get("filtered_reason"))
            if code == "other" and not action.get("execution_plan_decision"):
                continue
            filtered_summary[code] = filtered_summary.get(code, 0) + 1
            if len(filtered_examples) < 5:
                filtered_examples.append({
                    "ticker": action.get("ticker"),
                    "type": action.get("type"),
                    "code": code,
                    "reason": action.get("filtered_reason"),
                    "plan_item_id": action.get("plan_item_id"),
                    # Preserve the original decision coordinates when they exist.
                    # Today can then plot rejected actions without inventing
                    # confidence or portfolio-impact values for qualitative lanes.
                    "confidence_pct": action.get("confidence_pct"),
                    "estimated_notional_jpy": action.get("estimated_notional_jpy"),
                })

    as_of = plan.get("as_of")
    as_of_dt = _parse_dt(as_of)
    age_hours = round((now - as_of_dt).total_seconds() / 3600, 1) if as_of_dt else None
    active_items = sum(1 for item in raw_items if isinstance(item, dict) and item.get("status") == "active")
    covered_items = sum(1 for item in raw_items if isinstance(item, dict) and item.get("status") == "covered")
    remaining_normal = consumption.get("remaining_normal_jpy")
    remaining_opp = consumption.get("remaining_opportunity_jpy")
    open_consumed = consumption.get("open_order_consumed_jpy")
    filled_consumed = consumption.get("filled_consumed_jpy")
    warnings = plan.get("warnings") or []

    def _float_value(value: object) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    # Normal plan objectives now share one contribution-backed pool.  Summing
    # per-item consumption would count one real buy multiple times when it
    # matches several allocation objectives, so use the monthly attribution
    # ledger in that mode instead of the old item-wallet presentation.
    shared_normal_pool = budgets.get("normal_pool_available_jpy") is not None
    normal_plan_budget_consumed = 0.0
    normal_matched_notional = 0.0
    normal_open_notional = 0.0
    normal_filled_notional = 0.0
    opportunity_matched_notional = 0.0
    if shared_normal_pool:
        normal_objective_ids = {
            str(item.get("monthly_objective_id"))
            for item in raw_items
            if isinstance(item, dict)
            and str(item.get("budget_bucket") or "normal").strip().lower() == "normal"
            and item.get("monthly_objective_id")
        }
        for record in consumption.get("monthly_consumed_by") or []:
            if not isinstance(record, dict) or str(record.get("monthly_objective_id") or "") not in normal_objective_ids:
                continue
            notional = _float_value(record.get("notional_jpy"))
            normal_plan_budget_consumed += notional
            normal_matched_notional += notional
            if record.get("consumption_type") == "filled":
                normal_filled_notional += notional
            else:
                normal_open_notional += notional
    else:
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            bucket = str(item.get("budget_bucket") or "normal").strip().lower()
            consumed_jpy = _float_value(item.get("consumed_jpy"))
            if bucket == "opportunity":
                opportunity_matched_notional += consumed_jpy
                continue
            normal_plan_budget_consumed += min(
                consumed_jpy,
                _float_value(item.get("normal_budget_jpy")),
            )
            normal_matched_notional += consumed_jpy
            normal_open_notional += _float_value(item.get("open_order_consumed_jpy"))
            normal_filled_notional += _float_value(item.get("filled_consumed_jpy"))

    weekly_normal = _float_value(budgets.get("weekly_normal_jpy"))
    normal_plan_budget_consumed_pct = (
        round(normal_plan_budget_consumed / weekly_normal * 100, 1)
        if weekly_normal > 0 and not shared_normal_pool
        else None
    )
    monthly_attribution_incomplete = _float_value(
        consumption.get("unattributed_monthly_total_count")
    ) > 0

    if board:
        today_decision = {
            "code": "actions_available",
            "label": f"今日の発注 {len(board)} 件",
            "reason": "実行計画と既存ガードを通過した発注候補があります。",
        }
    elif plan.get("status") == "disabled":
        today_decision = {
            "code": "disabled",
            "label": "計画レイヤー無効",
            "reason": "実行計画の生成に失敗したため、古い計画で抑止しないよう一時無効化されています。",
        }
    elif filtered_summary.get("plan_consumed_by_open_order"):
        today_decision = {
            "code": "wait_open_order",
            "label": "既存注文待ち",
            "reason": "今日の候補は、同じ計画枠を既存の発注中注文または今週約定がすでに消費しているため見送りです。",
        }
    elif covered_items and active_items == 0:
        today_decision = {
            "code": "covered",
            "label": "今週枠は消化済み",
            "reason": "週次計画の主要枠は既存注文または約定で消化済みです。",
        }
    elif active_items and not board:
        today_decision = {
            "code": "wait_candidate",
            "label": "候補待ち",
            "reason": "計画残枠はありますが、今日のAI候補は確信度・予算・既存ガードを通過していません。",
        }
    elif warnings:
        today_decision = {
            "code": "warning",
            "label": "計画に警告あり",
            "reason": "実行計画に警告があるため、予算判断は保守的に扱われています。",
        }
    else:
        today_decision = {
            "code": "no_action",
            "label": "今日の発注 0 件",
            "reason": "実行計画上も最終発注に進む候補がありません。",
        }

    return {
        "status": plan.get("status") or "unknown",
        "as_of": as_of,
        "age_hours": age_hours,
        "horizon": plan.get("horizon") or {},
        "budgets": {
            "monthly_total_jpy": budgets.get("monthly_total_jpy"),
            "monthly_remaining_jpy": budgets.get("monthly_remaining_jpy"),
            "monthly_discretionary_budget_jpy": budgets.get("monthly_discretionary_budget_jpy"),
            "monthly_base_consumed_jpy": budgets.get("monthly_base_consumed_jpy"),
            "monthly_base_remaining_jpy": budgets.get("monthly_base_remaining_jpy"),
            "approved_contribution_released_this_month_jpy": budgets.get("approved_contribution_released_this_month_jpy"),
            "normal_pool_available_jpy": budgets.get("normal_pool_available_jpy"),
            "opportunity_pool_available_jpy": budgets.get("opportunity_pool_available_jpy"),
            "weekly_normal_jpy": budgets.get("weekly_normal_jpy"),
            "weekly_opportunity_reserve_jpy": budgets.get("weekly_opportunity_reserve_jpy"),
            "weekly_defensive_reserve_jpy": budgets.get("weekly_defensive_reserve_jpy"),
            "max_single_normal_action_jpy": budgets.get("max_single_normal_action_jpy"),
            "max_single_opportunity_action_jpy": budgets.get("max_single_opportunity_action_jpy"),
            "h2_hard_cap_jpy": budgets.get("h2_hard_cap_jpy"),
            "budget_source": budgets.get("budget_source"),
            "scheduled_contributions_remaining_jpy": budgets.get("scheduled_contributions_remaining_jpy"),
        },
        "consumption": {
            "normal_consumed_jpy": consumption.get("normal_consumed_jpy"),
            "open_order_consumed_jpy": open_consumed,
            "filled_consumed_jpy": filled_consumed,
            "monthly_open_order_consumed_jpy": consumption.get("monthly_open_order_consumed_jpy"),
            "monthly_filled_consumed_jpy": consumption.get("monthly_filled_consumed_jpy"),
            "monthly_consumed_jpy": consumption.get("monthly_consumed_jpy"),
            "monthly_remaining_jpy": consumption.get("monthly_remaining_jpy"),
            "unattributed_monthly_open_order_count": consumption.get("unattributed_monthly_open_order_count"),
            "unattributed_monthly_open_order_notional_jpy": consumption.get("unattributed_monthly_open_order_notional_jpy"),
            "unattributed_monthly_filled_count": consumption.get("unattributed_monthly_filled_count"),
            "unattributed_monthly_filled_notional_jpy": consumption.get("unattributed_monthly_filled_notional_jpy"),
            "unattributed_monthly_total_count": consumption.get("unattributed_monthly_total_count"),
            "unattributed_monthly_total_notional_jpy": consumption.get("unattributed_monthly_total_notional_jpy"),
            "unattributed_monthly_buy_total_count": consumption.get("unattributed_monthly_buy_total_count"),
            "unattributed_monthly_buy_total_notional_jpy": consumption.get("unattributed_monthly_buy_total_notional_jpy"),
            "unattributed_monthly_sell_total_count": consumption.get("unattributed_monthly_sell_total_count"),
            "unattributed_monthly_sell_total_notional_jpy": consumption.get("unattributed_monthly_sell_total_notional_jpy"),
            "unattributed_monthly_unpriced_count": consumption.get("unattributed_monthly_unpriced_count"),
            "remaining_normal_jpy": remaining_normal,
            "remaining_opportunity_jpy": remaining_opp,
            "normal_plan_budget_consumed_jpy": normal_plan_budget_consumed,
            "normal_plan_budget_consumed_pct": normal_plan_budget_consumed_pct,
            "normal_matched_notional_jpy": normal_matched_notional,
            "normal_open_order_matched_notional_jpy": normal_open_notional,
            "normal_filled_matched_notional_jpy": normal_filled_notional,
            "opportunity_matched_notional_jpy": opportunity_matched_notional,
            "monthly_attribution_incomplete": monthly_attribution_incomplete,
        },
        "summary": {
            "items_total": len([i for i in raw_items if isinstance(i, dict)]),
            "active_items": active_items,
            "covered_items": covered_items,
            "board_count": len(board),
            "plan_filtered_count": sum(filtered_summary.values()),
        },
        "items": items,
        "contributions": {
            "approved_contribution_count": (plan.get("contribution_summary") or {}).get("approved_contribution_count", 0),
            "released_this_month_jpy": (plan.get("contribution_summary") or {}).get("released_this_month_jpy", 0),
            "available_jpy": (plan.get("contribution_summary") or {}).get("available_jpy", 0),
            "available_normal_jpy": (plan.get("contribution_summary") or {}).get("available_normal_jpy", 0),
            "available_opportunity_jpy": (plan.get("contribution_summary") or {}).get("available_opportunity_jpy", 0),
            "sources": (plan.get("contribution_summary") or {}).get("sources", []),
        },
        "today_decision": today_decision,
        "filtered_summary": filtered_summary,
        "filtered_examples": filtered_examples,
        "order_intent_review": order_intent_review,
        "gate_observation": gate_observation,
        "warnings": warnings,
        "no_action_rationale": _normalize_no_action_rationale(plan.get("no_action_rationale")),
    }


def _build_today() -> dict:
    analysis = _load("ai_portfolio_analysis.json")
    action_state = _load("action_state.json").get("actions", {})
    reliability = _load("agent_reliability.json")
    macro = _load("macro_state.json")
    currency = _load("currency_policy_state.json")
    guard = _load("guard_state.json")
    nisa = _load("nisa_portfolio.json")
    execution_log = _load("action_executions.json")
    execution_plan_state = _load("execution_plan_state.json")
    scenario_state = _load("scenario_state.json")

    # Build one coherent live snapshot for all Today consumers.  Components
    # must not independently refetch portfolio/risk data with a different as-of.
    try:
        from portfolio_manager import build_portfolio_snapshot
        portfolio_snapshot = build_portfolio_snapshot()
    except Exception as exc:
        portfolio_snapshot = {"error": str(exc), "positions": [], "total_jpy": 0}
    try:
        from api.routes.dashboard import _build_data_health
        data_health = _build_data_health()
    except Exception:
        data_health = {"ok": False, "sources": {}, "stale_sources": [], "missing_sources": []}

    synthesis = analysis.get("synthesis") or {}
    long_a = analysis.get("long_analysis") or {}
    medium_a = analysis.get("medium_analysis") or {}
    margin_a = analysis.get("margin_long_analysis") or {}
    short_sell_a = analysis.get("short_selling_analysis") or {}
    short_pos_a = analysis.get("short_positions_analysis") or {}
    redteam = analysis.get("redteam") or {}

    portfolio_total = portfolio_snapshot.get("total_jpy") or analysis.get("portfolio_total") or guard.get("portfolio_value") or 0
    as_of_str = analysis.get("as_of")
    as_of_dt = _parse_dt(as_of_str)
    now = datetime.now()
    data_age_hours = (
        round((now - as_of_dt).total_seconds() / 3600, 1) if as_of_dt else None
    )
    if portfolio_snapshot.get("error"):
        system_status = "degraded"
    elif not data_health.get("ok"):
        system_status = "data_stale"
    elif data_age_hours is None or data_age_hours > 24:
        system_status = "analysis_old"
    else:
        system_status = "healthy"

    # ── 発注ボード ──
    board = []
    review_board = []
    actions = sorted(
        synthesis.get("priority_actions") or [],
        key=lambda a: a.get("rank", 99),
    )

    def _board_row(a: dict, *, force_review: bool = False) -> dict:
        entry = _match_lifecycle(a, action_state, as_of_dt)
        notional = a.get("estimated_notional_jpy")
        impact = (
            round(notional / portfolio_total * 100, 2)
            if notional and portfolio_total else None
        )
        readiness = str(a.get("execution_readiness") or "").strip().lower()
        if force_review or not readiness:
            readiness = "review"
        block_reasons = [r for r in (a.get("execution_block_reasons") or []) if isinstance(r, dict)]
        if force_review and not block_reasons:
            message = a.get("non_executable_reason") or a.get("filtered_reason") or "実行前の確認が必要です"
            block_reasons = [{
                "code": a.get("filter_rule") or a.get("order_intent_decision") or "review_required",
                "message": message,
            }]
        lifecycle = _lifecycle_view(entry, a.get("expiry_minutes"))
        if lifecycle.get("status") == "expired" and readiness == "ready":
            readiness = "review"
            block_reasons.append({
                "code": "order_expired",
                "message": "推奨時の有効期限を過ぎています。新しい価格で再分析が必要です",
            })
        if lifecycle.get("status") == "reprice_required" and readiness == "ready":
            readiness = "review"
            block_reasons.append({
                "code": "market_closed_reprice_required",
                "message": "休場日に生成された候補です。次の取引セッションで再分析・再価格設定が必要です",
            })
        return {
            **{k: a.get(k) for k in (
                "rank", "source_rank", "display_rank", "tier", "ticker", "type", "urgency", "action", "reason",
                "amount_hint", "confidence_pct", "order_type", "limit_price",
                "decision_price", "execution_reason", "execution_note",
                "expiry_minutes", "target_5d_pct", "target_20d_pct",
                "cooldown_warning", "return_20d_rank", "plan_item_id", "monthly_objective_id",
                "execution_plan_decision", "execution_plan_override",
                "execution_plan_observed_decision", "execution_plan_would_filter",
                "plan_remaining_before_jpy", "plan_remaining_after_jpy",
                "override_reason", "budget_impact_jpy", "ai_bounded_gate",
                "order_intent_decision", "filter_rule", "minimum_executable_quantity",
                "execution_owner", "execution_broker", "execution_account",
                "execution_investment_type", "execution_position_keys",
                "execution_advisories", "market_quote_confirmation_required",
                "market_order_window", "expiry_starts_at", "expiry_ends_at",
                "market_reprice_required", "market_reprice_after",
            )},
            "analysis_id": a.get("analysis_id") or synthesis.get("analysis_id"),
            "action_state_id": lifecycle.get("id"),
            "execution_readiness": readiness,
            "execution_block_reasons": block_reasons,
            "estimated_notional_jpy": notional,
            "impact_nav_pct": impact,
            "lifecycle": lifecycle,
        }

    for a in actions:
        if not isinstance(a, dict):
            continue
        row = _board_row(a)
        if row["execution_readiness"] == "ready" and not a.get("non_executable"):
            board.append(row)
        else:
            review_board.append(row)

    for a in synthesis.get("order_intent_deferred_actions") or []:
        if isinstance(a, dict):
            review_board.append(_board_row(a, force_review=True))

    # 積み残し: 直近分析の board に載らなくなった古い pending（提案されたが未処理のまま埋もれたもの）
    try:
        from action_state_tracker import dedup_key_for_action
        board_keys = {dedup_key_for_action(b) for b in board + review_board}
    except Exception:
        board_keys = {(b.get("ticker"), b.get("type")) for b in board + review_board}
    stale_cutoff = now - timedelta(hours=20)
    backlog = []
    for v in action_state.values():
        if v.get("status") != "pending":
            continue
        rec_dt = _parse_dt(v.get("recommended_at"))
        if not rec_dt or rec_dt > stale_cutoff:
            continue
        try:
            key = dedup_key_for_action(v)
        except Exception:
            key = (v.get("ticker"), v.get("action_type"))
        if key in board_keys:
            continue
        from execution_safety import execution_expiry_at

        expiry_at = execution_expiry_at(v)
        is_expired = (
            expiry_at is not None
            and expiry_at <= datetime.now(expiry_at.tzinfo)
        )
        backlog_row = {
            "rank": None,
            "tier": None,
            "ticker": v.get("ticker"),
            "type": v.get("action_type"),
            "urgency": v.get("urgency"),
            "action": v.get("action_detail"),
            "reason": v.get("reason"),
            "amount_hint": v.get("amount_hint"),
            "confidence_pct": None,
            "order_type": v.get("order_type"),
            "limit_price": v.get("limit_price"),
            "decision_price": v.get("decision_price"),
            "execution_reason": v.get("execution_reason"),
            "analysis_id": v.get("analysis_id"),
            "action_state_id": v.get("id"),
            "execution_readiness": "review",
            "execution_block_reasons": [
                row for row in (v.get("execution_block_reasons") or []) if isinstance(row, dict)
            ],
            "execution_plan_observed_decision": v.get("execution_plan_observed_decision"),
            "execution_plan_would_filter": bool(v.get("execution_plan_would_filter")),
            "execution_owner": v.get("execution_owner"),
            "execution_broker": v.get("execution_broker"),
            "execution_account": v.get("execution_account"),
            "execution_investment_type": v.get("execution_investment_type"),
            "execution_position_keys": v.get("execution_position_keys") or [],
            "historical_backlog": True,
            "days_pending": (now.date() - rec_dt.date()).days,
            "lifecycle": {
                "id": v.get("id"),
                "status": "expired" if is_expired else "review",
                "recommended_at": v.get("recommended_at"),
                "placed_at": v.get("placed_at"),
                "filled_at": None,
                "expiry_at": expiry_at.isoformat() if expiry_at else None,
                "expiry_starts_at": v.get("expiry_starts_at"),
                "expiry_ends_at": v.get("expiry_ends_at"),
                "note": v.get("note"),
            },
        }
        if not backlog_row["execution_block_reasons"]:
            backlog_row["execution_block_reasons"] = [{
                "code": "historical_backlog_expired" if is_expired else "historical_readiness_unknown",
                "message": (
                    "提案期限を過ぎた履歴です。新規注文には使えません"
                    if is_expired else
                    "期限情報のない旧候補です。新規注文には使えません"
                ),
            }]
        backlog.append(backlog_row)
    backlog.sort(key=lambda b: b["lifecycle"]["recommended_at"] or "")

    # やらないことの説明（実施しなかったレーンの理由）
    board_notes = []
    if snr := (synthesis.get("short_not_recommended") or short_sell_a.get("short_not_recommended")):
        board_notes.append({"label": "空売り", "text": snr})
    if ntr := margin_a.get("no_trade_rationale"):
        board_notes.append({"label": "信用買い", "text": ntr})

    # ── エンジンルーム ──
    tier_candidates = sum(len(x.get("priority_actions") or []) for x in (
        long_a, medium_a, margin_a, short_sell_a, short_pos_a))
    funnel = [
        {"key": "tiers", "label": "ティア候補", "count": tier_candidates,
         "note": f"Long {len(long_a.get('priority_actions') or [])} / Medium {len(medium_a.get('priority_actions') or [])} / 信用・空売り {tier_candidates - len(long_a.get('priority_actions') or []) - len(medium_a.get('priority_actions') or [])}"},
        {"key": "redteam", "label": "Red Team 攻撃案", "count": len(redteam.get("attacks") or [])},
        {"key": "lanes", "label": "情報レーン", "count": len(synthesis.get("information_lane_verdicts") or [])},
        {"key": "final", "label": "Opus 最終", "count": len(actions), "hot": True},
        {"key": "orders", "label": "指値推奨", "count": sum(1 for a in actions if a.get("limit_price"))},
    ]
    engine = {
        "funnel": funnel,
        "stance_reason": synthesis.get("stance_reason") or long_a.get("stance_reason"),
        "operational_stance": synthesis.get("operational_stance") or {},
        "red_team": synthesis.get("red_team_verdict") or [],
        "lanes": synthesis.get("information_lane_verdicts") or [],
        "attacks": redteam.get("attacks") or [],
        "underutilized": redteam.get("underutilized") or [],
    }

    # ── アナリストレポート ──
    report = {
        "long": _tier_report(long_a),
        "medium": _tier_report(medium_a),
        "margin_long": _tier_report(margin_a),
        "short_selling": _tier_report(short_sell_a),
        "short_positions": _tier_report(short_pos_a),
        "synthesis": _tier_report(synthesis),
    }

    # ── 成績表 ──
    score_rows = []
    for agent, roles in (reliability.get("agents") or {}).items():
        for role, m in (roles or {}).items():
            score_rows.append({
                "agent": agent, "role": role,
                "n": m.get("n"), "measured_n": m.get("measured_n"),
                "win_rate": m.get("win_rate"),
                "excess_bps": m.get("mean_excess_return_bps"),
                "payoff": m.get("payoff_ratio"),
                "measured": m.get("measured"),
            })
    status_counts: dict[str, int] = {}
    for v in action_state.values():
        s = v.get("status") or "unknown"
        status_counts[s] = status_counts.get(s, 0) + 1
    fills = sorted(
        (v for v in action_state.values() if v.get("status") == "filled"),
        key=lambda v: v.get("filled_at") or "", reverse=True,
    )[:5]
    recent_fills = [{
        "ticker": v.get("ticker"), "action_type": v.get("action_type"),
        "detail": v.get("action_detail"), "filled_at": v.get("filled_at"),
        "limit_price": v.get("limit_price"),
    } for v in fills]
    scorecard = {
        "as_of": reliability.get("as_of"),
        "horizon_days": reliability.get("horizon_days"),
        "rows": score_rows,
        "status_counts": status_counts,
        "recent_fills": recent_fills,
    }

    # ── 配分レーダー ──
    breakdown = analysis.get("currency_breakdown") or {}
    usd_ratio = (breakdown.get("USD") or {}).get("ratio")

    def _nisa_view(owner: str, p: dict) -> dict:
        broker = "rakuten" if owner == "husband" else "sbi"

        def effective(account_name: str) -> dict:
            try:
                from execution_safety import evaluate_nisa_capacity
                return evaluate_nisa_capacity({
                    "type": "buy",
                    "execution_account": account_name,
                    "execution_owner": owner,
                    "execution_broker": broker,
                    "estimated_notional_jpy": 1,
                }, base_dir=BASE_DIR, now=now.replace(tzinfo=ZoneInfo("Asia/Tokyo")))
            except Exception as exc:
                return {
                    "readiness": "review",
                    "reasons": [{"code": "nisa_capacity_unresolved", "message": str(exc)}],
                }

        growth = effective("NISA成長投資枠")
        tsumitate = effective("NISAつみたて投資枠")
        reasons = growth.get("reasons") or []
        unattributed_ids = []
        age_days = None
        for reason in reasons:
            if reason.get("code") == "nisa_capacity_unattributed_activity":
                unattributed_ids = reason.get("execution_ids") or []
            if reason.get("code") == "nisa_capacity_stale":
                age_days = reason.get("age_days")
        return {
            "broker": p.get("broker"),
            "growth_remaining": growth.get("nisa_capacity_remaining_jpy", 0),
            "tsumitate_remaining": tsumitate.get("nisa_capacity_remaining_jpy", 0),
            "baseline": growth.get("nisa_capacity_baseline") or nisa.get("last_updated"),
            "age_days": age_days,
            "unattributed_count": len(unattributed_ids),
            "unattributed_execution_ids": unattributed_ids,
            "growth_readiness": growth.get("readiness"),
            "tsumitate_readiness": tsumitate.get("readiness"),
        }

    ginn = long_a.get("ginn_vol") or {}
    allocation = {
        "currency": {
            "current_usd_pct": round(usd_ratio * 100, 1) if usd_ratio else None,
            "usd_target_pct": currency.get("usd_target_pct"),
            "jpy_target_pct": currency.get("jpy_target_pct"),
            "confidence_pct": currency.get("confidence_pct"),
            "valid_until": currency.get("valid_until"),
            "reason": currency.get("reason"),
            "review_triggers": currency.get("review_triggers") or [],
            "risk_notes": currency.get("risk_notes"),
        },
        "nisa": {
            "husband": _nisa_view("husband", nisa.get("husband") or {}),
            "wife": _nisa_view("wife", nisa.get("wife") or {}),
        },
        "risk_warnings": synthesis.get("risk_warnings") or long_a.get("risk_warnings") or [],
        "stop_loss_alerts": synthesis.get("stop_loss_alerts") or long_a.get("stop_loss_alerts") or [],
        "ginn_vol": ginn,
        "margin_health": synthesis.get("margin_health") or long_a.get("margin_health"),
        "margin_summary": synthesis.get("margin_summary") or long_a.get("margin_summary"),
    }

    # ── コマンドバー ──
    focus = board[0] if board else None
    command = {
        "scenario": analysis.get("scenario_key"),
        "stance": synthesis.get("overall_stance") or long_a.get("overall_stance"),
        "operational_stance": synthesis.get("operational_stance") or {},
        "health": synthesis.get("health") or long_a.get("health"),
        "vix": macro.get("vix"),
        "vix_status": macro.get("vix_status"),
        "yield_10y": macro.get("yield_10y"),
        "fear_greed": macro.get("fear_greed"),
        "guard": {
            "new_entry_allowed": guard.get("new_entry_allowed"),
            "trading_allowed": guard.get("trading_allowed"),
            "alerts": guard.get("alerts") or [],
            "daily_pnl_pct": guard.get("daily_pnl_pct"),
            "monthly_pnl_pct": guard.get("monthly_pnl_pct"),
        },
        "usd_ratio_pct": round(usd_ratio * 100, 1) if usd_ratio else None,
        "usd_target_pct": currency.get("usd_target_pct"),
        "data_age_hours": data_age_hours,
    }

    # ── v7: charts / almanac / delta ──
    pnl_hist = guard.get("pnl_history") or []
    cum = 0.0
    pnl_series = []
    for row in pnl_hist:
        cum += row.get("pnl_jpy") or 0
        pnl_series.append({"d": (row.get("date") or "")[5:], "v": round(cum)})

    ticker_series = {}
    for b in board + review_board:
        t = b.get("ticker")
        if t and t not in ticker_series:
            s = _ticker_closes(t)
            if s:
                ticker_series[t] = s

    # 保有全銘柄の 30 日終値（PORTFOLIO 行展開用）
    holdings_raw = _load("holdings.json")
    position_tickers: list[str] = []
    if isinstance(holdings_raw, dict):
        for v in holdings_raw.values():
            t = v.get("ticker") if isinstance(v, dict) else None
            if t and t not in position_tickers:
                position_tickers.append(t)
    holdings_series = {}
    for t in position_tickers:
        s = _ticker_closes(t, days=30)
        if s:
            holdings_series[t] = s

    charts = {"pnl": pnl_series, "tickers": ticker_series, "holdings": holdings_series}
    holdings_intel = _build_holdings_intel(long_a, medium_a, synthesis)
    almanac = _build_almanac(board, analysis, currency, nisa, now, guard)
    delta = _build_delta(analysis, board)
    benchmark = _build_benchmark(guard)
    execution_plan = _build_execution_plan_view(execution_plan_state, board, synthesis, now)
    scenario_summary = build_scenario_summary(scenario_state)
    pending_portfolio_applications = [
        {
            "id": row.get("id"),
            "ticker": row.get("ticker"),
            "direction": row.get("direction"),
            "quantity": row.get("quantity"),
            "price": row.get("price"),
            "account": row.get("account"),
            "investment_type": row.get("investment_type"),
            "execution_owner": row.get("execution_owner"),
            "execution_broker": row.get("execution_broker"),
            "saved_at": row.get("saved_at"),
            "reasons": row.get("portfolio_application_reasons") or [],
            "candidate_position_keys": row.get("candidate_position_keys") or [],
        }
        for row in (execution_log.get("executions") or [])
        if isinstance(row, dict)
        and (
            row.get("portfolio_application_status") == "pending"
            or row.get("portfolio_application_pending") is True
        )
    ]
    cash_status = []
    if isinstance(holdings_raw, dict):
        for cash_key in ("CASH_JPY_SBI", "CASH_JPY_SBI_WIFE"):
            row = holdings_raw.get(cash_key)
            if not isinstance(row, dict):
                continue
            is_wife = cash_key.endswith("_WIFE")
            default_status = "estimated" if is_wife else "confirmed"
            cash_status.append({
                "key": cash_key,
                "owner": "wife" if is_wife else "husband",
                "broker": "sbi",
                "currency": "JPY",
                "effective_balance": row.get("shares"),
                "reported_balance": row.get("reported_balance_jpy", row.get("shares")),
                "reported_as_of": row.get("reported_as_of", "2026-05-12" if is_wife else None),
                "ledger_delta_since_report": row.get("ledger_delta_since_report_jpy", 0),
                "balance_status": row.get("balance_status", default_status),
                "reconciliation_required": bool(row.get("reconciliation_required", is_wife)),
                "available_for_new_buy": (
                    row.get("shares")
                    if row.get("balance_status", default_status) == "confirmed" else 0
                ),
            })

    return {
        "as_of": as_of_str,
        "generated_at": now.isoformat(timespec="seconds"),
        "portfolio_total": portfolio_total,
        "portfolio_snapshot": portfolio_snapshot,
        "snapshot_meta": {
            "snapshot_id": hashlib.sha256(
                f"{as_of_str}|{portfolio_snapshot.get('as_of')}|{now.isoformat(timespec='seconds')}".encode()
            ).hexdigest()[:16],
            "analysis_as_of": as_of_str,
            "portfolio_as_of": portfolio_snapshot.get("as_of"),
            "generated_at": now.isoformat(timespec="seconds"),
            "status": system_status,
            "data_health": data_health,
        },
        "command": command,
        "focus": focus,
        "board": board,
        "review_board": review_board,
        "decision_summary": synthesis.get("decision_summary") or {
            "candidate_count": (
                len(actions)
                + len(synthesis.get("_filtered_actions") or [])
                + len(synthesis.get("order_intent_deferred_actions") or [])
            ),
            "executable_count": len(board),
            "review_count": len(review_board),
            "filtered_count": len(synthesis.get("_filtered_actions") or []),
            "deferred_count": len(synthesis.get("order_intent_deferred_actions") or []),
            "no_action_classification": "system_constraints" if actions and not board else None,
            "reason_counts": {},
            "count_conservation_ok": None,
        },
        "board_notes": board_notes,
        "backlog": backlog,
        "pending_portfolio_applications": pending_portfolio_applications,
        "cash_status": cash_status,
        "engine": engine,
        "report": report,
        "scorecard": scorecard,
        "allocation": allocation,
        "charts": charts,
        "almanac": almanac,
        "delta": delta,
        "benchmark": benchmark,
        "execution_plan": execution_plan,
        "scenario_summary": scenario_summary,
        "holdings_intel": holdings_intel,
        "pulse": {"vix": macro.get("vix")},
    }


@router.get("/api/today")
async def get_today():
    return await asyncio.to_thread(_build_today)

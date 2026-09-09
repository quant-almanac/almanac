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

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from freshness_policy import stale_after_hours
from pseudo_tickers import is_non_earnings_ticker
from utils import LockBusy, heartbeat, positive_finite as _positive_finite

BASE_DIR = Path(__file__).parent
OUTPUT   = BASE_DIR / "earnings_hedge_suggestions.json"
HOLDINGS = BASE_DIR / "holdings.json"
ANALYSIS = BASE_DIR / "ai_portfolio_analysis.json"
GUARD_STATE = BASE_DIR / "guard_state.json"
ACCOUNT = BASE_DIR / "account.json"
EARNINGS_OVERRIDES = BASE_DIR / "earnings_calendar_overrides.json"
# 起動区分（scheduled/manual）ごとの実行を追記のみで記録する。
# --scheduled だけが watchdog 可視の heartbeat を更新するため、手動実行や
# analyst の self-heal 経由の scan() はそこには現れない。それでも
# 「今日なぜ2回走ったか」を追える durable な記録として残す（2026-09 レビュー S0）。
RUN_HISTORY_PATH = BASE_DIR / "earnings_proximity_run_history.jsonl"

PROX_DAYS              = 10    # 決算 10 営業日前から監視 (IV 膨張は 5-7 日前で顕在化だが、AAPL 等 8bd も救済)
IMPL_MOVE_FUDGE        = 0.85  # ATM straddle → implied move 係数 (Bachelier/近似)
DAMAGE_PCT_THRESHOLD   = 0.015 # total_portfolio の 1.5% で hedge 推奨
BEAT_RATE_FORCE_TRIM   = 0.50
YFIN_RETRY_ATTEMPTS    = 3     # yfinance .calendar の intermittent 404/rate-limit 対策リトライ
OUTPUT_SCHEMA_VERSION  = 4
PORTFOLIO_INPUT_MAX_AGE_HOURS = 24.0  # hourly guard producer; prior formal analysis is fallback
FX_INPUT_MAX_AGE_HOURS = stale_after_hours("fx")
FUTURE_TOLERANCE_HOURS = 1.0


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
        raise FileNotFoundError(f"holdings source is missing: {HOLDINGS}")
    try:
        h = json.loads(HOLDINGS.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"holdings source is unreadable: {exc}") from exc
    if isinstance(h, dict) and "positions" in h:
        raw_positions = h.get("positions")
        if isinstance(raw_positions, dict):
            rows = list(raw_positions.values())
        elif isinstance(raw_positions, list):
            rows = raw_positions
        else:
            raise ValueError("holdings positions must be a list or object")
    elif isinstance(h, dict):
        rows = list(h.values())
    elif isinstance(h, list):
        rows = h
    else:
        raise ValueError("holdings source must be a list or object")
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
        if not math.isfinite(sh) or sh <= 0:
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


def _holdings_snapshot_sha256(holdings: list[dict]) -> str:
    """Hash the exact position inputs that drive earnings damage sizing.

    A same-day artifact is reusable only while ticker, aggregate shares, and
    currency still match.  A count alone cannot distinguish a replacement
    holding or a position-size change.
    """
    normalized = sorted(
        (
            str(row.get("ticker") or "").strip().upper(),
            float(row.get("shares")),
            str(row.get("currency") or "USD").strip().upper(),
        )
        for row in holdings
    )
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# この producer が読む naive timestamp (guard_state.last_updated /
# ai_portfolio_analysis.as_of 等) はいずれも JST を前提に書かれている。
# ホスト TZ 環境変数 (datetime.now().astimezone().tzinfo) で補完すると、同じ
# 文字列でも実行環境ごとに異なる絶対時刻へ解釈され、鮮度判定が環境依存になる
# （TZ=UTC で 9 時間ズレることを確認・2026-09 レビュー Codex 指摘）。
_NAIVE_TIMESTAMP_TZ = ZoneInfo("Asia/Tokyo")


def _aware_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(_NAIVE_TIMESTAMP_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_NAIVE_TIMESTAMP_TZ)
    return current.astimezone(timezone.utc)


def _parse_input_timestamp(value: object) -> datetime | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            epoch = float(value)
            if not math.isfinite(epoch) or epoch < 946_684_800:
                return None
            if epoch >= 10_000_000_000:
                epoch /= 1000.0
            if epoch < 946_684_800:
                return None
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OSError, OverflowError, TypeError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_NAIVE_TIMESTAMP_TZ)
    return parsed.astimezone(timezone.utc)


def _input_age_hours(value: object, *, now: datetime | None = None) -> float:
    parsed = _parse_input_timestamp(value)
    if parsed is None:
        raise ValueError("input timestamp is missing or invalid")
    age = (_aware_now(now) - parsed).total_seconds() / 3600.0
    if age < -FUTURE_TOLERANCE_HOURS:
        raise ValueError("input timestamp is future-dated")
    return max(0.0, age)


def _read_json_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"{path.name} is missing or unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain an object")
    return payload


def _portfolio_total_observation(*, now: datetime | None = None) -> dict:
    """Resolve the freshest verifiable portfolio denominator.

    Evaluates every candidate rather than returning on the first success.
    ``behavioral_guard.save_state`` stamps a shared ``last_updated`` on every
    save — including writers (position count updates, overrides, status
    display) that never touch ``portfolio_value`` — so that field cannot
    prove revaluation.  ``portfolio_value_as_of`` is a dedicated field that
    only a writer which actually recomputed the valuation sets.  Even so, an
    unrevalued-but-present guard_state must never outrank a genuinely newer
    formal_analysis value just because it happens to be checked first
    (2026-09 review).
    """
    failures: list[str] = []
    candidates = (
        (GUARD_STATE, "guard_state", "portfolio_value", "portfolio_value_as_of"),
        (ANALYSIS, "formal_analysis", "portfolio_total", "as_of"),
    )
    resolved: list[tuple[datetime, dict]] = []
    for path, source, value_key, timestamp_key in candidates:
        try:
            payload = _read_json_object(path)
            raw_value = payload.get(value_key)
            if isinstance(raw_value, dict):
                raw_value = raw_value.get("total_jpy") or raw_value.get("total")
            value_jpy = _positive_finite(raw_value, label=f"{source}.{value_key}")
            as_of = payload.get(timestamp_key)
            age_hours = _input_age_hours(as_of, now=now)
            if age_hours > PORTFOLIO_INPUT_MAX_AGE_HOURS:
                raise ValueError(
                    f"{source}.{timestamp_key} is stale ({age_hours:.1f}h)"
                )
            parsed_as_of = _parse_input_timestamp(as_of)
            if parsed_as_of is None:
                # _input_age_hours already required a parseable, non-future
                # timestamp above; this is unreachable unless that changes.
                raise ValueError(f"{source}.{timestamp_key} is unparseable")
            resolved.append((parsed_as_of, {
                "value_jpy": value_jpy,
                "source": source,
                "as_of": as_of,
            }))
        except ValueError as exc:
            failures.append(str(exc))
    if resolved:
        # Stable sort: a tie keeps candidates' declared order (guard_state first).
        resolved.sort(key=lambda pair: pair[0], reverse=True)
        return resolved[0][1]
    raise RuntimeError(
        "current portfolio total is unavailable; " + "; ".join(failures)
    )


def _fx_rate_observation(*, now: datetime | None = None) -> dict:
    """Require a recent observed FX rate; never turn 150 into market data."""
    from utils import get_fx_rate_observation

    observation = get_fx_rate_observation(account_json_path=ACCOUNT)
    if not isinstance(observation, dict):
        raise RuntimeError("FX observation is not an object")
    try:
        rate = _positive_finite(observation.get("rate"), label="fx.rate")
    except ValueError as exc:
        raise RuntimeError(f"FX observation rate is invalid: {exc}") from exc
    if not 50 < rate < 500:
        raise RuntimeError(f"FX observation is outside sanity range: {rate}")
    source = str(observation.get("source") or "unknown")
    if source not in {"live", "cache", "account_stale"}:
        raise RuntimeError(f"FX observation source is not usable: {source}")
    try:
        age_hours = _input_age_hours(observation.get("observed_at"), now=now)
    except ValueError as exc:
        raise RuntimeError(f"FX observation timestamp is invalid: {exc}") from exc
    if age_hours > FX_INPUT_MAX_AGE_HOURS:
        raise RuntimeError(f"FX observation is stale ({age_hours:.1f}h)")
    return {
        "rate": rate,
        "source": source,
        "observed_at": observation.get("observed_at"),
    }


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


def _read_and_validate_snapshot(
    *, today: date | None = None, now: datetime | None = None,
) -> dict | None:
    """OUTPUT を一度だけ読み、その場で検証し、同じ dict を返す（検証済みなら）。

    以前は ``snapshot_is_current()``（検証のみ、内部で1回読む）と、
    ``_load_current_snapshot()``/``format_for_prompt()``（それぞれ独自に
    もう一度読む）が分離していたため、**検証した内容と実際に返す/表示する
    内容が同じ瞬間の同じデータである保証が無かった**（TOCTOU: 検証と
    読み込みの間に別プロセスが再スキャンして書き換え得る）。

    加えて以下2点も未検証だった（fix_plan_v2.md S2 節）:
    - ``generated_at`` が未来日時でも拒否しない
      （時計ずれ・書き込みバグでの誤って新しい日付を弾けない）。
    - NAV/FX は「パース可能か」しか見ておらず、**消費時点での実年齢**
      （生成時に検証した鮮度がそのまま「今も新鮮」を意味しない）を
      再検証していなかった。生成直後は新鮮でも、同じ JST 日のうちに
      ``PORTFOLIO_INPUT_MAX_AGE_HOURS``/``FX_INPUT_MAX_AGE_HOURS`` を
      超えた NAV/FX がそのまま「有効」として使われ続け得た。

    ``snapshot_is_current()`` はこの関数の真偽値版の互換ラッパー、
    ``_load_current_snapshot()``/``format_for_prompt()`` はこの関数の
    戻り値をそのまま使う（追加の読み込みをしない）。
    """
    now = _aware_now(now)
    today = today or now.astimezone(_NAIVE_TIMESTAMP_TZ).date()
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        generated_raw = data.get("generated_at")
        # _input_age_hours は「パース不能」と「FUTURE_TOLERANCE_HOURS を
        # 超えた未来日時」の両方で ValueError を投げる（呼出元の生成時刻
        # 検証と同じ許容幅を再利用する）。
        _input_age_hours(generated_raw, now=now)
        generated = _parse_input_timestamp(generated_raw)
        if generated is None:
            return None
        if generated.astimezone(_NAIVE_TIMESTAMP_TZ).date() != today:
            return None
        if data.get("schema_version") != OUTPUT_SCHEMA_VERSION:
            return None
        if (
            data.get("portfolio_jpy_source") not in {"guard_state", "formal_analysis"}
            or data.get("fx_rate_source") not in {"live", "cache", "account_stale"}
        ):
            return None
        # 生成時点のパース可能性だけでなく、消費時点での実年齢を再検証する。
        if _input_age_hours(data.get("portfolio_jpy_as_of"), now=now) > PORTFOLIO_INPUT_MAX_AGE_HOURS:
            return None
        if _input_age_hours(data.get("fx_rate_usdjpy_as_of"), now=now) > FX_INPUT_MAX_AGE_HOURS:
            return None
        _positive_finite(data.get("portfolio_jpy"), label="snapshot.portfolio_jpy")
        fx_rate = _positive_finite(data.get("usd_jpy"), label="snapshot.usd_jpy")
        if not 50 < fx_rate < 500:
            return None
        holdings = _load_holdings()
        holdings_scanned = data.get("holdings_scanned")
        if (
            isinstance(holdings_scanned, bool)
            or not isinstance(holdings_scanned, int)
            or holdings_scanned != len(holdings)
            or data.get("holdings_snapshot_sha256") != _holdings_snapshot_sha256(holdings)
        ):
            return None
        # Consumers slice/iterate these arrays. Empty dict/None must not pass
        # validation as empty rows and subsequently break rendering.
        if not isinstance(data.get("suggestions"), list) or not isinstance(data.get("skipped"), list):
            return None
        result_rows = data["suggestions"] + data["skipped"]
        if not all(isinstance(row, dict) for row in result_rows):
            return None
        result_tickers = [
            str(row.get("ticker") or "").strip().upper()
            for row in result_rows
        ]
        expected_tickers = {
            str(row.get("ticker") or "").strip().upper()
            for row in holdings
        }
        if (
            any(not ticker for ticker in result_tickers)
            or len(result_tickers) != len(set(result_tickers))
            or set(result_tickers) != expected_tickers
        ):
            return None
        rows = {str(row.get("ticker") or "").upper(): row for row in result_rows}
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
                return None
        return data
    except Exception:
        return None


def snapshot_is_current(
    *, today: date | None = None, now: datetime | None = None,
) -> bool:
    """True only when today's snapshot covers the current holdings exactly.

    ``_read_and_validate_snapshot`` の真偽値版の互換ラッパー（S2）。
    """
    return _read_and_validate_snapshot(today=today, now=now) is not None


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
        calls = calls.copy()
        puts = puts.copy()
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
        beats = 0
        total = 0
        cols = set(eh.columns) if hasattr(eh, "columns") else set()
        act_col = "epsActual" if "epsActual" in cols else ("actual" if "actual" in cols else None)
        est_col = "epsEstimate" if "epsEstimate" in cols else ("estimate" if "estimate" in cols else None)
        if not act_col or not est_col:
            return None
        for _, row in eh.iterrows():
            try:
                a = float(row[act_col])
                e = float(row[est_col])
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


def _scan_once(dry_run: bool = False) -> dict:
    holdings = _load_holdings()
    portfolio_observation = _portfolio_total_observation()
    total_jpy = portfolio_observation["value_jpy"]
    print(f"[earnings] scanning {len(holdings)} US holdings, portfolio=¥{total_jpy:,.0f}")
    fx_observation = _fx_rate_observation()
    fx = fx_observation["rate"]

    suggestions: list[dict] = []
    skipped: list[dict] = []  # 観測性: なぜ hedge 対象から外されたかを記録
    for h in holdings:
        tk = h["ticker"]
        sh = h["shares"]
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
        "holdings_snapshot_sha256": _holdings_snapshot_sha256(holdings),
        "portfolio_jpy":     total_jpy,
        "portfolio_jpy_source": portfolio_observation["source"],
        "portfolio_jpy_as_of": portfolio_observation["as_of"],
        "usd_jpy":           round(fx, 2),
        "fx_rate_source":    fx_observation["source"],
        "fx_rate_usdjpy_as_of": fx_observation["observed_at"],
        "prox_days":         PROX_DAYS,
        "damage_threshold_pct": round(DAMAGE_PCT_THRESHOLD * 100, 2),
        "suggestion_count":  len(suggestions),
        "suggestions":       suggestions,
        "skipped":           skipped,  # 観測性: なぜ 0 suggestion かを Opus に伝えるため保持
    }
    if not dry_run:
        from utils import atomic_write_json
        atomic_write_json(OUTPUT, out)
        print(f"[earnings] wrote {OUTPUT.name}: {len(suggestions)} suggestions, {len(skipped)} skipped")
        if skipped:
            reason_counts: dict[str, int] = {}
            for s in skipped:
                reason_counts[s["reason"]] = reason_counts.get(s["reason"], 0) + 1
            print(f"[earnings] skip reasons: {reason_counts}")
    return out


def _load_current_snapshot(*, now: datetime | None = None) -> dict | None:
    """Load today's schema-valid, consumption-time-fresh snapshot, or
    ``None`` fail-closed. Reads OUTPUT exactly once (S2)."""
    return _read_and_validate_snapshot(now=now)


def _record_run_history(*, kind: str, status: str, reused: bool,
                         generated_at: str | None = None,
                         holdings_snapshot_sha256: str | None = None,
                         error: str | None = None) -> bool:
    """Append one row per non-dry-run scan() invocation, regardless of caller.

    Distinct from the ``earnings_proximity`` heartbeat: only a ``kind="scheduled"``
    (cron) call moves that watchdog-visible signal (see :func:`main`). A manual
    run or the analyst's self-heal call (``kind="manual"``, the default) never
    touches the heartbeat, so without this file those quieter invocations would
    leave no durable trace at all. Appending, not read-modify-write, so a
    failed write here can never corrupt an earlier row.

    Returns:
        ``True`` if the row was written, ``False`` on failure. This is an
        observational aid only — its failure must never abort ``scan()`` —
        but a silent ``None`` return left even the *scheduled* success path
        unable to tell watchdog that the audit trail itself had gone dark
        (2026-09 review, Codex round 2 #9).
    """
    row = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "status": status,
        "reused": reused,
        "generated_at": generated_at,
        "holdings_snapshot_sha256": holdings_snapshot_sha256,
        "error": error,
    }
    try:
        with open(RUN_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        # 観測用の補助記録であり、これ自体の失敗で scan() を止めない。
        print(f"[earnings] run history 記録失敗（継続）: {e}", file=sys.stderr)
        return False


def scan(dry_run: bool = False, *, reuse_current: bool = False, kind: str = "manual") -> dict:
    """Run one scan, serializing scheduled producers across processes.

    The legacy cron and the formal analysis LaunchAgent both start at 06:15.
    A write-only lock would still run the same network scan twice in sequence,
    so scheduled callers set ``reuse_current=True`` and the second caller
    rechecks the published snapshot *after* acquiring the shared lock.
    ``--force`` and programmatic callers can retain the historical refresh
    behavior by leaving ``reuse_current`` false.

    Args:
        kind: ``"scheduled"`` for the cron invocation, ``"manual"`` (default)
            for everything else — a manual CLI run or the analyst's self-heal
            call. Purely for :func:`_record_run_history`; never used for gating.
    """
    if dry_run:
        return _scan_once(dry_run=True)

    from utils import process_lock  # LockBusy is imported at module level

    try:
        with process_lock("earnings_proximity", timeout=300.0):
            if reuse_current:
                current = _load_current_snapshot()
                if current is not None:
                    print("[earnings] current snapshot already published; duplicate scan skipped")
                    recorded = _record_run_history(
                        kind=kind, status="ok", reused=True,
                        generated_at=current.get("generated_at"),
                        holdings_snapshot_sha256=current.get("holdings_snapshot_sha256"),
                    )
                    current["run_history_recorded"] = recorded
                    return current
            try:
                out = _scan_once(dry_run=False)
            except LockBusy:
                # _scan_once() does not itself take the "earnings_proximity"
                # lock today, so this branch is not currently reachable —
                # but if that ever changes, let the outer handler own the
                # single lock_busy record rather than double-recording here.
                raise
            except Exception as exc:
                _record_run_history(kind=kind, status="error", reused=False, error=str(exc)[:500])
                raise
            recorded = _record_run_history(
                kind=kind, status="ok", reused=False,
                generated_at=out.get("generated_at"),
                holdings_snapshot_sha256=out.get("holdings_snapshot_sha256"),
            )
            out["run_history_recorded"] = recorded
            return out
    except LockBusy:
        _record_run_history(kind=kind, status="lock_busy", reused=False)
        raise


def format_for_prompt(max_entries: int = 6, *, now: datetime | None = None) -> str:
    # OUTPUT を一度だけ読み、検証したその同じ dict を表示に使う（S2）。
    # 旧実装は snapshot_is_current()・mtime・自身の json.loads の3回読んで
    # おり、mtime を generated_at とは独立の鮮度権威として使っていた
    # （fix_plan_v2.md の明示的な禁止事項）。
    data = _read_and_validate_snapshot(now=now)
    if data is None:
        return ""
    sug = data.get("suggestions", [])[:max_entries]
    skipped = data.get("skipped", []) or []
    # 観測性: suggestion も skipped も無ければ静黙（scan 未実行）
    if not sug and not skipped:
        return ""

    # 全体契約（schema/鮮度/hash等）が有効でも、個々の行の表示用の値だけが
    # 壊れていることがある。1行の欠陥で全件（この alpha ブロック丸ごと）を
    # 失わせない ―― 壊れた行だけ省略し、件数と理由を残す
    # （呼出元 analyst/__init__.py はこの関数の例外を捕捉して alpha
    # ブロック全体をスキップするため、ここで捕捉しないと「1行の欠陥で
    # 全件消失」になる）。「対象なし」とは表示しない。
    omitted: list[str] = []

    sug_lines: list[str] = []
    for s in sug:
        try:
            sug_lines.append(
                f"- **{s['ticker']}** T-{s['business_days']}bd ({s['earnings_date']}) "
                f"impl-move {s['implied_move_pct']:.1f}% / damage {s['damage_pct']:.2f}% "
                f"→ {s['recommended_action']}"
            )
        except (KeyError, ValueError, TypeError) as e:
            omitted.append(f"{s.get('ticker', '?')}: {e}")

    lines = ["## 🎯 Earnings Proximity Hedge / Trim 候補", ""]
    if sug_lines:
        lines.extend(sug_lines)
        lines.append("")
        lines.append(f"→ damage_pct > {data.get('damage_threshold_pct', 1.5)}% の銘柄は priority_actions に hedge/trim として確実に注入。"
                     "beat_rate < 0.5 の銘柄は前日 trim_50pct を強制採用。")
    else:
        lines.append(f"*閾値超の hedge 対象は現在なし (damage>{data.get('damage_threshold_pct', 1.5)}% 条件)。*")

    # 決算週間近だが閾値下 or option chain 無しの銘柄も Opus に伝える（hedge ではなく monitor として）
    in_window_skips = []
    for s in skipped:
        if s.get("reason") not in ("damage_below_threshold", "no_option_chain"):
            continue
        days = s.get("bdays")
        if days is None:
            continue
        # Validate before comparing: a malformed row must not suppress every
        # otherwise valid row in the earnings prompt.
        if isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(days):
            omitted.append(f"{s.get('ticker', '?')}: invalid bdays")
            continue
        if 0 <= days <= 10:
            in_window_skips.append(s)
    if in_window_skips:
        skip_lines: list[str] = []
        for s in in_window_skips[:8]:
            try:
                if s["reason"] == "damage_below_threshold":
                    skip_lines.append(
                        f"- {s['ticker']} T-{s['bdays']}bd ({s['earnings']}) "
                        f"impl-move {s.get('implied_move_pct','?')}% / damage {s.get('damage_pct','?')}% "
                        f"(pos {s.get('position_pct','?')}%) → monitor のみ"
                    )
                else:
                    skip_lines.append(f"- {s['ticker']} T-{s['bdays']}bd ({s['earnings']}) option chain 取得失敗 → 決算前日 trim_25pct 検討")
            except (KeyError, ValueError, TypeError) as e:
                omitted.append(f"{s.get('ticker', '?')}: {e}")
        if skip_lines:
            lines.append("")
            lines.append("### 📅 決算接近銘柄（hedge 閾値下 or option chain 未取得）")
            lines.extend(skip_lines)
            lines.append("→ priority_actions には含めず hold_notes / risk_warnings で言及すること。")

    if omitted:
        lines.append("")
        lines.append(f"⚠️ 表示不可のため {len(omitted)}件 省略: " + "; ".join(omitted))

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Part E-6 Earnings Proximity Manager")
    parser.add_argument("--dry-run", action="store_true",
                        help="scan して結果を表示するだけ。監視記録も成果物も更新しない。")
    parser.add_argument("--force", action="store_true",
                        help="reuse_current を無効化し常に再スキャンする。")
    parser.add_argument(
        "--scheduled", action="store_true",
        help="cron からの起動であることを明示する。このフラグを付けた実行だけが"
             " earnings_proximity heartbeat（watchdog 可視）を更新する ―― 手動実行や"
             " analyst の self-heal 経由の成功実行が cron 停止を隠さないため。",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        out = scan(dry_run=True)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    kind = "scheduled" if args.scheduled else "manual"

    try:
        out = scan(dry_run=False, reuse_current=not args.force, kind=kind)
    except LockBusy:
        print("⚠️ 別の earnings scan が進行中です。二重起動しません。")
        if args.scheduled:
            heartbeat("earnings_proximity", "warn",
                      error="earnings_proximity_lock_busy",
                      extra={"scheduled": True})
        return 1
    except Exception as exc:
        if args.scheduled:
            heartbeat("earnings_proximity", "error", str(exc)[:500],
                      extra={"scheduled": True})
        raise

    if args.scheduled:
        run_history_recorded = out.get("run_history_recorded", True)
        # scan() 自体は成功しているので rc は変えない ―― run history は
        # 補助的な監視記録であり、その書込み失敗で「スキャンが失敗した」
        # ように見せない。ただし記録するだけで誰も読まないのでは
        # ラウンド2の指摘への対応が名目だけになる: 既存の warn_is_error
        # 経路（earnings_proximity は EXPECTED_INTERVALS で既に設定済み）
        # へ実際に接続し、watchdog が ok と区別できるようにする
        # （2026-09 レビュー Codex 3ラウンド目 指摘 #6・実機再現:
        # run_history_recorded=False でも producer→watchdog が終始 ok
        # のままだった）。
        heartbeat(
            "earnings_proximity",
            "ok" if run_history_recorded else "warn",
            error=None if run_history_recorded else "run_history_write_failed",
            extra={
                "scheduled": True,
                "suggestion_count": out.get("suggestion_count"),
                "holdings_scanned": out.get("holdings_scanned"),
                "generated_at": out.get("generated_at"),
                "run_history_recorded": run_history_recorded,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

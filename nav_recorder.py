"""
nav_recorder.py — P1-18-C: 日次 NAV スナップショット + Modified Dietz TWR

Codex 3 ラウンド目 (T2: NAV 土台が未完成) + 4 ラウンド目補正
(record_daily_performance だけでは TWR は出ない、cash flow を ledger に持たせよ)
を満たすための measurement 基盤。

役割:
  - 日次 NAV を `daily_performance` テーブルに snapshot (既存 record_daily_performance を利用)
  - 期間 TWR を Modified Dietz で計算 (event_ledger の cash_flow を期内入出金として扱う)
  - benchmark との比較は本ファイルでは持たず、別モジュール (benchmark_tracker) で扱う想定

Modified Dietz 公式:
    TWR ≈ (V_end - V_start - sum(CF_i)) / (V_start + sum(w_i * CF_i))

    V_start, V_end : 期初・期末 NAV (JPY)
    CF_i           : 期内 i 番目の外部 cash flow (入金 +, 出金 -, JPY)
    w_i            : (T - t_i) / T  (t_i は flow の経過日数、T は期間日数)

  シンプルなため毎日 valuation がなくても近似できる。正確な TWR には日次の sub-period 計算が要るが、
  個人投資家の月次レポーティング用途では十分。

使い方:
  # cron から日次:
  python nav_recorder.py snapshot
  # 月次/任意期間の TWR:
  python nav_recorder.py twr --from 2026-01-01 --to 2026-05-31
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from almanac.runtime_config import resolve_db_path

BASE_DIR = Path(__file__).parent
DB_PATH  = resolve_db_path(BASE_DIR)


# ============================================================
# Connection (event_ledger と同じスキーマでないため別 helper)
# ============================================================

@contextmanager
def _conn(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    p = db_path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ============================================================
# Daily NAV snapshot
# ============================================================

def snapshot_today(*, db_path: Optional[Path] = None) -> dict:
    """
    portfolio_manager.build_portfolio_snapshot() で現在 NAV を取得し、
    data_fetcher.record_daily_performance() で daily_performance テーブルに保存する。

    既に同日のレコードがある場合は上書き（同日中の再 snapshot を許可）。
    """
    sys.path.insert(0, str(BASE_DIR))
    from portfolio_manager import build_portfolio_snapshot
    from data_fetcher import record_daily_performance

    snap = build_portfolio_snapshot()
    if not isinstance(snap, dict):
        raise RuntimeError(f"build_portfolio_snapshot returned {type(snap).__name__}")
    total = snap.get("total_jpy")
    if total is None or total <= 0:
        raise RuntimeError(f"invalid total_jpy={total!r} - skip snapshot")

    today = date.today().isoformat()

    # 前日 NAV から daily pnl を計算
    prev_nav = _get_previous_nav(today, db_path=db_path)
    daily_pnl_jpy = (float(total) - prev_nav) if prev_nav else 0.0
    daily_pnl_pct = ((daily_pnl_jpy / prev_nav) * 100) if prev_nav and prev_nav > 0 else 0.0

    # 月間損益 (同月 1 日からの差分)
    month_start = today[:7] + "-01"
    month_nav = _get_nav_on_or_after(month_start, db_path=db_path)
    monthly_pnl_jpy = (float(total) - month_nav) if month_nav else 0.0
    monthly_pnl_pct = ((monthly_pnl_jpy / month_nav) * 100) if month_nav and month_nav > 0 else 0.0

    fx = snap.get("fx_rate")
    record_daily_performance(
        portfolio_value=float(total),
        daily_pnl_jpy=daily_pnl_jpy,
        daily_pnl_pct=daily_pnl_pct,
        monthly_pnl_jpy=monthly_pnl_jpy,
        monthly_pnl_pct=monthly_pnl_pct,
        drawdown_pct=0.0,  # current_dd は別途 risk_engine で計算
        fx_rate=fx,
        record_date=today,
    )

    return {
        "date":            today,
        "portfolio_value": float(total),
        "daily_pnl_jpy":   daily_pnl_jpy,
        "daily_pnl_pct":   daily_pnl_pct,
        "monthly_pnl_jpy": monthly_pnl_jpy,
        "monthly_pnl_pct": monthly_pnl_pct,
        "fx_rate":         fx,
    }


def _has_estimated_col(c) -> bool:
    """daily_performance に estimated 列があるか (旧 DB 互換)。"""
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(daily_performance)").fetchall()]
        return "estimated" in cols
    except Exception:
        return False


def _get_previous_nav(today_iso: str, *, db_path: Optional[Path] = None) -> Optional[float]:
    # Codex re-review #4: daily_pnl は DD/VaR/policy に流れるため、前日 NAV は
    # 推定 (nav_backfill, estimated=1) を除外し実測行のみから取る。
    with _conn(db_path) as c:
        est = "AND COALESCE(estimated, 0) = 0 " if _has_estimated_col(c) else ""
        row = c.execute(
            "SELECT portfolio_value FROM daily_performance "
            f"WHERE date < ? {est}ORDER BY date DESC LIMIT 1",
            (today_iso,),
        ).fetchone()
    return float(row["portfolio_value"]) if row else None


def _get_nav_on_or_after(date_iso: str, *, db_path: Optional[Path] = None) -> Optional[float]:
    # Codex re-review #4: monthly_pnl も同様に実測行のみ (推定混入を防ぐ)。
    with _conn(db_path) as c:
        est = "AND COALESCE(estimated, 0) = 0 " if _has_estimated_col(c) else ""
        row = c.execute(
            "SELECT portfolio_value FROM daily_performance "
            f"WHERE date >= ? {est}ORDER BY date ASC LIMIT 1",
            (date_iso,),
        ).fetchone()
    return float(row["portfolio_value"]) if row else None


def _get_nav_on_or_before(date_iso: str, *, db_path: Optional[Path] = None) -> Optional[tuple]:
    """(portfolio_value, actual_date_iso) を返す。該当が無ければ None。

    P0-2: 実日付も返す。`date <= ?` のため、要求日に行が無いと過去日を拾う。
    呼出側が clean_since との突合 (v_start_date >= clean_since) を行えるようにする。
    """
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT portfolio_value, date FROM daily_performance "
            "WHERE date <= ? ORDER BY date DESC LIMIT 1",
            (date_iso,),
        ).fetchone()
    if not row:
        return None
    return (float(row["portfolio_value"]), str(row["date"]))


def _resolve_clean_window(
    date_from: str,
    *,
    clean_since: Optional[str],
    min_clean_days: Optional[int],
    allow_dirty: bool,
) -> tuple[str, Optional[str], Optional[int]]:
    """TWR/DD で共有するクリーン起点のクランプ規約。"""
    if allow_dirty:
        return date_from, clean_since, min_clean_days
    try:
        from config_clean_baseline import (
            clean_nav_since_iso as _clean_nav_since_iso,
            min_clean_days as _min_clean_days,
        )
        effective_clean_since = clean_since or _clean_nav_since_iso()
        effective_min_days = min_clean_days if min_clean_days is not None else _min_clean_days()
        return max(date_from, effective_clean_since), effective_clean_since, effective_min_days
    except Exception:
        return date_from, clean_since, min_clean_days


# ============================================================
# Modified Dietz TWR
# ============================================================

def modified_dietz_twr(
    *,
    date_from: str,
    date_to: str,
    clean_since: Optional[str] = None,
    min_clean_days: Optional[int] = None,
    align_gap_days: int = 4,
    allow_dirty: bool = False,
    include_benchmark: bool = True,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Modified Dietz TWR を計算する。

    入力期間 [date_from, date_to]:
      V_start = date_from 以前の最新 NAV（default では clean_since までクランプ）
      V_end   = date_to 以前の最新 NAV
      cash flows = event_ledger.cash_flow_sum を sub-step で

    P0-2: clean baseline 対応。
      default: clean_nav_since() 以降のみを測定対象にする。allow_dirty=True で明示解除。
      clean_since 指定時: 実 V_start 日付が clean_since より前なら insufficient で α を出さない。
      min_clean_days 指定時: 実測 window がそれ未満なら confirmed=False（数値は返すが未確定）。
      benchmark との実日付整合が取れない (benchmark stale 等) 場合は excess を None にする。
      include_benchmark=False: 系列生成など、portfolio TWR だけ必要な場合の軽量計算。

    cash_flow convention:
      NAV snapshot は終日 NAV とみなす。したがって v_start_date 当日の cash_flow は V_start に
      既に含まれるため期内 flow から除外し、(v_start_date, v_end_date] の flow だけを
      Modified Dietz で controlled-out する。定期積立の日付は nominal のため、実約定日が
      入るまでは TWR の絶対精度に限界がある。

    Returns（主要キー）:
      twr_pct, v_start, v_end, v_start_date, v_end_date,
      benchmark_twr_pct, benchmark_v_start_date, benchmark_v_end_date,
      excess_return_pct (整合不可なら None), excess_suppressed_reason,
      confirmed (bool), clean_ok (bool), period_days_actual, error
    """
    from event_ledger import query_events
    date_from, clean_since, min_clean_days = _resolve_clean_window(
        date_from,
        clean_since=clean_since,
        min_clean_days=min_clean_days,
        allow_dirty=allow_dirty,
    )

    v_start_pair = _get_nav_on_or_before(date_from, db_path=db_path)
    v_end_pair   = _get_nav_on_or_before(date_to,   db_path=db_path)

    if v_start_pair is None or v_end_pair is None:
        return {
            "twr_pct": None,
            "error": f"NAV データ不足 (v_start={v_start_pair}, v_end={v_end_pair}). nav_recorder.py snapshot を cron で動かしてください。",
            "period_days": 0,
            "v_start": None, "v_end": None,
            "v_start_date": None, "v_end_date": None,
            "confirmed": False, "clean_ok": False,
            "excess_return_pct": None, "excess_suppressed_reason": "no_nav_data",
            "net_cash_flow": 0.0, "weighted_cf": 0.0,
            "denominator": 0.0, "numerator": 0.0, "flows": [],
        }

    v_start, v_start_date = v_start_pair
    v_end,   v_end_date   = v_end_pair

    # ── clean baseline 強制: 実 V_start 日付が clean_since より前なら測定不可 ──
    if clean_since and v_start_date < clean_since:
        return {
            "twr_pct": None,
            "error": (
                f"clean_since={clean_since} より前の V_start({v_start_date}) しか無いため "
                f"信頼できる TWR を出せません（汚染期間）。クリーン履歴が貯まるまで保留。"
            ),
            "period_days": 0,
            "v_start": v_start, "v_end": v_end,
            "v_start_date": v_start_date, "v_end_date": v_end_date,
            "confirmed": False, "clean_ok": False,
            "excess_return_pct": None, "excess_suppressed_reason": "v_start_before_clean_since",
            "net_cash_flow": 0.0, "weighted_cf": 0.0,
            "denominator": 0.0, "numerator": 0.0, "flows": [],
        }

    requested_from = date.fromisoformat(date_from)
    requested_to   = date.fromisoformat(date_to)
    requested_T = (requested_to - requested_from).days
    if requested_T <= 0:
        return {
            "twr_pct": None,
            "error": "date_from >= date_to. 期間 ≥ 1 日を指定してください。",
            "period_days": requested_T,
            "v_start": v_start, "v_end": v_end,
            "v_start_date": v_start_date, "v_end_date": v_end_date,
            "confirmed": False, "clean_ok": bool(clean_since is None or v_start_date >= clean_since),
            "excess_return_pct": None, "excess_suppressed_reason": "non_positive_period",
            "net_cash_flow": 0.0, "weighted_cf": 0.0,
            "denominator": 0.0, "numerator": 0.0, "flows": [],
        }

    d_from = date.fromisoformat(v_start_date)
    d_to   = date.fromisoformat(v_end_date)
    T = (d_to - d_from).days
    if T <= 0:
        return {
            "twr_pct": None,
            "error": "実 NAV 日付の期間が 0 日以下です。クリーン履歴が貯まるまで保留。",
            "period_days": requested_T,
            "v_start": v_start, "v_end": v_end,
            "v_start_date": v_start_date, "v_end_date": v_end_date,
            "confirmed": False, "clean_ok": bool(clean_since is None or v_start_date >= clean_since),
            "excess_return_pct": None, "excess_suppressed_reason": "non_positive_actual_period",
            "net_cash_flow": 0.0, "weighted_cf": 0.0,
            "denominator": 0.0, "numerator": 0.0, "flows": [],
        }

    # 期内の cash_flow event を取得
    flows_raw = query_events(
        date_from=v_start_date,
        date_to=v_end_date + "T23:59:59",
        types=["cash_flow"],
        db_path=db_path,
    )

    flows = []
    net_cf = 0.0
    weighted_cf = 0.0
    for ev in flows_raw:
        amt = ev.get("amount_jpy")
        if amt is None:
            continue
        # occurred_at から日付抽出 (YYYY-MM-DD)
        occ = (ev.get("occurred_at") or "")[:10]
        try:
            d_i = date.fromisoformat(occ)
        except ValueError:
            continue
        # NAV snapshot は終日値。V_start 当日の入金は V_start に含まれるため二重控除しない。
        if d_i <= d_from or d_i > d_to:
            continue
        t_i = (d_i - d_from).days
        w_i = (T - t_i) / T if T > 0 else 0.0
        net_cf += float(amt)
        weighted_cf += w_i * float(amt)
        flows.append({"date": occ, "amount_jpy": float(amt), "weight": round(w_i, 4)})

    denominator = float(v_start) + weighted_cf
    numerator   = float(v_end) - float(v_start) - net_cf

    # 実測 window（実 NAV 日付ベース）と confirmed 判定
    try:
        period_days_actual = (date.fromisoformat(v_end_date) - date.fromisoformat(v_start_date)).days
    except ValueError:
        period_days_actual = T
    clean_ok = bool(clean_since is None or v_start_date >= clean_since)
    confirmed = clean_ok and (min_clean_days is None or period_days_actual >= int(min_clean_days))

    if abs(denominator) < 1e-6:
        return {
            "twr_pct": None,
            "error": "denominator ≈ 0 (V_start + weighted_cf がほぼゼロ)",
            "period_days": T,
            "v_start": v_start, "v_end": v_end,
            "v_start_date": v_start_date, "v_end_date": v_end_date,
            "confirmed": False, "clean_ok": clean_ok,
            "period_days_actual": period_days_actual,
            "excess_return_pct": None, "excess_suppressed_reason": "degenerate_denominator",
            "net_cash_flow": net_cf, "weighted_cf": weighted_cf,
            "denominator": denominator, "numerator": numerator,
            "flows": flows,
        }

    twr = numerator / denominator
    twr_pct = round(twr * 100, 4)

    # ── benchmark 比較（実日付整合を確認してから excess を出す）──
    bench = None
    bench_v_start_date = None
    bench_v_end_date = None
    excess = None
    excess_suppressed_reason = None
    if include_benchmark:
        try:
            from benchmark_tracker import get_benchmark_twr, excess_return
            b = get_benchmark_twr(date_from=date_from, date_to=date_to, db_path=db_path)
            if b.get("error") is None:
                bench = b["twr_pct"]
                bench_v_start_date = b.get("v_start_date")
                bench_v_end_date = b.get("v_end_date")
                # 整合チェック: benchmark が clean_since 前で停止 / portfolio と期末が乖離 → excess を出さない
                reasons = []
                if not confirmed:
                    reasons.append("portfolio_twr_unconfirmed")
                # benchmark は clean な市場データ (会計バグ非依存) なので clean_since 制約は課さない。
                # portfolio と benchmark が「同一期間」を測っているか (期初/期末の近接) だけを確認する。
                # 例: clean_since=5/25 が US 休場なら benchmark v_start=5/22 だが、portfolio v_start
                #     (5/25) と 3 営業日以内なので整合とみなす。
                if bench_v_start_date and v_start_date:
                    try:
                        sgap = abs((date.fromisoformat(v_start_date) - date.fromisoformat(bench_v_start_date)).days)
                        if sgap > align_gap_days:
                            reasons.append(f"benchmark_start_misaligned_{sgap}d")
                    except ValueError:
                        reasons.append("benchmark_date_parse_error")
                if bench_v_end_date:
                    try:
                        gap = abs((date.fromisoformat(v_end_date) - date.fromisoformat(bench_v_end_date)).days)
                        if gap > align_gap_days:
                            reasons.append(f"benchmark_stale_gap_{gap}d")
                    except ValueError:
                        reasons.append("benchmark_date_parse_error")
                if reasons:
                    excess_suppressed_reason = ";".join(reasons)
                else:
                    excess = excess_return(twr_pct, bench)
        except Exception as _e:
            excess_suppressed_reason = f"benchmark_error:{_e}"

    return {
        "twr_pct": twr_pct,
        "benchmark_twr_pct": bench,
        "excess_return_pct": excess,
        "excess_suppressed_reason": excess_suppressed_reason,
        "confirmed": confirmed,
        "clean_ok": clean_ok,
        "period_days": T,
        "period_days_actual": period_days_actual,
        "v_start": v_start, "v_end": v_end,
        "v_start_date": v_start_date, "v_end_date": v_end_date,
        "benchmark_v_start_date": bench_v_start_date,
        "benchmark_v_end_date": bench_v_end_date,
        "net_cash_flow": net_cf,
        "weighted_cf":   weighted_cf,
        "denominator":   denominator,
        "numerator":     numerator,
        "flows":         flows,
        "error":         None,
    }


def modified_dietz_twr_series(
    *,
    date_from: str,
    date_to: str,
    clean_since: Optional[str] = None,
    min_clean_days: Optional[int] = None,
    allow_dirty: bool = False,
    db_path: Optional[Path] = None,
) -> dict:
    """同一起点の Modified Dietz TWR を NAV スナップショットごとに返す。

    各点を前の点から連結するのではなく、全点を同じ V_start と cash-flow 規約で
    ``modified_dietz_twr`` から再計算する。したがって、グラフ上の最終点は同期間の
    単体 TWR と一致し、入出金を運用損益へ誤算入しない。
    """
    effective_from, effective_clean_since, effective_min_days = _resolve_clean_window(
        date_from,
        clean_since=clean_since,
        min_clean_days=min_clean_days,
        allow_dirty=allow_dirty,
    )
    final = modified_dietz_twr(
        date_from=effective_from,
        date_to=date_to,
        clean_since=effective_clean_since,
        min_clean_days=effective_min_days,
        allow_dirty=allow_dirty,
        include_benchmark=False,
        db_path=db_path,
    )
    if final.get("error") is not None:
        return {
            "points": [],
            "confirmed": False,
            "clean_ok": bool(final.get("clean_ok")),
            "clean_since": effective_clean_since,
            "v_start_date": final.get("v_start_date"),
            "v_end_date": final.get("v_end_date"),
            "period_days_actual": final.get("period_days_actual", 0),
            "net_cash_flow": final.get("net_cash_flow", 0.0),
            "error": final.get("error"),
        }

    start_date = str(final["v_start_date"])
    end_date = str(final["v_end_date"])
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT date FROM daily_performance "
            "WHERE date > ? AND date <= ? AND portfolio_value IS NOT NULL ORDER BY date ASC",
            (start_date, end_date),
        ).fetchall()

    points = [{"date": start_date, "twr_pct": 0.0}]
    for row in rows:
        point_date = str(row["date"])
        point = modified_dietz_twr(
            date_from=effective_from,
            date_to=point_date,
            clean_since=effective_clean_since,
            min_clean_days=effective_min_days,
            allow_dirty=allow_dirty,
            include_benchmark=False,
            db_path=db_path,
        )
        if point.get("error") is None and point.get("twr_pct") is not None:
            points.append({"date": point_date, "twr_pct": float(point["twr_pct"])})

    return {
        "points": points,
        "confirmed": bool(final.get("confirmed")),
        "clean_ok": bool(final.get("clean_ok")),
        "clean_since": effective_clean_since,
        "v_start_date": start_date,
        "v_end_date": end_date,
        "period_days_actual": final.get("period_days_actual", 0),
        "net_cash_flow": final.get("net_cash_flow", 0.0),
        "error": None,
    }


def compute_max_drawdown(
    *,
    date_from: str,
    date_to: str,
    clean_since: Optional[str] = None,
    min_clean_days: Optional[int] = None,
    allow_dirty: bool = False,
    db_path: Optional[Path] = None,
) -> dict:
    """クリーン起点以降の daily_performance NAV から最大 DD を返す。"""
    date_from, clean_since, min_clean_days = _resolve_clean_window(
        date_from,
        clean_since=clean_since,
        min_clean_days=min_clean_days,
        allow_dirty=allow_dirty,
    )
    try:
        if date.fromisoformat(date_from) >= date.fromisoformat(date_to):
            raise ValueError("date_from >= date_to")
        with _conn(db_path) as c:
            rows = c.execute(
                "SELECT date, portfolio_value FROM daily_performance "
                "WHERE date >= ? AND date <= ? ORDER BY date ASC",
                (date_from, date_to),
            ).fetchall()
    except Exception as exc:
        return {
            "dd_pct": None,
            "confirmed": False,
            "period_days_actual": 0,
            "v_start_date": None,
            "v_end_date": None,
            "clean_ok": False,
            "error": f"NAV データ不足 ({exc})",
        }

    series: list[tuple[str, float]] = []
    for row in rows:
        try:
            nav = float(row["portfolio_value"])
        except (TypeError, ValueError):
            continue
        if nav > 0:
            series.append((str(row["date"]), nav))
    if len(series) < 2:
        return {
            "dd_pct": None,
            "confirmed": False,
            "period_days_actual": 0,
            "v_start_date": series[0][0] if series else None,
            "v_end_date": series[-1][0] if series else None,
            "clean_ok": False,
            "error": "NAV データ不足 (最大DDの算出には2点以上必要です)",
        }

    start_date, end_date = series[0][0], series[-1][0]
    period_days_actual = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
    clean_ok = bool(clean_since is None or start_date >= clean_since)
    confirmed = clean_ok and (min_clean_days is None or period_days_actual >= int(min_clean_days))
    peak = series[0][1]
    max_dd = 0.0
    for _, nav in series:
        peak = max(peak, nav)
        max_dd = min(max_dd, (nav / peak - 1.0) * 100)
    return {
        "dd_pct": round(max_dd, 4),
        "confirmed": confirmed,
        "period_days_actual": period_days_actual,
        "v_start_date": start_date,
        "v_end_date": end_date,
        "clean_ok": clean_ok,
        "error": None,
    }


# ============================================================
# cash_flow 台帳の健全性 (excess α 再解禁ゲート)
# ============================================================

def cash_flow_ledger_status(
    *,
    date_from: str,
    date_to: str,
    db_path: Optional[Path] = None,
) -> dict:
    """期間内の cash_flow event が、既知の定期積立スケジュールに照らして十分記録されて
    いるかを返す。記録漏れがあると Modified Dietz TWR が積立を運用成績として誤計上する
    ため、excess α の再解禁ゲートに使う (P0-5)。

    Returns: {ok, reason, expected_count, actual_count}
    """
    try:
        from contribution_schedule import generate_transactions as _generate_transactions
        expected = _generate_transactions(date_from, date_to)
        exp = len(expected)
    except Exception as e:
        return {"ok": False, "reason": f"schedule_unavailable:{e}",
                "expected_count": None, "actual_count": None}

    try:
        from event_ledger import query_events
        evs = query_events(
            date_from=date_from,
            date_to=date_to + "T23:59:59",
            types=["cash_flow"],
            db_path=db_path,
        )
        actual_events = [e for e in evs if e.get("amount_jpy") is not None]
        actual = len(actual_events)
    except Exception as e:
        return {"ok": False, "reason": f"ledger_unavailable:{e}",
                "expected_count": exp, "actual_count": None}

    if exp == 0:
        # 期間に積立予定が無ければ cash_flow 不要 → ok。
        return {"ok": True, "reason": "no_scheduled_contributions",
                "expected_count": 0, "actual_count": actual}

    expected_by_id = {str(tx.get("id") or ""): tx for tx in expected if tx.get("id")}

    def _tx_key(tx: dict) -> tuple:
        return (
            str(tx.get("timestamp") or "")[:10],
            str(tx.get("broker") or ""),
            float(tx.get("amount") or 0.0),
            "in" if str(tx.get("type") or "").lower() in {"deposit", "in", "入金"} else "out",
        )

    expected_keys = {_tx_key(tx): tx for tx in expected}
    matched_ids = set()
    matched_keys = set()
    extra_event_ids = []
    for ev in actual_events:
        eid = str(ev.get("event_id") or "")
        if eid in expected_by_id:
            matched_ids.add(eid)
            continue
        key = (
            str(ev.get("occurred_at") or "")[:10],
            str(ev.get("account") or ""),
            float(ev.get("amount_jpy") or 0.0),
            str(ev.get("direction") or ""),
        )
        if key in expected_keys:
            matched_keys.add(key)
        else:
            extra_event_ids.append(eid or str(ev.get("id") or ""))

    missing = []
    for eid, tx in expected_by_id.items():
        if eid not in matched_ids and _tx_key(tx) not in matched_keys:
            missing.append(eid)

    ok = len(missing) == 0
    return {
        "ok": ok,
        "reason": "ok" if ok else f"missing_cash_flow:{actual - len(extra_event_ids)}/{exp}",
        "expected_count": exp,
        "actual_count": actual,
        "matched_count": exp - len(missing),
        "missing_ids": missing,
        "extra_event_ids": extra_event_ids,
    }


# ============================================================
# CLI
# ============================================================

def _main() -> None:
    parser = argparse.ArgumentParser(description="ALMANAC NAV recorder & TWR calculator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("snapshot", help="今日の NAV を daily_performance に記録")

    t = sub.add_parser("twr", help="期間 TWR を Modified Dietz で計算")
    t.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    t.add_argument("--to",   dest="date_to",   required=True, help="YYYY-MM-DD")

    args = parser.parse_args()

    if args.cmd == "snapshot":
        r = snapshot_today()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        # heartbeat
        try:
            from utils import heartbeat
            heartbeat("nav_recorder", "ok", extra={"date": r["date"], "nav": r["portfolio_value"]})
        except Exception:
            pass

    elif args.cmd == "twr":
        r = modified_dietz_twr(date_from=args.date_from, date_to=args.date_to)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if r.get("error"):
            sys.exit(1)


if __name__ == "__main__":
    _main()

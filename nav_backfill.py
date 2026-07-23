"""
nav_backfill.py — 過去日 NAV を厳密に再構築する

設計 (Codex 2026-05-17 P3 「厳密版なら OK」を満たす):
  - anchor: 現在の portfolio_total_jpy (build_portfolio_snapshot) を起点に固定
  - event_ledger の trade / cash_flow event を逆向きに巻き戻して、各営業日終わりの
    shares と cash を再構築
  - その日の close (parquet) と USDJPY (parquet) で時価評価
  - daily_performance テーブルに INSERT OR REPLACE で idempotent 保存

明示する制約 (=雑な backfill ではなく事実ベースである理由):
  - 「現状の事実 (holdings + account 残高) を始点にした逆計算」なので、
    現在と整合する NAV 系列が得られる
  - 過去の入出金 (cash_flow event) が ledger に無い場合、その分は「投資成績」と
    みなされる。**過去入出金を後付けで補完してから本スクリプトを再実行すれば、
    TWR は controlled out で正しく再計算される (event_ledger は idempotent なので安全)**
  - 投信 (SLIM_*, MNXACT 等) は parquet が無いため anchor 時点評価のまま固定。
    時系列の細かい変動は無視される。価格データが揃ったら精度が自動的に上がる

使い方:
  python nav_backfill.py --days 90               # dry-run: 直近 90 営業日
  python nav_backfill.py --days 90 --apply       # daily_performance に書き込み
  python nav_backfill.py --start 2026-02-19 --end 2026-05-17 --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from almanac.runtime_config import resolve_db_path

BASE_DIR = Path(__file__).parent
DB_PATH = resolve_db_path(BASE_DIR)
OHLCV_DIR = BASE_DIR / "data" / "ohlcv"
HOLDINGS_FILE = BASE_DIR / "holdings.json"

# 投信・現金など close 価格 timeseries を持たない key (holdings.json の key 名)
NON_TIMESERIES_KEYS = {
    "CASH_JPY", "CASH_USD", "CASH_JPY_SBI",
    "GS_MMF_USD",
    "SLIM_SP500", "SLIM_ORCAN", "MNXACT", "IFREE_FANGPLUS", "NOMURA_SEMI",
    "SLIM_SP500_WIFE", "SLIM_ORCAN_WIFE", "CASH_JPY_SBI_WIFE",
}

# 投信 ticker の prefix (event_ledger.trade event で ticker が "SLIM_*" 等の場合に検出)
# 投信は単位が 1万口建て NAV のため、event_ledger の price/quantity を素朴に
# shares × price で計算すると桁が壊れる (実際にデータ汚染が発生済)。
# 当面 nav_backfill では投信 trade を rest_value 側に押し付けて scope 外とする。
NON_TIMESERIES_TICKER_PREFIXES = ("SLIM_", "MNXACT", "IFREE_", "NOMURA_")


def _is_non_timeseries_ticker(ticker: str) -> bool:
    if not ticker:
        return False
    if ticker in NON_TIMESERIES_KEYS:
        return True
    return any(ticker.startswith(prefix) for prefix in NON_TIMESERIES_TICKER_PREFIXES)


def _load_close_series(ticker: str) -> Optional[dict]:
    """parquet の Close 系列を {YYYY-MM-DD: close_price} で返す。"""
    try:
        import pandas as pd
    except ImportError:
        return None
    p = OHLCV_DIR / f"{ticker.replace('/', '_')}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
    except Exception:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        cands = [c for c in df.columns if c[0] == "Close"]
        if not cands:
            return None
        s = df[cands[0]]
    elif "Close" in df.columns:
        s = df["Close"]
    else:
        return None

    s = s.dropna()
    out = {}
    for idx, val in s.items():
        try:
            d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
            out[d] = float(val)
        except Exception:
            continue
    return out


def _close_on_or_before(series: dict, date_iso: str) -> Optional[float]:
    """date_iso 当日または直前の close を返す。土日休場対応。"""
    if not series:
        return None
    d = date_iso
    for _ in range(10):  # 最大 10 日遡る
        if d in series:
            return series[d]
        try:
            prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        except ValueError:
            return None
        d = prev
    return None


def _query_events(db_path: Path) -> list:
    """trade + cash_flow + dividend + tax の全 event を occurred_at ASC で取得。"""
    from event_ledger import init_schema, query_events
    init_schema(db_path)
    return query_events(
        types=["trade", "cash_flow", "dividend", "tax", "fee"],
        db_path=db_path,
    )


def _ticker_currency(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    if t.endswith(".T") or t.endswith(".JP") or t.endswith(".JPX") or t.endswith(".OS"):
        return "JPY"
    return "USD"


def _build_shares_anchor(holdings: dict) -> dict:
    """
    timeseries 評価対象 (NON_TIMESERIES_KEYS でない) 銘柄の現状 shares を返す。
    投信・現金・MMF などはここに含まれず、rest_value 側で逆算的に扱う。
    """
    shares_by_ticker: dict = {}
    for key, pos in holdings.items():
        if not isinstance(pos, dict):
            continue
        shares = float(pos.get("shares") or 0)
        if shares == 0:
            continue
        ticker = pos.get("ticker", key)
        if key in NON_TIMESERIES_KEYS or ticker in NON_TIMESERIES_KEYS:
            continue
        shares_by_ticker[ticker] = shares_by_ticker.get(ticker, 0.0) + shares
    return shares_by_ticker


def _value_positions_on_date(
    shares_by_ticker: dict,
    target_date: str,
    *,
    close_cache: dict,
    fx_close_series: dict,
) -> float:
    """指定日終わり時点の position 時価 (JPY) を返す。"""
    fx = _close_on_or_before(fx_close_series, target_date) or 150.0
    total = 0.0
    for ticker, shares in shares_by_ticker.items():
        if abs(shares) < 1e-9:
            continue
        series = close_cache.get(ticker)
        if series is None:
            series = _load_close_series(ticker)
            close_cache[ticker] = series or {}
        close = _close_on_or_before(close_cache.get(ticker) or {}, target_date)
        if close is None:
            continue
        currency = _ticker_currency(ticker)
        jpy_value = shares * close * fx if currency == "USD" else shares * close
        total += jpy_value
    return round(total, 2)


def _latest_price_date(
    shares_by_ticker: dict,
    not_after_iso: str,
    *,
    close_cache: dict,
    fx_close_series: dict,
) -> Optional[str]:
    """timeseries 銘柄 + FX の中で not_after_iso 以前の最新 close 日付を返す。

    anchor を「最後に判明している現値」で評価するために使う。`date.today()` を直接使うと、
    parquet が数日 stale なだけで _close_on_or_before が None を返し、anchor ポジションが
    丸ごと 0 評価 → rest_value が過大になるサイレント故障が起きるため (本関数で回避)。
    """
    latest: Optional[str] = None
    serieses = [fx_close_series] if fx_close_series else []
    for ticker, shares in shares_by_ticker.items():
        if abs(shares) < 1e-9:
            continue
        s = close_cache.get(ticker)
        if s is None:
            s = _load_close_series(ticker) or {}
            close_cache[ticker] = s
        serieses.append(s)
    for s in serieses:
        for d_iso in s.keys():
            if d_iso <= not_after_iso and (latest is None or d_iso > latest):
                latest = d_iso
    return latest


# outlier 検出閾値: 単一 trade event の amount_jpy が portfolio_total の 20% を超えたら
# 「データ汚染の可能性」として scope 外にし、warn list に積む。
# (実例: trade_history.csv の桁ミスで shares=120 @ $4815 = ¥9100 万 など)
OUTLIER_AMOUNT_RATIO = 0.20


def _detect_outlier_events(events: list, anchor_total_jpy: float) -> set:
    """abs(amount_jpy) > anchor_total × OUTLIER_AMOUNT_RATIO の event_id 集合を返す。"""
    threshold = abs(anchor_total_jpy) * OUTLIER_AMOUNT_RATIO
    outliers = set()
    for ev in events:
        amt = ev.get("amount_jpy")
        if amt is None:
            continue
        if abs(float(amt)) > threshold:
            outliers.add(ev.get("event_id"))
    return outliers


def reconstruct_nav_series(
    *,
    start_date: str,
    end_date: str,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """
    指定期間の各カレンダー日終わりの NAV を再構築する。

    方針:
      - anchor_total_jpy = build_portfolio_snapshot()["total_jpy"] (今日の事実値)
      - anchor_positions_today = Σ (timeseries 銘柄の現状 shares × close_today × fx_today)
      - rest_value_anchor = anchor_total_jpy - anchor_positions_today
          → 投信 / 現金 / MMF / 持株会以外 (parquet 無い key) の合計が anchor 時点で含まれる
          → 過去日でも rest_value は固定 (投信 NAV の時間変動は捨てる、運用上許容)
      - 各日 D:
          shares_at_D = anchor_shares から (D 以降の trade) を巻き戻し
          cash_delta_after_D = Σ amount_jpy for events D 以降
          positions_at_D = Σ (shares_at_D × close[D] × fx[D])
          NAV(D) = positions_at_D + rest_value_anchor - cash_delta_after_D

    重要な性質:
      - D = today なら cash_delta_after_D=0、positions_at_D=anchor_positions_today
            → NAV(today) = anchor_total_jpy (現在の事実値と一致)
    """
    from utils import load_json_strict
    sys.path.insert(0, str(BASE_DIR))
    from portfolio_manager import build_portfolio_snapshot

    holdings = load_json_strict(HOLDINGS_FILE)
    snap = build_portfolio_snapshot()
    anchor_total = float(snap.get("total_jpy") or 0)

    events = _query_events(db_path)
    fx_series = _load_close_series("USDJPY=X") or {}
    close_cache: dict = {}

    shares_anchor = _build_shares_anchor(holdings)
    today_iso = date.today().isoformat()
    # anchor_total は build_portfolio_snapshot の「現在の最新価格」ベース。整合のため
    # anchor ポジションも today ではなく「最新の判明済み価格日」で評価する。
    # parquet が stale でも 0 評価 → rest_value 過大 (サイレント故障) を避ける。
    anchor_price_date = _latest_price_date(
        shares_anchor, today_iso, close_cache=close_cache, fx_close_series=fx_series,
    ) or today_iso
    anchor_positions_today = _value_positions_on_date(
        shares_anchor, anchor_price_date, close_cache=close_cache, fx_close_series=fx_series,
    )
    rest_value_anchor = round(anchor_total - anchor_positions_today, 2)

    # 投信 event は scope 外 (data 汚染リスクが高い)、outlier event も scope 外
    outlier_event_ids = _detect_outlier_events(events, anchor_total)

    d_start = date.fromisoformat(start_date)
    d_end   = date.fromisoformat(end_date)
    if d_start > d_end:
        raise ValueError("start_date > end_date")

    results = []
    d = d_start
    while d <= d_end:
        d_iso = d.isoformat()
        end_of_day = d_iso + "T23:59:59"

        future_events = [e for e in events if (e.get("occurred_at") or "") > end_of_day]

        shares_at_D = dict(shares_anchor)
        cash_delta_after_D = 0.0
        skipped_funds = 0
        skipped_outliers = 0
        for ev in future_events:
            etype = ev.get("event_type")
            ticker = ev.get("ticker") or ""

            # 投信 trade は scope 外 (data 汚染で amount_jpy が桁違いの可能性あり)。
            # rest_value 側に固定計上されている前提で、ここでは cash delta も shares も触らない。
            if etype == "trade" and _is_non_timeseries_ticker(ticker):
                skipped_funds += 1
                continue

            # outlier (abs(amount_jpy) > anchor_total × 20%) は scope 外
            # 例: trade_history.csv の桁ミスで $48.15 → $4815 と入力された event
            if ev.get("event_id") in outlier_event_ids:
                skipped_outliers += 1
                continue

            amt = ev.get("amount_jpy")
            if amt is not None:
                cash_delta_after_D += float(amt)
            if etype == "trade":
                if not ticker:
                    continue
                qty = float(ev.get("quantity") or 0)
                if qty == 0:
                    continue
                if ev.get("direction") == "buy":
                    shares_at_D[ticker] = shares_at_D.get(ticker, 0.0) - qty
                elif ev.get("direction") == "sell":
                    shares_at_D[ticker] = shares_at_D.get(ticker, 0.0) + qty

        positions_value = _value_positions_on_date(
            shares_at_D, d_iso, close_cache=close_cache, fx_close_series=fx_series,
        )
        nav = round(positions_value + rest_value_anchor - cash_delta_after_D, 0)

        results.append({
            "date":               d_iso,
            "portfolio_value":    int(nav),
            "positions_value":    int(round(positions_value)),
            "rest_value":         int(rest_value_anchor),
            "cash_delta_after":   int(round(cash_delta_after_D)),
            "future_events":      len(future_events),
            "skipped_fund_evts":  skipped_funds,
            "skipped_outlier_evts": skipped_outliers,
        })
        d += timedelta(days=1)

    return results


def upsert_daily_performance(rows: list[dict], *, db_path: Path = DB_PATH,
                             estimated: bool = True) -> int:
    """daily_performance テーブルに INSERT OR REPLACE で idempotent 保存。

    Codex P1 #4: nav_backfill が書く行は「現在の保有・総資産を anchor に過去へ逆計算した
    *推定* NAV」(投信は anchor 固定、outlier 除外、価格欠損は近似)。estimated=1 で印を付け、
    DD/VaR/policy のリーダーが除外できるようにする (TWR/performance 表示では利用可)。
    実スナップショット (data_fetcher) は estimated=0。
    """
    if not rows:
        return 0
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_performance (
                date            TEXT PRIMARY KEY,
                portfolio_value REAL,
                daily_pnl_jpy   REAL,
                daily_pnl_pct   REAL,
                monthly_pnl_jpy REAL,
                monthly_pnl_pct REAL,
                drawdown_pct    REAL,
                fx_rate_usdjpy  REAL,
                estimated       INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (datetime('now', 'localtime'))
            );
        """)
        # 既存テーブルに estimated 列が無ければ追加 (idempotent migration)
        try:
            conn.execute("ALTER TABLE daily_performance ADD COLUMN estimated INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        est = 1 if estimated else 0
        # 前日 NAV から daily_pnl を再計算
        prev = None
        for row in rows:
            nav = row["portfolio_value"]
            d_pnl_jpy = (nav - prev) if prev else 0
            d_pnl_pct = (d_pnl_jpy / prev * 100) if prev else 0.0
            # Codex re-review #4: 実測行 (estimated=0) を推定行で上書きしない。
            # 競合時は既存が推定 (estimated=1) のときだけ更新する。
            conn.execute(
                """
                INSERT INTO daily_performance
                  (date, portfolio_value, daily_pnl_jpy, daily_pnl_pct,
                   monthly_pnl_jpy, monthly_pnl_pct, drawdown_pct, fx_rate_usdjpy, estimated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                  portfolio_value = excluded.portfolio_value,
                  daily_pnl_jpy   = excluded.daily_pnl_jpy,
                  daily_pnl_pct   = excluded.daily_pnl_pct,
                  monthly_pnl_jpy = excluded.monthly_pnl_jpy,
                  monthly_pnl_pct = excluded.monthly_pnl_pct,
                  drawdown_pct    = excluded.drawdown_pct,
                  fx_rate_usdjpy  = excluded.fx_rate_usdjpy,
                  estimated       = excluded.estimated
                WHERE daily_performance.estimated = 1
                """,
                (row["date"], nav, d_pnl_jpy, round(d_pnl_pct, 4), 0.0, 0.0, 0.0, None, est),
            )
            prev = nav
        conn.commit()
    return len(rows)


def _main() -> None:
    parser = argparse.ArgumentParser(description="event_ledger から過去日 NAV を厳密再構築")
    parser.add_argument("--days", type=int, default=None,
                        help="今日から N 日前まで (--start/--end と排他)")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD")
    parser.add_argument("--end",   default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--apply", action="store_true", help="daily_performance に書き込む")
    args = parser.parse_args()

    if args.days is not None:
        end_d = date.today()
        start_d = end_d - timedelta(days=args.days)
    else:
        start_d = date.fromisoformat(args.start) if args.start else (date.today() - timedelta(days=90))
        end_d   = date.fromisoformat(args.end)   if args.end   else date.today()

    rows = reconstruct_nav_series(start_date=start_d.isoformat(), end_date=end_d.isoformat())

    # outlier event を audit のため別途リストアップ
    from event_ledger import query_events
    events = query_events(types=["trade", "cash_flow", "dividend", "tax", "fee"])
    sys.path.insert(0, str(BASE_DIR))
    from portfolio_manager import build_portfolio_snapshot
    anchor_total = float(build_portfolio_snapshot().get("total_jpy") or 0)
    outlier_ids = _detect_outlier_events(events, anchor_total)
    outliers = [
        {
            "event_id":    e.get("event_id"),
            "occurred_at": e.get("occurred_at"),
            "ticker":      e.get("ticker"),
            "direction":   e.get("direction"),
            "quantity":    e.get("quantity"),
            "price":       e.get("price"),
            "currency":    e.get("currency"),
            "amount_jpy":  e.get("amount_jpy"),
        }
        for e in events if e.get("event_id") in outlier_ids
    ]

    summary = {
        "dry_run":     not args.apply,
        "start_date":  start_d.isoformat(),
        "end_date":    end_d.isoformat(),
        "rows":        len(rows),
        "first_nav":   rows[0] if rows else None,
        "last_nav":    rows[-1] if rows else None,
        "min_nav":     min((r["portfolio_value"] for r in rows), default=None),
        "max_nav":     max((r["portfolio_value"] for r in rows), default=None),
        "outlier_event_count": len(outliers),
        "outliers":    outliers,
    }
    if args.apply:
        n = upsert_daily_performance(rows)
        summary["inserted_or_replaced"] = n

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _main()

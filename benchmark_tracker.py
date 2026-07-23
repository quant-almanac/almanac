"""
benchmark_tracker.py — P1-18 と一体: JPY base benchmark の NAV 計測

目的:
  「税引後・手数料後・JPY建て実質純資産が benchmark + 200bps を 12 ヶ月 rolling で達成」が
   "資産最大化 OS" の合否判定。本モジュールは benchmark 側を担当する。

ベンチマーク定義 (Plan の objective より):
  60% MSCI ACWI (JPY建て)  +  40% 日本国債

実装 proxy (yfinance で取得可能なもの):
  - equity: VT (Vanguard Total World ETF, USD) × USDJPY を JPY 換算
  - bond:   AGG (US Aggregate Bond, USD) × USDJPY を JPY 換算
    (日本国債 ETF は流動性が低く yfinance での履歴も不安定なため、グローバル投資適格債で代替)

  両 ETF を JPY 換算したシリーズで constant-weight (60/40) のリバランス free な
  仮想ポートフォリオ NAV を作る。月次リバランスは P2 で実装、現状は continuous rebalance。

スキーマ (almanac.db / legacy nexustrader.db に追加):
  benchmark_daily
    date            TEXT PRIMARY KEY
    nav             REAL          -- 100 起点の JPY NAV
    equity_close    REAL          -- 構成銘柄の close (USD)
    bond_close      REAL
    fx_usdjpy       REAL          -- 当該日 close

API:
  rebuild_history(start_date='2020-01-01') -> int  # daily NAV を構築
  get_benchmark_twr(date_from, date_to) -> float | None  # 期間 TWR
  excess_return(portfolio_twr_pct, benchmark_twr_pct) -> float  # 簡易差分

使い方:
  python benchmark_tracker.py rebuild  # yfinance から再構築
  python benchmark_tracker.py twr --from 2026-01-01 --to 2026-05-16
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
# Config — env var で上書き可能
# ============================================================

DEFAULT_EQUITY_TICKER = "VT"     # MSCI ACWI proxy
DEFAULT_BOND_TICKER   = "AGG"    # 日本国債の代替 (流動性 + yfinance 履歴の都合)
DEFAULT_EQUITY_WEIGHT = 0.60
DEFAULT_BOND_WEIGHT   = 0.40


def _config() -> dict:
    import os
    return {
        "equity_ticker": os.environ.get("BENCHMARK_EQUITY_TICKER", DEFAULT_EQUITY_TICKER),
        "bond_ticker":   os.environ.get("BENCHMARK_BOND_TICKER",   DEFAULT_BOND_TICKER),
        "equity_weight": float(os.environ.get("BENCHMARK_EQUITY_WEIGHT", DEFAULT_EQUITY_WEIGHT)),
        "bond_weight":   float(os.environ.get("BENCHMARK_BOND_WEIGHT",   DEFAULT_BOND_WEIGHT)),
        "fx_pair":       os.environ.get("BENCHMARK_FX_PAIR", "USDJPY=X"),
    }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS benchmark_daily (
    date            TEXT NOT NULL,
    nav             REAL NOT NULL,
    equity_close    REAL,
    bond_close      REAL,
    fx_usdjpy       REAL,
    config_hash     TEXT NOT NULL DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (config_hash, date)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_date     ON benchmark_daily(date);
CREATE INDEX IF NOT EXISTS idx_benchmark_cfg_date ON benchmark_daily(config_hash, date);
"""


def _config_hash(cfg: Optional[dict] = None) -> str:
    """benchmark 構成 (ticker/weight/fx) を一意化する決定論ハッシュ文字列。"""
    return json.dumps(cfg if cfg is not None else _config(), sort_keys=True)


# ============================================================
# Connection
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


def init_schema(db_path: Optional[Path] = None) -> None:
    with _conn(db_path) as c:
        # Codex P2 #11: 旧スキーマ (date 単独 PK) を composite PK (config_hash, date) へ移行。
        # SQLite は PK 変更に table 再作成が必要なため、退避→新規→コピー→DROP を idempotent に行う。
        row = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='benchmark_daily'"
        ).fetchone()
        if row and row["sql"] and "PRIMARY KEY (config_hash, date)" not in row["sql"]:
            c.execute("ALTER TABLE benchmark_daily RENAME TO benchmark_daily_old")
            c.executescript(SCHEMA_SQL)
            # Codex re-review #11: 旧 NULL/'' config_hash を現行デフォルト config hash に割当てる。
            # '' に移すと get_benchmark_twr(現行 config) から不可視になり TWR が出せないため。
            # 旧 benchmark_daily は date 単独 PK = 単一構成で、現行デフォルト構成で構築されている前提
            # (構成変更時は `benchmark_tracker.py rebuild` で正しい hash に再構築される)。
            legacy_hash = _config_hash()
            c.execute(
                """
                INSERT OR REPLACE INTO benchmark_daily
                  (date, nav, equity_close, bond_close, fx_usdjpy, config_hash, created_at)
                SELECT date, nav, equity_close, bond_close, fx_usdjpy,
                       CASE WHEN config_hash IS NULL OR config_hash = ''
                            THEN ? ELSE config_hash END,
                       created_at
                FROM benchmark_daily_old
                """,
                (legacy_hash,),
            )
            c.execute("DROP TABLE benchmark_daily_old")
        else:
            c.executescript(SCHEMA_SQL)

        # Codex re-re-review #11: 既に composite PK だが、過去の不完全な移行で
        # config_hash='' / NULL のまま残った行も現行 hash に移す (idempotent)。
        # 空行が無ければ何もしない (頻繁に呼ばれる init_schema のため軽量ガード)。
        has_empty = c.execute(
            "SELECT 1 FROM benchmark_daily WHERE config_hash IS NULL OR config_hash = '' LIMIT 1"
        ).fetchone()
        if has_empty:
            legacy_hash = _config_hash()
            # date 重複時は現行 hash 行を優先し、空 hash 行を削除 (PK 衝突回避)。
            c.execute(
                "DELETE FROM benchmark_daily "
                " WHERE (config_hash IS NULL OR config_hash = '') "
                "   AND date IN (SELECT date FROM benchmark_daily WHERE config_hash = ?)",
                (legacy_hash,),
            )
            c.execute(
                "UPDATE benchmark_daily SET config_hash = ? "
                " WHERE config_hash IS NULL OR config_hash = ''",
                (legacy_hash,),
            )


# ============================================================
# History rebuild
# ============================================================

def rebuild_history(
    *,
    start_date: str = "2020-01-01",
    end_date: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> int:
    """
    yfinance から equity + bond + fx を取得し、benchmark daily NAV を再構築する。

    constant weight rebalance なし (continuous): 各日の単純加重平均 return から NAV を積む。
    起点 NAV = 100.0、起点 = start_date の最初の有効日。

    Returns 挿入された行数。
    """
    cfg = _config()
    end_date = end_date or date.today().isoformat()

    try:
        import yfinance as yf
        import pandas as pd
    except ImportError as e:
        raise RuntimeError(f"yfinance / pandas が必要: {e}") from e

    # 3 系列を取得 (Close)
    tickers = [cfg["equity_ticker"], cfg["bond_ticker"], cfg["fx_pair"]]
    raw = yf.download(tickers, start=start_date, end=end_date, progress=False, auto_adjust=True)
    if raw.empty:
        raise RuntimeError("yfinance returned empty data")

    # MultiIndex columns → "Close" を抜く
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            close = raw["Close"]
        else:
            close = raw  # fallback
    else:
        close = raw

    close = close.dropna(how="any")
    if close.empty:
        raise RuntimeError("3 系列とも揃った日付がない（FX が祝日休場の場合あり）")

    eq_col = cfg["equity_ticker"]
    bd_col = cfg["bond_ticker"]
    fx_col = cfg["fx_pair"]
    for col in (eq_col, bd_col, fx_col):
        if col not in close.columns:
            raise RuntimeError(f"ticker '{col}' のデータが取れない")

    # USD → JPY 換算
    eq_jpy = close[eq_col] * close[fx_col]
    bd_jpy = close[bd_col] * close[fx_col]

    # 日次 return
    eq_ret = eq_jpy.pct_change().fillna(0.0)
    bd_ret = bd_jpy.pct_change().fillna(0.0)

    # constant weight 加重
    port_ret = cfg["equity_weight"] * eq_ret + cfg["bond_weight"] * bd_ret

    # NAV 累積 (100 起点)
    nav = (1.0 + port_ret).cumprod() * 100.0
    nav.iloc[0] = 100.0

    config_hash = _config_hash(cfg)

    init_schema(db_path)
    inserted = 0
    with _conn(db_path) as c:
        # まず該当 config_hash の全行を削除して入れ直す (idempotent rebuild)
        c.execute("DELETE FROM benchmark_daily WHERE config_hash = ?", (config_hash,))
        for ts in nav.index:
            iso = ts.strftime("%Y-%m-%d")
            c.execute(
                """
                INSERT OR REPLACE INTO benchmark_daily
                  (date, nav, equity_close, bond_close, fx_usdjpy, config_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    iso,
                    float(nav.loc[ts]),
                    float(close[eq_col].loc[ts]),
                    float(close[bd_col].loc[ts]),
                    float(close[fx_col].loc[ts]),
                    config_hash,
                ),
            )
            inserted += 1
    return inserted


# ============================================================
# TWR / excess return
# ============================================================

def _get_nav_on_or_before(date_iso: str, *, config_hash: Optional[str] = None,
                          db_path: Optional[Path] = None) -> Optional[tuple]:
    """(nav, actual_date_iso) を返す。該当が無ければ None。

    P0-2: 実日付も返し、呼出側 (nav_recorder) が portfolio との期日整合
    (benchmark stale 検知) を行えるようにする。
    Codex P2 #11: config_hash で benchmark 構成を固定し、別構成の NAV が混入しないようにする。
    """
    if config_hash is None:
        config_hash = _config_hash()
    init_schema(db_path)
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT nav, date FROM benchmark_daily "
            "WHERE config_hash = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (config_hash, date_iso),
        ).fetchone()
    if not row:
        return None
    return (float(row["nav"]), str(row["date"]))


def get_benchmark_twr(
    *,
    date_from: str,
    date_to: str,
    config_hash: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Benchmark TWR を計算する。constant weight なので単純比率 (V_end / V_start - 1)。
    P0-2: 実 v_start_date / v_end_date も返す。
    Codex P2 #11: 開始 NAV と終了 NAV を必ず同一 config_hash から取り、構成混在を防ぐ。
    """
    if config_hash is None:
        config_hash = _config_hash()
    v_start_pair = _get_nav_on_or_before(date_from, config_hash=config_hash, db_path=db_path)
    v_end_pair   = _get_nav_on_or_before(date_to,   config_hash=config_hash, db_path=db_path)
    if v_start_pair is None or v_end_pair is None:
        return {
            "twr_pct": None,
            "v_start": None if v_start_pair is None else v_start_pair[0],
            "v_end":   None if v_end_pair is None else v_end_pair[0],
            "v_start_date": None if v_start_pair is None else v_start_pair[1],
            "v_end_date":   None if v_end_pair is None else v_end_pair[1],
            "error":   "benchmark_daily 不足。`python benchmark_tracker.py rebuild` で構築してください。",
        }
    v_start, v_start_date = v_start_pair
    v_end,   v_end_date   = v_end_pair
    twr = (v_end / v_start) - 1.0
    return {
        "twr_pct": round(twr * 100, 4),
        "v_start": v_start,
        "v_end":   v_end,
        "v_start_date": v_start_date,
        "v_end_date":   v_end_date,
        "error":   None,
    }


def excess_return(portfolio_twr_pct: Optional[float],
                  benchmark_twr_pct: Optional[float]) -> Optional[float]:
    """portfolio - benchmark の単純差分 (bp 単位ではなく %)。両方必要なら None。"""
    if portfolio_twr_pct is None or benchmark_twr_pct is None:
        return None
    return round(portfolio_twr_pct - benchmark_twr_pct, 4)


def beta_adjusted_alpha(
    portfolio_returns,
    benchmark_returns,
    *,
    periods_per_year: int = 252,
) -> dict:
    """OLS beta and annualized arithmetic Jensen alpha, with zero risk-free rate."""
    import numpy as np

    p = np.asarray(list(portfolio_returns), dtype=float)
    b = np.asarray(list(benchmark_returns), dtype=float)
    mask = np.isfinite(p) & np.isfinite(b)
    p, b = p[mask], b[mask]
    if len(p) < 20:
        return {"n": len(p), "beta": None, "alpha_pct_annualized": None,
                "error": "at least 20 aligned observations required"}
    variance = float(np.var(b, ddof=1))
    if variance <= 0:
        return {"n": len(p), "beta": None, "alpha_pct_annualized": None,
                "error": "benchmark variance is zero"}
    beta = float(np.cov(p, b, ddof=1)[0, 1] / variance)
    alpha_daily = float(np.mean(p - beta * b))
    return {
        "n": len(p),
        "beta": beta,
        "alpha_pct_annualized": alpha_daily * periods_per_year * 100.0,
        "error": None,
    }


def get_beta_adjusted_alpha(
    *,
    date_from: str,
    date_to: str,
    config_hash: Optional[str] = None,
    db_path: Optional[Path] = None,
    allow_dirty: bool = False,
) -> dict:
    """Calculate beta-adjusted alpha from clean portfolio and benchmark daily returns."""
    if not allow_dirty:
        from config_clean_baseline import clamp_date_from
        date_from = clamp_date_from(date_from)
    config_hash = config_hash or _config_hash()
    init_schema(db_path)
    with _conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.date, d.daily_pnl_pct, b.nav
            FROM daily_performance d
            JOIN benchmark_daily b ON b.date = d.date
            WHERE d.date BETWEEN ? AND ?
              AND COALESCE(d.estimated, 0) = 0
              AND d.daily_pnl_pct IS NOT NULL
              AND b.config_hash = ?
            ORDER BY d.date
            """,
            (date_from, date_to, config_hash),
        ).fetchall()
    portfolio = []
    benchmark = []
    previous_nav = None
    for row in rows:
        nav = float(row["nav"])
        if previous_nav is not None and previous_nav > 0:
            portfolio.append(float(row["daily_pnl_pct"]) / 100.0)
            benchmark.append(nav / previous_nav - 1.0)
        previous_nav = nav
    result = beta_adjusted_alpha(portfolio, benchmark)
    result.update({
        "date_from": date_from,
        "date_to": date_to,
        "config": _config(),
    })
    return result


# ============================================================
# CLI
# ============================================================

def _main() -> None:
    parser = argparse.ArgumentParser(description="ALMANAC benchmark tracker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("rebuild", help="yfinance から benchmark NAV 履歴を再構築")
    r.add_argument("--start", default="2020-01-01")
    r.add_argument("--end",   default=None)

    t = sub.add_parser("twr", help="期間 benchmark TWR")
    t.add_argument("--from", dest="date_from", required=True)
    t.add_argument("--to",   dest="date_to",   required=True)

    sub.add_parser("config", help="現在の config を表示")

    args = parser.parse_args()

    if args.cmd == "rebuild":
        n = rebuild_history(start_date=args.start, end_date=args.end)
        print(json.dumps({"inserted": n, "config": _config()}, ensure_ascii=False, indent=2))
    elif args.cmd == "twr":
        r = get_benchmark_twr(date_from=args.date_from, date_to=args.date_to)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if r.get("error"):
            sys.exit(1)
    elif args.cmd == "config":
        print(json.dumps(_config(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()

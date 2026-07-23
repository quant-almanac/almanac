"""
ALMANAC v4.0 - データ取得・永続化
yfinanceからOHLCVを取得してParquetに保存、SQLiteでトレード・ガードレール履歴を管理
"""

import sqlite3
import json
import hashlib
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
import yfinance as yf
from almanac.runtime_config import resolve_db_path
from utils import init_yfinance_timeout

init_yfinance_timeout()

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / 'data'
OHLCV_DIR = DATA_DIR / 'ohlcv'
DB_PATH   = resolve_db_path(BASE_DIR)
PRICE_SANITY_LOG = BASE_DIR / 'data' / 'price_sanity_flags.jsonl'

OHLCV_DIR.mkdir(parents=True, exist_ok=True)


def _connect(db_path=DB_PATH, timeout: float = 30.0) -> sqlite3.Connection:
    """SQLite 接続を WAL モード + 30 秒待ちで開く（並列 cron での競合回避）。"""
    con = sqlite3.connect(db_path, timeout=timeout)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA busy_timeout=30000')
        con.execute('PRAGMA synchronous=NORMAL')
    except sqlite3.Error:
        pass
    return con

# yfinanceで取得できない独自キー（投資信託等・複数口座保有銘柄の非標準キー）
SKIP_TICKERS = {
    'SLIM_SP500', 'SLIM_ORCAN', 'MNXACT',
    'IFREE_FANGPLUS', 'NOMURA_SEMI',
    'GS_MMF_USD',
    'CASH_JPY', 'CASH_USD', 'CASH_JPY_SBI', 'CASH_JPY_SBI_WIFE',
    # AVGO は AVGO_toku / AVGO_ippan の 2 キーで保有。
    # 実ティッカー "AVGO" として別途取得されるためここではスキップ。
    'AVGO_toku', 'AVGO_ippan',
}


# ============================================================
# SQLite 初期化
# ============================================================

def init_db():
    """データベースとテーブルを初期化する。"""
    con = _connect()
    cur = con.cursor()

    # トレード履歴
    cur.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            action      TEXT NOT NULL,   -- buy / sell
            shares      REAL NOT NULL,
            price       REAL NOT NULL,
            pnl_jpy     REAL DEFAULT 0,
            account     TEXT,
            strategy    TEXT,
            investment_type TEXT DEFAULT 'swing',
            note        TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')

    # 日次パフォーマンス
    cur.execute('''
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
            created_at      TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')
    # Codex P1 #4: 既存 DB に estimated 列を追加 (idempotent)。実スナップショットは 0。
    try:
        cur.execute("ALTER TABLE daily_performance ADD COLUMN estimated INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # ガードレール状態履歴
    cur.execute('''
        CREATE TABLE IF NOT EXISTS guard_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            trading_allowed INTEGER,
            entry_allowed   INTEGER,
            daily_pnl_pct   REAL,
            monthly_pnl_pct REAL,
            alerts          TEXT,   -- JSON
            created_at      TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')

    # 価格スナップショット（日次終値）
    cur.execute('''
        CREATE TABLE IF NOT EXISTS price_snapshots (
            date        TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            close       REAL,
            currency    TEXT,
            PRIMARY KEY (date, ticker)
        )
    ''')

    con.commit()
    con.close()
    print(f'DB初期化完了: {DB_PATH}')


# ============================================================
# OHLCVデータ取得・保存（Parquet）
# ============================================================

def _close_series(frame: pd.DataFrame) -> pd.Series:
    close = frame['Close']
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return pd.to_numeric(close, errors='coerce').dropna()


def detect_price_sanity_flags(
    ticker: str,
    frame: pd.DataFrame,
    *,
    threshold: float = 0.30,
) -> list[dict]:
    """Flag single-source daily moves that need split/merge or source review."""
    close = _close_series(frame)
    changes = close.pct_change().dropna()
    flags = []
    for idx, change in changes[changes.abs() > threshold].items():
        date_iso = pd.Timestamp(idx).strftime('%Y-%m-%d')
        payload = f'{ticker}|{date_iso}|{float(change):.12f}'
        flags.append({
            'flag_id': hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24],
            'ticker': ticker,
            'date': date_iso,
            'daily_change_pct': round(float(change) * 100.0, 4),
            'threshold_pct': threshold * 100.0,
            'status': 'review_required',
            'source': 'yfinance',
            'single_source_dependency': True,
            'reason': 'absolute daily move exceeds sanity boundary; verify corporate action or alternate source',
        })
    return flags


def append_price_sanity_flags(
    flags: list[dict],
    *,
    path: Path = PRICE_SANITY_LOG,
) -> int:
    if not flags:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                seen.add(json.loads(line).get('flag_id'))
            except (json.JSONDecodeError, AttributeError):
                continue
    new_rows = [row for row in flags if row.get('flag_id') not in seen]
    if new_rows:
        with path.open('a', encoding='utf-8') as handle:
            for row in new_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    return len(new_rows)


def append_price_sanity_review(
    *,
    flag_id: str,
    ticker: str,
    status: str,
    resolution: str,
    reviewer: str,
    evidence: list[dict],
    path: Path = PRICE_SANITY_LOG,
) -> dict:
    """Append a review decision for an existing price sanity flag."""
    row = {
        'flag_id': flag_id,
        'ticker': ticker,
        'status': status,
        'resolution': resolution,
        'reviewer': reviewer,
        'reviewed_at': datetime.now().isoformat(timespec='seconds'),
        'evidence': evidence,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    return row


def _save_ohlcv_with_sanity(ticker: str, frame: pd.DataFrame, path: Path) -> None:
    append_price_sanity_flags(detect_price_sanity_flags(ticker, frame))
    frame.to_parquet(path)

def fetch_and_save_ohlcv(
    tickers: list,
    period: str = '1y',
    force: bool = False,
) -> dict:
    """
    yfinanceからOHLCVを取得してParquetに保存する。
    既存ファイルがあれば差分取得のみ行う。

    Args:
        tickers: ティッカーリスト
        period:  初回取得期間（'1y', '2y', '5y'等）
        force:   Trueなら強制上書き

    Returns:
        {ticker: DataFrame} の辞書
    """
    results = {}

    for ticker in tickers:
        if ticker in SKIP_TICKERS:
            continue

        parquet_path = OHLCV_DIR / f'{ticker.replace("/", "_")}.parquet'

        if parquet_path.exists() and not force:
            # 差分取得: 最終日の翌日から今日まで
            existing = pd.read_parquet(parquet_path)
            last_date = existing.index.max()
            start = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
            today = date.today().strftime('%Y-%m-%d')

            if start >= today:
                results[ticker] = existing
                continue

            new_data = yf.download(ticker, start=start, progress=False, auto_adjust=True)
            if not new_data.empty:
                combined = pd.concat([existing, new_data])
                combined = combined[~combined.index.duplicated(keep='last')]
                _save_ohlcv_with_sanity(ticker, combined, parquet_path)
                results[ticker] = combined
                print(f'  {ticker}: +{len(new_data)}件追加')
            else:
                results[ticker] = existing
        else:
            # 初回取得
            data = yf.download(ticker, period=period, progress=False, auto_adjust=True)
            if not data.empty:
                _save_ohlcv_with_sanity(ticker, data, parquet_path)
                results[ticker] = data
                print(f'  {ticker}: {len(data)}件取得')
            else:
                print(f'  {ticker}: データなし')

    return results


def load_ohlcv(ticker: str) -> Optional[pd.DataFrame]:
    """保存済みParquetを読み込む。"""
    path = OHLCV_DIR / f'{ticker.replace("/", "_")}.parquet'
    if path.exists():
        return pd.read_parquet(path)
    return None


def get_returns(ticker: str, period_days: int = 252) -> Optional[pd.Series]:
    """日次リターン系列を返す。risk_engineへの入力用。"""
    df = load_ohlcv(ticker)
    if df is None or df.empty:
        return None
    close = df['Close'].dropna()
    if hasattr(close.columns, '__len__'):   # MultiIndex対策
        close = close.iloc[:, 0]
    returns = close.pct_change().dropna()
    return returns.tail(period_days)


# ============================================================
# SQLite: トレード記録
# ============================================================

def record_trade(
    ticker:          str,
    action:          str,       # 'buy' or 'sell'
    shares:          float,
    price:           float,
    pnl_jpy:         float = 0,
    account:         str   = '',
    strategy:        str   = '',
    investment_type: str   = 'swing',
    note:            str   = '',
    trade_date:      Optional[str] = None,
) -> int:
    """トレードをSQLiteに記録する。記録したIDを返す。"""
    con = _connect()
    cur = con.cursor()
    cur.execute(
        '''INSERT INTO trades
           (date, ticker, action, shares, price, pnl_jpy, account, strategy, investment_type, note)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (
            trade_date or date.today().isoformat(),
            ticker, action, shares, price, pnl_jpy,
            account, strategy, investment_type, note,
        )
    )
    row_id = cur.lastrowid
    con.commit()
    con.close()
    return row_id


def get_trade_history(
    ticker: Optional[str] = None,
    days:   int = 365,
) -> pd.DataFrame:
    """トレード履歴をDataFrameで返す。"""
    con = _connect()
    since = (date.today() - timedelta(days=days)).isoformat()
    if ticker:
        df = pd.read_sql(
            'SELECT * FROM trades WHERE ticker=? AND date>=? ORDER BY date DESC',
            con, params=(ticker, since)
        )
    else:
        df = pd.read_sql(
            'SELECT * FROM trades WHERE date>=? ORDER BY date DESC',
            con, params=(since,)
        )
    con.close()
    return df


# ============================================================
# SQLite: 日次パフォーマンス記録
# ============================================================

def record_daily_performance(
    portfolio_value: float,
    daily_pnl_jpy:   float,
    daily_pnl_pct:   float,
    monthly_pnl_jpy: float,
    monthly_pnl_pct: float,
    drawdown_pct:    float = 0,
    fx_rate:         Optional[float] = None,
    record_date:     Optional[str] = None,
):
    if fx_rate is None:
        from utils import get_fx_rate_cached
        fx_rate, _ = get_fx_rate_cached()
    """日次パフォーマンスをSQLiteに記録する。"""
    con = _connect()
    cur = con.cursor()
    cur.execute(
        '''INSERT OR REPLACE INTO daily_performance
           (date, portfolio_value, daily_pnl_jpy, daily_pnl_pct,
            monthly_pnl_jpy, monthly_pnl_pct, drawdown_pct, fx_rate_usdjpy)
           VALUES (?,?,?,?,?,?,?,?)''',
        (
            record_date or date.today().isoformat(),
            portfolio_value, daily_pnl_jpy, daily_pnl_pct,
            monthly_pnl_jpy, monthly_pnl_pct, drawdown_pct, fx_rate,
        )
    )
    con.commit()
    con.close()


def get_performance_history(days: int = 252) -> pd.DataFrame:
    """日次パフォーマンス履歴をDataFrameで返す。risk_engine入力用。"""
    con   = _connect()
    since = (date.today() - timedelta(days=days)).isoformat()
    df    = pd.read_sql(
        'SELECT * FROM daily_performance WHERE date>=? ORDER BY date',
        con, params=(since,)
    )
    con.close()
    if not df.empty:
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
    return df


# ============================================================
# SQLite: 価格スナップショット
# ============================================================

def record_price_snapshot(ticker: str, close: float, currency: str = 'USD'):
    """日次終値をSQLiteに記録する。"""
    con = _connect()
    cur = con.cursor()
    cur.execute(
        'INSERT OR REPLACE INTO price_snapshots (date, ticker, close, currency) VALUES (?,?,?,?)',
        (date.today().isoformat(), ticker, close, currency)
    )
    con.commit()
    con.close()


# ============================================================
# 一括データ更新（crontabから呼び出す）
# ============================================================

def daily_update(holdings_path: Optional[Path] = None):
    """
    保有銘柄のOHLCV更新・終値スナップショット記録。
    crontabで平日17時以降に実行する想定。
    """
    path = holdings_path or (BASE_DIR / 'holdings.json')
    if not path.exists():
        print('holdings.json が見つかりません')
        return

    with open(path, encoding='utf-8') as f:
        holdings = json.load(f)

    # 有効なyfinanceティッカーを抽出
    # key が SKIP_TICKERS でも、マップ先のticker（例: AVGO）は取得対象にする
    tickers = []
    for key, info in holdings.items():
        ticker = info.get('ticker', key)
        if ticker not in SKIP_TICKERS and ticker not in tickers:
            tickers.append(ticker)

    # 持株会
    if '9999.T' not in tickers:
        tickers.append('9999.T')

    # P1-1: リスクモデル用の補助ティッカー（proxy: SLIM_SP500→VOO / SLIM_ORCAN→VT, FX）
    for _aux in ('VOO', 'VT', 'USDJPY=X'):
        if _aux not in tickers:
            tickers.append(_aux)

    scenario_state = {}
    scenario_state_path = BASE_DIR / 'scenario_state.json'
    if scenario_state_path.exists():
        scenario_state = json.loads(scenario_state_path.read_text(encoding='utf-8'))
    from scenario_invariants import active_scenario_action_tickers
    for _scenario_ticker in active_scenario_action_tickers(scenario_state):
        if _scenario_ticker not in SKIP_TICKERS and _scenario_ticker not in tickers:
            tickers.append(_scenario_ticker)

    print(f'OHLCV更新: {len(tickers)}銘柄')
    data = fetch_and_save_ohlcv(tickers)

    # 終値スナップショットをSQLiteに記録
    con = _connect()
    cur = con.cursor()
    for ticker, df in data.items():
        if df is not None and not df.empty:
            close = float(df['Close'].iloc[-1].item() if hasattr(df['Close'].iloc[-1], 'item') else df['Close'].iloc[-1])
            currency = 'JPY' if ticker.endswith('.T') or ticker.isdigit() else 'USD'
            cur.execute(
                'INSERT OR REPLACE INTO price_snapshots (date, ticker, close, currency) VALUES (?,?,?,?)',
                (date.today().isoformat(), ticker, close, currency)
            )
    con.commit()
    con.close()
    print('スナップショット記録完了')


if __name__ == '__main__':
    import sys
    args = sys.argv[1:]

    if not args or args[0] == 'init':
        init_db()

    elif args[0] == 'update':
        # P2-9: ヘルスチェック用ハートビート
        try:
            from utils import heartbeat as _hb
        except Exception:
            _hb = None
        try:
            init_db()
            daily_update()
            if _hb:
                _hb('data_fetcher', 'ok')
        except Exception as _e:
            if _hb:
                _hb('data_fetcher', 'error', str(_e)[:500])
            raise

    elif args[0] == 'fetch' and len(args) > 1:
        tickers = args[1:]
        print(f'{tickers} のOHLCVを取得します...')
        data = fetch_and_save_ohlcv(tickers, period='2y')
        for t, df in data.items():
            if df is not None:
                print(f'  {t}: {len(df)}件 ({df.index[0].date()} 〜 {df.index[-1].date()})')

    else:
        print('使い方:')
        print('  python data_fetcher.py init              # DB初期化')
        print('  python data_fetcher.py update            # 保有銘柄を一括更新')
        print('  python data_fetcher.py fetch NVDA AVGO   # 個別取得')

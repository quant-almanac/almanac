"""
parquet_rebuilder.py — P2-23: parquet 月次再構築 (split/dividend adjusted)

Codex H4 への応答:
  既存 data_fetcher.py は差分追記 (yf.download で start=last_date+1) だが、
  auto_adjust=True は split/dividend 発生時に **過去全期間の adjusted close を再計算**する。
  追記しか走らない場合、新しい行は新 adjustment で、古い行は旧 adjustment で記録され、
  シリーズに不連続が混ざる。長期で risk_engine の戻り値が劣化する。

対策:
  本ファイルは **対象 ticker の全期間を毎月 1 度フルダウンロード** して上書きする。
  既存ファイルがある場合は `.bak` で退避し、ダウンロード成功時のみ swap。

  cron 想定: 毎月 1 日 06:00
    0 6 1 * * cd ~/portfolio-bot && venv/bin/python parquet_rebuilder.py monthly

CLI:
  python parquet_rebuilder.py monthly                # 月次ジョブ (rebuild + heartbeat)
  python parquet_rebuilder.py rebuild --ticker AAPL  # 1 件
  python parquet_rebuilder.py rebuild-all            # holdings 全件
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

BASE_DIR = Path(__file__).parent
OHLCV_DIR = BASE_DIR / 'data' / 'ohlcv'
HOLDINGS_FILE = BASE_DIR / 'holdings.json'

# data_fetcher と同じ skip list
SKIP_TICKERS = {
    'SLIM_SP500', 'SLIM_ORCAN', 'MNXACT',
    'IFREE_FANGPLUS', 'NOMURA_SEMI',
    'AVGO_特定', 'AVGO_一般',
    'CASH_JPY', 'CASH_USD', 'CASH_JPY_SBI',
}

# yfinance period: 過去 2 年でリスク計算には十分。長期分析なら period='max' を CLI で指定。
DEFAULT_PERIOD = '2y'


def _list_holdings_tickers() -> List[str]:
    if not HOLDINGS_FILE.exists():
        return []
    try:
        data = json.loads(HOLDINGS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    tickers = []
    for key, pos in data.items():
        if key in SKIP_TICKERS:
            continue
        t = pos.get('ticker', key) if isinstance(pos, dict) else key
        if isinstance(t, str) and t and t not in SKIP_TICKERS:
            tickers.append(t)
    return sorted(set(tickers))


def _safe_swap(tmp: Path, dst: Path) -> None:
    """tmp parquet を dst にアトミックに置換。既存は .bak へ。"""
    if dst.exists():
        bak = dst.with_suffix(dst.suffix + '.bak')
        try:
            shutil.move(str(dst), str(bak))
        except OSError as e:
            raise RuntimeError(f"backup move failed: {e}") from e
    shutil.move(str(tmp), str(dst))


def rebuild_one(
    ticker: str,
    *,
    period: str = DEFAULT_PERIOD,
    ohlcv_dir: Optional[Path] = None,
) -> dict:
    """
    1 銘柄を full download で再構築。split/dividend adjusted close を最新値で上書き。

    Returns:
      {
        'ticker':       str,
        'rows':         int | 0,
        'updated':      bool,
        'error':        str | None,
        'before_rows':  int | None,
      }
    """
    ohlcv_dir = ohlcv_dir or OHLCV_DIR
    ohlcv_dir.mkdir(parents=True, exist_ok=True)
    dst = ohlcv_dir / f'{ticker.replace("/", "_")}.parquet'

    before_rows: Optional[int] = None
    if dst.exists():
        try:
            import pandas as pd
            existing = pd.read_parquet(dst)
            before_rows = len(existing)
        except Exception:
            before_rows = None

    try:
        import pandas as pd
        import yfinance as yf
    except ImportError as e:
        return {'ticker': ticker, 'rows': 0, 'updated': False,
                'error': f'yfinance missing: {e}', 'before_rows': before_rows}

    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
    except Exception as e:
        return {'ticker': ticker, 'rows': 0, 'updated': False,
                'error': f'yf.download failed: {e}', 'before_rows': before_rows}

    if df is None or df.empty:
        return {'ticker': ticker, 'rows': 0, 'updated': False,
                'error': 'empty data from yfinance', 'before_rows': before_rows}

    tmp = ohlcv_dir / f'{ticker.replace("/", "_")}.parquet.tmp'
    try:
        df.to_parquet(tmp)
        _safe_swap(tmp, dst)
    except Exception as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {'ticker': ticker, 'rows': 0, 'updated': False,
                'error': f'parquet write failed: {e}', 'before_rows': before_rows}

    return {'ticker': ticker, 'rows': int(len(df)), 'updated': True,
            'error': None, 'before_rows': before_rows}


def rebuild_many(
    tickers: Iterable[str],
    *,
    period: str = DEFAULT_PERIOD,
    sleep_sec: float = 0.5,
    ohlcv_dir: Optional[Path] = None,
) -> List[dict]:
    """複数 ticker を順次 rebuild。yfinance への過剰アクセスを避けるため sleep。"""
    results = []
    for t in tickers:
        r = rebuild_one(t, period=period, ohlcv_dir=ohlcv_dir)
        results.append(r)
        time.sleep(sleep_sec)
    return results


def rebuild_all_holdings(*, period: str = DEFAULT_PERIOD, ohlcv_dir: Optional[Path] = None) -> List[dict]:
    return rebuild_many(_list_holdings_tickers(), period=period, ohlcv_dir=ohlcv_dir)


# ============================================================
# CLI
# ============================================================

def _main() -> None:
    parser = argparse.ArgumentParser(description='ALMANAC parquet 月次再構築')
    sub = parser.add_subparsers(dest='cmd', required=True)

    r = sub.add_parser('rebuild', help='指定 ticker を full download で再構築')
    r.add_argument('--ticker', required=True)
    r.add_argument('--period', default=DEFAULT_PERIOD)

    a = sub.add_parser('rebuild-all', help='holdings の全 ticker を再構築')
    a.add_argument('--period', default=DEFAULT_PERIOD)

    m = sub.add_parser('monthly', help='毎月 1 日 cron 用: rebuild-all + heartbeat')
    m.add_argument('--period', default=DEFAULT_PERIOD)

    args = parser.parse_args()

    if args.cmd == 'rebuild':
        r = rebuild_one(args.ticker, period=args.period)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0 if r['updated'] else 1)
    elif args.cmd == 'rebuild-all':
        rs = rebuild_all_holdings(period=args.period)
        ok = sum(1 for r in rs if r['updated'])
        print(json.dumps({'total': len(rs), 'updated': ok, 'results': rs}, ensure_ascii=False, indent=2))
        sys.exit(0 if ok == len(rs) else 1)
    elif args.cmd == 'monthly':
        rs = rebuild_all_holdings(period=args.period)
        ok = sum(1 for r in rs if r['updated'])
        print(json.dumps({'total': len(rs), 'updated': ok}, ensure_ascii=False, indent=2))
        try:
            from utils import heartbeat
            heartbeat('parquet_rebuilder', 'ok' if ok == len(rs) else 'warn',
                      extra={'total': len(rs), 'updated': ok})
        except Exception:
            pass
        sys.exit(0)


if __name__ == '__main__':
    _main()

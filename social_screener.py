#!/usr/bin/env python3
"""
social_screener.py — SNS感情 + オプション異常スクリーニング

データソース:
  - StockTwits API (無料、認証不要)
  - yfinance オプションチェーン（コール/プット比率）
  - Google Finance トレンド（フォールバック）

出力: social_sentiment.json
"""

import json
import math
import os
import tempfile
import time
import requests
import yfinance as yf
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT_FILE = BASE_DIR / 'social_sentiment.json'

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# StockTwits スキャン対象（主要80銘柄）
STOCKTWITS_TICKERS = [
    'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'META', 'GOOGL', 'AMD', 'COIN',
    'PLTR', 'SMCI', 'MSTR', 'ARM', 'NFLX', 'SHOP', 'SNOW', 'CRWD', 'DDOG',
    'UBER', 'RIVN', 'LCID', 'NIO', 'SOFI', 'HOOD', 'RIOT', 'MARA', 'HUT',
    'SOXL', 'TQQQ', 'ARKK', 'BABA', 'JD', 'PDD', 'XPEV', 'LI',
    'AVGO', 'QCOM', 'MU', 'LRCX', 'AMAT', 'PANW', 'ZS', 'OKTA',
    'JPM', 'BAC', 'GS', 'WFC', 'V', 'MA', 'PYPL', 'SQ',
    'XOM', 'CVX', 'OXY', 'SLB', 'GLD', 'GDX', 'USO',
    'SPY', 'QQQ', 'IWM', 'DIA', 'VNQ',
]

# オプション分析対象（流動性高い主要銘柄）
OPTIONS_TICKERS = [
    'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'META', 'GOOGL', 'AMD',
    'SPY', 'QQQ', 'IWM', 'COIN', 'PLTR', 'ARM', 'SMCI', 'NFLX',
    'AVGO', 'CRWD', 'SNOW', 'UBER', 'RIVN', 'MSTR', 'RIOT', 'MARA',
]


def _send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg,
                                 'parse_mode': 'HTML'}, timeout=10)
    except Exception:
        pass


def fetch_stocktwits_sentiment(ticker: str, timeout: int = 8) -> dict | None:
    """
    StockTwits API から感情データ取得（無料、認証不要）
    https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json
    """
    # StockTwits はドット記号を対応: 6762.T → 使えないので日本株はスキップ
    if '.' in ticker:
        return None

    url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
        'Accept': 'application/json',
    }

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code == 429:
            print(f"    StockTwits レートリミット - スキップ: {ticker}")
            time.sleep(5)
            return None
        if response.status_code != 200:
            return None

        data = response.json()
        messages = data.get('messages', [])

        if not messages:
            return None

        # 感情集計
        bullish_count = 0
        bearish_count = 0

        for msg in messages:
            entities = msg.get('entities', {})
            sentiment = entities.get('sentiment', {})
            if sentiment:
                basic = sentiment.get('basic', '')
                if basic == 'Bullish':
                    bullish_count += 1
                elif basic == 'Bearish':
                    bearish_count += 1

        total = bullish_count + bearish_count
        if total == 0:
            # 感情ラベルなしでもメッセージ数は有用
            return {
                'bullish_pct': 50.0,
                'bearish_pct': 50.0,
                'message_count': len(messages),
                'is_trending': data.get('symbol', {}).get('watchlist_count', 0) > 10000,
                'sentiment': 'NEUTRAL',
            }

        bullish_pct = bullish_count / total * 100
        bearish_pct = bearish_count / total * 100

        sentiment = 'NEUTRAL'
        if bullish_pct >= 65:
            sentiment = 'BULLISH'
        elif bearish_pct >= 65:
            sentiment = 'BEARISH'

        watchlist_count = data.get('symbol', {}).get('watchlist_count', 0)

        return {
            'bullish_pct': round(bullish_pct, 1),
            'bearish_pct': round(bearish_pct, 1),
            'message_count': len(messages),
            'is_trending': watchlist_count > 10000,
            'watchlist_count': watchlist_count,
            'sentiment': sentiment,
        }

    except requests.exceptions.Timeout:
        return None
    except Exception:
        return None


def fetch_options_unusual(ticker: str) -> dict | None:
    """
    yfinance のオプションチェーンから異常なコール/プット比率を検出
    """
    try:
        tk = yf.Ticker(ticker)
        option_dates = tk.options

        if not option_dates:
            return None

        # 直近2つの満期を分析（流動性が高い）
        total_call_vol = 0
        total_put_vol = 0
        total_call_oi = 0
        total_put_oi = 0

        for date in option_dates[:2]:
            try:
                chain = tk.option_chain(date)
                calls = chain.calls
                puts = chain.puts

                if calls.empty or puts.empty:
                    continue

                # 出来高とOI集計
                call_vol = calls['volume'].fillna(0).sum()
                put_vol  = puts['volume'].fillna(0).sum()
                call_oi  = calls['openInterest'].fillna(0).sum()
                put_oi   = puts['openInterest'].fillna(0).sum()

                total_call_vol += call_vol
                total_put_vol  += put_vol
                total_call_oi  += call_oi
                total_put_oi   += put_oi
            except Exception:
                pass

        if total_put_vol == 0 and total_call_vol == 0:
            return None

        # コール/プット比率（通常: 0.5-1.5 が正常範囲）
        call_put_ratio = total_call_vol / max(total_put_vol, 1)

        unusual = call_put_ratio > 3.0 or call_put_ratio < 0.3

        if not unusual and (total_call_vol + total_put_vol) < 10000:
            return None  # 流動性低い → スキップ

        if call_put_ratio > 2.0:
            bias = 'CALL_HEAVY'  # 強気オプション活動
        elif call_put_ratio < 0.5:
            bias = 'PUT_HEAVY'   # 弱気オプション活動（ヘッジ or 弱気）
        else:
            bias = 'BALANCED'

        return {
            'ticker': ticker,
            'call_volume': int(total_call_vol),
            'put_volume': int(total_put_vol),
            'call_put_ratio': round(call_put_ratio, 2),
            'call_oi': int(total_call_oi),
            'put_oi': int(total_put_oi),
            'unusual': unusual,
            'bias': bias,
        }

    except Exception:
        return None


def run_social_screen(
    st_tickers: list[str] | None = None,
    opt_tickers: list[str] | None = None,
) -> dict:
    """SNS感情 + オプション異常スクリーニング"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] SNS/オプションスクリーニング開始...")

    from insider_restrictions import filter_allowed_tickers
    st_scan = filter_allowed_tickers(st_tickers or STOCKTWITS_TICKERS)
    opt_scan = filter_allowed_tickers(opt_tickers or OPTIONS_TICKERS)

    # --- StockTwits 感情収集 ---
    print(f"  StockTwits: {len(st_scan)}銘柄スキャン中...")
    stocktwits_data = {}
    top_bullish = []
    top_bearish = []
    trending_tickers = []

    for i, ticker in enumerate(st_scan):
        print(f"  StockTwits [{i+1}/{len(st_scan)}] {ticker}...", end='\r')
        data = fetch_stocktwits_sentiment(ticker)
        if data:
            stocktwits_data[ticker] = data
            if data['sentiment'] == 'BULLISH':
                top_bullish.append((ticker, data['bullish_pct']))
            elif data['sentiment'] == 'BEARISH':
                top_bearish.append((ticker, data['bearish_pct']))
            if data.get('is_trending'):
                trending_tickers.append(ticker)
        time.sleep(1.2)  # StockTwits レートリミット対策（1秒/リクエスト）

    top_bullish.sort(key=lambda x: x[1], reverse=True)
    top_bearish.sort(key=lambda x: x[1], reverse=True)

    print(f"\n  StockTwits 完了: 強気{len(top_bullish)}件 / 弱気{len(top_bearish)}件")

    # --- オプション異常検出 ---
    print(f"  オプション: {len(opt_scan)}銘柄分析中...")
    options_unusual = []

    for i, ticker in enumerate(opt_scan):
        print(f"  オプション [{i+1}/{len(opt_scan)}] {ticker}...", end='\r')
        result = fetch_options_unusual(ticker)
        if result:
            options_unusual.append(result)
        time.sleep(0.8)

    # 異常なもの優先でソート
    options_unusual.sort(key=lambda x: (x['unusual'], x['call_put_ratio']), reverse=True)

    print(f"\n  オプション完了: {len(options_unusual)}件")

    result = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'stocktwits': stocktwits_data,
        'options_unusual': options_unusual[:20],  # 上位20件
        'top_bullish': [t for t, _ in top_bullish[:10]],
        'top_bearish': [t for t, _ in top_bearish[:10]],
        'trending_tickers': trending_tickers[:15],
    }

    def _sanitize(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, bool):
            return bool(obj)
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(i) for i in obj]
        # numpy scalar types
        try:
            import numpy as np
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return None if (np.isnan(obj) or np.isinf(obj)) else float(obj)
        except ImportError:
            pass
        return obj

    tmp_fd, tmp_path = tempfile.mkstemp(dir=OUTPUT_FILE.parent, suffix='.tmp')
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(_sanitize(result), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, OUTPUT_FILE)
    except Exception:
        os.unlink(tmp_path)
        raise

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 完了: social_sentiment.json 保存")

    # SNSセンチメントの Telegram 通知は廃止。詳細は social_sentiment.json / Web UI を参照。
    if len(top_bullish) >= 3:
        print(f"[social_screener] 強気シグナル {len(top_bullish)} 件（通知は UI で確認）")

    return result


if __name__ == '__main__':
    result = run_social_screen()

    print(f"\n=== 結果サマリー ===")
    print(f"StockTwits: {len(result['stocktwits'])}銘柄")
    print(f"強気TOP5: {result['top_bullish'][:5]}")
    print(f"弱気TOP5: {result['top_bearish'][:5]}")
    print(f"オプション異常: {len(result['options_unusual'])}件")
    if result['options_unusual']:
        top = result['options_unusual'][0]
        print(f"  最大異常: {top['ticker']} C/P={top['call_put_ratio']:.2f}x ({top['bias']})")

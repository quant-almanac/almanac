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


SHADOW_HISTORY_FILE = BASE_DIR / 'data' / 'social_sentiment_shadow.jsonl'
SHADOW_SCHEMA_VERSION = 'social_shadow_v1'


def _append_shadow_history(result: dict) -> bool:
    """日次の集計値を append-only の shadow 履歴へ残す。

    ⚠️ social_sentiment.json は毎日上書きされるため、そのままでは
    20-40 営業日の分布を後から見られない。閾値を実データから校正するには
    履歴が要るので、選抜条件を変える「前」から貯め始める。

    保存するのは銘柄ごとの集計値だけで、投稿本文・ユーザー名は含まない
    (unique_author_count は件数のみ)。書き込み失敗で本処理は落とさないが、
    握り潰さず stderr へ残す —— 静かに欠測すると校正時に気づけない。
    """
    import sys as _sys
    try:
        as_of = result.get('generated_at') or datetime.now().strftime('%Y-%m-%d %H:%M')
        stocktwits = result.get('stocktwits') or {}
        requested = result.get('stocktwits_requested')
        if requested is None:
            requested = len(stocktwits)
        succeeded = len(stocktwits)
        failed = max(0, int(requested) - succeeded)

        # ⚠️ collection_run_id で「同じ run の再実行」を識別できるようにする。
        # 無条件 append だと同一 run を 2 回流したときに完全に同じ行が重複し、
        # 校正時に日次分布が歪む (レビューで実測: 2行重複)。
        collection_run_id = f"{as_of}::{result.get('run_id') or 'cron'}"
        # ⚠️ 完了マーカーを持つ run だけを「記録済み」とみなす。header だけ書いて
        # 中断した場合、以前は再実行が「記録済み」と判断して欠けた ticker 行を
        # 永久に補完しなかった (レビューで指摘)。
        existing = _completed_collection_run_ids()
        if collection_run_id in existing:
            print(f"[social_screener] shadow 履歴: {collection_run_id} は記録済み。"
                  f"重複追記をスキップ")
            return True

        header = {
            'schema_version': SHADOW_SCHEMA_VERSION,
            'collection_run_id': collection_run_id,
            'as_of': as_of,
            'kind': 'collection_summary',
            'requested_count': int(requested),
            'succeeded_count': succeeded,
            'failed_count': failed,
            'timeout_count': result.get('stocktwits_timeouts'),
            'rate_limited_count': result.get('stocktwits_rate_limited'),
            'http_error_count': result.get('stocktwits_http_errors'),
        }

        # header は最後に書く (complete marker)。途中で落ちれば marker が無く、
        # 次回の再実行が同じ run を書き直せる。
        rows = []
        for ticker, info in stocktwits.items():
            if not isinstance(info, dict):
                continue
            rows.append({
                'schema_version': SHADOW_SCHEMA_VERSION,
                'collection_run_id': collection_run_id,
                'as_of': as_of,
                'kind': 'ticker',
                'ticker': ticker,
                'fetch_status': info.get('fetch_status', 'ok'),
                'sample_message_count': info.get('sample_message_count', info.get('message_count')),
                'labeled_message_count': info.get('labeled_message_count'),
                'bullish_count': info.get('bullish_count'),
                'bearish_count': info.get('bearish_count'),
                'bullish_pct': info.get('bullish_pct'),
                'unique_author_count': info.get('unique_author_count'),
                'oldest_message_at': info.get('oldest_message_at'),
                'newest_message_at': info.get('newest_message_at'),
                'watchlist_count': info.get('watchlist_count'),
                'is_trending': info.get('is_trending'),
                'source_window': info.get('source_window'),
            })

        # ⚠️ 全銘柄の取得が失敗した日でも header だけは残す。以前は
        # stocktwits が空だと 1 行も書かず、「未実行」と「60件試して全失敗」を
        # 区別できなかった (レビューで指摘)。
        rows.append(header)          # 完了マーカーを最後に

        # ⚠️ 完了マーカーの無い run を再実行すると、前回中断で残った ticker
        # 行が孤立したまま残り、新しい完全な行と重複する (レビューで実測:
        # AAPL が 2 行になった)。この run_id に属す孤立行があれば除去してから
        # 書く。無ければ従来通りの単純追記 (速い経路)。
        SHADOW_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        survivors = _orphaned_run_purged(collection_run_id)
        if survivors is None:
            with open(SHADOW_HISTORY_FILE, 'a', encoding='utf-8') as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + '\n')
                f.flush()
                os.fsync(f.fileno())
        else:
            print(f"[social_screener] shadow 履歴: {collection_run_id} の"
                  f"孤立行を除去して書き直します")
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=SHADOW_HISTORY_FILE.parent, suffix='.tmp')
            try:
                with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                    for line in survivors:
                        f.write(line + '\n')
                    for row in rows:
                        f.write(json.dumps(row, ensure_ascii=False) + '\n')
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, SHADOW_HISTORY_FILE)
            except Exception:
                os.unlink(tmp_path)
                raise
        print(f"[social_screener] shadow 履歴へ {len(rows)} 行追記 "
              f"(summary 1 + ticker {len(rows) - 1}, "
              f"requested={requested} succeeded={succeeded} failed={failed})")
        return True
    except Exception as exc:
        print(f"[social_screener] shadow 履歴の追記に失敗 (本処理は継続): {exc}",
              file=_sys.stderr)
        return False


def _orphaned_run_purged(run_id: str) -> list | None:
    """指定 run_id に属す行を除いた既存行 (生の JSON 文字列) を返す。

    _completed_collection_run_ids() で「完了マーカーが無い」と分かった run に
    ついて、ファイルに既にその run_id の行が残っているか (=前回中断の孤立行)
    を調べる。無ければ None (呼び出し側は単純追記でよい)。あれば、それらを
    取り除いた残り全行を返す (呼び出し側が新しい完全な行と合わせて
    アトミックに書き直す)。
    """
    if not SHADOW_HISTORY_FILE.exists():
        return None
    survivors: list[str] = []
    found = False
    try:
        with open(SHADOW_HISTORY_FILE, encoding='utf-8') as f:
            for raw in f:
                line = raw.rstrip('\n')
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    survivors.append(line)   # 壊れた行は推測で捨てず保持
                    continue
                if row.get('collection_run_id') == run_id:
                    found = True
                    continue
                survivors.append(line)
    except OSError:
        return None
    return survivors if found else None


def _completed_collection_run_ids() -> set:
    """完了マーカー (collection_summary 行) を持つ run の集合。

    ⚠️ 「その run の行が 1 つでもある」ではなく「完了マーカーがある」で判定する。
    header だけ書いて中断したケースを記録済みとみなすと、欠けた ticker 行が
    永久に補完されない (レビューで指摘)。"""
    if not SHADOW_HISTORY_FILE.exists():
        return set()
    out = set()
    try:
        with open(SHADOW_HISTORY_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get('kind') == 'collection_summary' and row.get('collection_run_id'):
                    out.add(row['collection_run_id'])
    except OSError:
        return set()
    return out


def _sample_quality(messages: list, bullish_count: int, bearish_count: int) -> dict:
    """将来の閾値校正に必要な「標本の素性」を記録する。

    ⚠️ 現状の ``message_count`` は StockTwits API が 1 回のリクエストで返す
    1 ページ分の長さ (実測 27-30) であって、24時間の投稿量ではない。それを
    volume と誤解した下流 (social_topic_analyzer) が ``message_count > 200``
    を要求していたため、選抜が 4 ヶ月間ずっと 0 件だった。

    ここでは判定条件を一切変えず、校正に必要な集計値だけを増やす:
      - sample_message_count : 1 ページ標本のサイズ (message_count と同値。
                               名前で「これは総量ではない」と明示する)
      - labeled_message_count: 感情ラベルが付いた投稿数 = bullish_pct の分母。
                               これが無いと「100% Bullish」が 2/2 なのか
                               20/20 なのか区別できない
      - unique_author_count  : 同一人物の連投で盛り上がって見える分を割る
      - oldest/newest_message_at : 標本が何時間分に相当するか (投稿速度)

    ⚠️ 投稿本文・ユーザー名などの個人情報は保存しない。集計値のみ。
    """
    labeled = int(bullish_count) + int(bearish_count)
    authors: set = set()
    stamps: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        user = msg.get('user') or {}
        # 識別子のみ (表示名・本文は保存しない)
        uid = user.get('id') if isinstance(user, dict) else None
        if uid is not None:
            authors.add(uid)
        created = msg.get('created_at')
        if created:
            stamps.append(str(created))
    stamps.sort()
    return {
        'sample_message_count': len(messages),
        'labeled_message_count': labeled,
        'bullish_count': int(bullish_count),
        'bearish_count': int(bearish_count),
        'unique_author_count': len(authors) or None,
        'oldest_message_at': stamps[0] if stamps else None,
        'newest_message_at': stamps[-1] if stamps else None,
        'source_window': 'api_page',   # 24h集計ではない。ページング/公式24h指標は未使用
    }


def fetch_stocktwits_sentiment(ticker: str, timeout: int = 8,
                               stats: dict | None = None) -> dict | None:
    """
    StockTwits API から感情データ取得（無料、認証不要）
    https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json
    """
    # ⚠️ 失敗理由を記録用の辞書へ集計する。以前は全ての失敗を None へ潰して
    # いたため、shadow 履歴の timeout / rate_limited / http_error が常に None
    # になり、「取れなかった日に何が起きたか」が残らなかった (レビューで指摘)。
    if stats is None:
        stats = {}

    def _fail(kind: str):
        stats[kind] = stats.get(kind, 0) + 1
        return None

    # StockTwits はドット記号を対応: 6762.T → 使えないので日本株はスキップ
    if '.' in ticker:
        return _fail('skipped_symbol')

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
            return _fail('rate_limited')
        if response.status_code != 200:
            return _fail('http_error')

        data = response.json()
        messages = data.get('messages', [])

        if not messages:
            return _fail('empty_page')

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
        watchlist_count = data.get('symbol', {}).get('watchlist_count', 0)
        quality = _sample_quality(messages, bullish_count, bearish_count)

        if total == 0:
            # 感情ラベルなしでもメッセージ数は有用
            return {
                'bullish_pct': 50.0,
                'bearish_pct': 50.0,
                'message_count': len(messages),
                'is_trending': watchlist_count > 10000,
                'watchlist_count': watchlist_count,
                'sentiment': 'NEUTRAL',
                **quality,
            }

        bullish_pct = bullish_count / total * 100
        bearish_pct = bearish_count / total * 100

        sentiment = 'NEUTRAL'
        if bullish_pct >= 65:
            sentiment = 'BULLISH'
        elif bearish_pct >= 65:
            sentiment = 'BEARISH'

        return {
            'bullish_pct': round(bullish_pct, 1),
            'bearish_pct': round(bearish_pct, 1),
            'message_count': len(messages),
            'is_trending': watchlist_count > 10000,
            'watchlist_count': watchlist_count,
            'sentiment': sentiment,
            **quality,
        }

    except requests.exceptions.Timeout:
        return _fail('timeout')
    except Exception:
        return _fail('exception')


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
    st_stats: dict = {}          # 失敗理由の内訳 (timeout/429/http/…)
    top_bullish = []
    top_bearish = []
    trending_tickers = []

    for i, ticker in enumerate(st_scan):
        print(f"  StockTwits [{i+1}/{len(st_scan)}] {ticker}...", end='\r')
        data = fetch_stocktwits_sentiment(ticker, stats=st_stats)
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
        # ⚠️ 「何銘柄試して何銘柄取れたか」を残す。取れた分だけ保存していると、
        # 全滅した日が「未実行」と区別できない (レビューで指摘)。
        'stocktwits_requested': len(st_scan),
        'stocktwits_timeouts': st_stats.get('timeout', 0),
        'stocktwits_rate_limited': st_stats.get('rate_limited', 0),
        'stocktwits_http_errors': st_stats.get('http_error', 0),
        'stocktwits_failure_breakdown': dict(st_stats),
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
    shadow_ok = _append_shadow_history(result)

    # ⚠️ social_screener 自身の heartbeat。下流の social_topic は上流欠損でも
    # no_candidates/ok になれるため、収集が止まったことを下流からは検知できない
    # (レビューで指摘)。収集側で直接生存シグナルを出す。
    #
    # ⚠️ shadow 書込み失敗を heartbeat に反映する。以前は stocktwits が
    # 1件でも取れれば ok で、shadow への書込みが (握り潰されて) 失敗していても
    # 監視は緑のままだった (レビューで指摘)。校正用データが欠測しているのに
    # 気づけない状態を watchdog の warn_is_error (Round 36 で設定済み) に
    # 拾わせる。
    try:
        from utils import heartbeat
        succeeded = len(result.get('stocktwits') or {})
        requested = result.get('stocktwits_requested') or 0
        if not succeeded:
            status, error = 'error', 'all stocktwits fetches failed'
        elif not shadow_ok:
            status, error = 'warn', 'shadow history write failed'
        else:
            status, error = 'ok', None
        heartbeat('social_screener', status=status, error=error,
                  extra={'requested': requested, 'succeeded': succeeded,
                         'failure_breakdown': result.get('stocktwits_failure_breakdown'),
                         'shadow_write_ok': shadow_ok})
    except Exception as exc:
        print(f"[social_screener] heartbeat 失敗 (本処理は継続): {exc}", file=__import__('sys').stderr)

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

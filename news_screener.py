#!/usr/bin/env python3
"""
news_screener.py — ニュース感情スクリーニング

データソース:
  - Yahoo Finance per-ticker RSS
  - Reuters/MarketWatch 一般RSSフィード
  - feedparser + FinBERT (transformers) で感情分析

出力: news_signal_candidates.json
"""

import json
import os
import time
import feedparser
import re
import ast
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent

NEWS_OUTPUT_FILE = BASE_DIR / 'news_signal_candidates.json'

# Telegram通知（他スクリーナーと同様）
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# スキャン対象ティッカー（主要銘柄80件）
NEWS_SCAN_TICKERS = [
    # S&P500 大型株
    'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO', 'BRK-B', 'LLY',
    'JPM', 'V', 'UNH', 'XOM', 'MA', 'COST', 'HD', 'JNJ', 'ABBV', 'NFLX',
    # テック/AI
    'AMD', 'ORCL', 'CRM', 'ADBE', 'INTU', 'PANW', 'CRWD', 'SNOW', 'PLTR', 'ARM',
    'SMCI', 'MU', 'LRCX', 'AMAT', 'KLAC', 'ASML',
    # ハイグロース
    'COIN', 'MSTR', 'SHOP', 'MELI', 'UBER', 'ABNB', 'DASH',
    'DDOG', 'ZS', 'OKTA', 'NOW', 'TTD',
    # 金融
    'GS', 'MS', 'BAC', 'WFC', 'SCHW', 'BLK',
    # その他セクター
    'CAT', 'DE', 'RTX', 'NEE', 'LIN', 'GLD',
    # 日本ADR/主要
    'TM', 'SONY', 'NMR', 'MFG',
    # ETF (市場全体感情)
    'SPY', 'QQQ', 'IWM', 'ARKK', 'SOXL',
]


def load_new_listing_tickers(base_dir: Path = BASE_DIR) -> list[str]:
    """Read download_tickers.py NEW_LISTINGS without importing its write-on-import module."""
    path = base_dir / "download_tickers.py"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    match = re.search(r"^NEW_LISTINGS\s*=\s*(\[[\s\S]*?\])", text, flags=re.MULTILINE)
    if not match:
        return []
    try:
        values = ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        ticker = str(value or "").strip().upper()
        if ticker and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
            out.append(ticker)
    return sorted(set(out))


def build_news_scan_tickers(
    tickers: list[str] | None = None,
    *,
    base_dir: Path = BASE_DIR,
) -> list[str]:
    """Return scan universe, extending the default list with manual NEW_LISTINGS."""
    if tickers is not None:
        return tickers
    return sorted(set(NEWS_SCAN_TICKERS) | set(load_new_listing_tickers(base_dir)))

# RSSフィード (一般マーケットニュース)
GENERAL_RSS_FEEDS = [
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("Investing.com", "https://www.investing.com/rss/news.rss"),
    ("CNBC Top", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
]


def _send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import requests
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg,
                                 'parse_mode': 'HTML'}, timeout=10)
    except Exception:
        pass


def _load_finbert():
    """FinBERT モデルのロード（遅延ロード）"""
    try:
        from transformers import pipeline
        sentiment_pipeline = pipeline(
            "sentiment-analysis",
            model="ProsusAI/finbert",
            tokenizer="ProsusAI/finbert",
            device=-1,  # CPU
            truncation=True,
            max_length=512,
        )
        return sentiment_pipeline
    except Exception as e:
        print(f"  FinBERT ロード失敗: {e} → ルールベース感情分析にフォールバック")
        return None


def _rule_based_sentiment(text: str) -> tuple[str, float]:
    """FinBERT が使えない場合のルールベース感情分析"""
    text_lower = text.lower()
    bullish_words = ['surge', 'rally', 'beat', 'record', 'growth', 'profit', 'rises',
                     'gains', 'upgrade', 'outperform', 'buy', 'strong', 'positive',
                     '上昇', '急騰', '好調', '増益', '最高値', '好業績']
    bearish_words = ['fall', 'drop', 'decline', 'miss', 'loss', 'plunge', 'crash',
                     'downgrade', 'underperform', 'sell', 'weak', 'concern', 'risk',
                     '下落', '急落', '低調', '減益', '懸念', '不安']

    bull_count = sum(1 for w in bullish_words if w in text_lower)
    bear_count = sum(1 for w in bearish_words if w in text_lower)

    if bull_count > bear_count:
        score = min(0.5 + (bull_count - bear_count) * 0.1, 1.0)
        return 'positive', score
    elif bear_count > bull_count:
        score = min(0.5 + (bear_count - bull_count) * 0.1, 1.0)
        return 'negative', score
    else:
        return 'neutral', 0.5


def analyze_sentiment(text: str, pipeline) -> tuple[str, float]:
    """テキストの感情分析。FinBERT または ルールベース"""
    if pipeline is not None:
        try:
            result = pipeline(text[:512])[0]
            label = result['label'].lower()  # positive/negative/neutral
            score = result['score']
            return label, score
        except Exception:
            pass
    return _rule_based_sentiment(text)


def fetch_ticker_news(ticker: str, max_articles: int | None = None) -> list[dict]:
    """Yahoo Finance の per-ticker RSS からニュース取得。
    max_articles を省略すると tunable_params: news_articles_per_ticker を使用（fallback 10）。"""
    if max_articles is None:
        try:
            from tunable_params import get as _tp_get
            max_articles = int(_tp_get("news_articles_per_ticker", 10))
        except Exception:
            max_articles = 10
    articles = []
    # Yahoo Finance 銘柄別 RSS
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:max_articles]:
            title = entry.get('title', '')
            summary = entry.get('summary', '')
            published = entry.get('published', '')
            if title:
                articles.append({
                    'title': title,
                    'summary': summary[:200],
                    'source': 'Yahoo Finance',
                    'published': published,
                })
    except Exception:
        pass
    return articles


def fetch_general_news(ticker: str) -> list[dict]:
    """一般 RSS フィードからティッカーに関連するニュースを取得"""
    articles = []
    ticker_clean = ticker.replace('-', '').replace('.T', '')

    for feed_name, url in GENERAL_RSS_FEEDS[:3]:  # 最大3フィード
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:30]:
                title = entry.get('title', '')
                summary = entry.get('summary', entry.get('description', ''))
                # ティッカー名がタイトル/概要に含まれているか
                if (ticker_clean.lower() in title.lower() or
                        ticker_clean.lower() in summary.lower()):
                    articles.append({
                        'title': title,
                        'summary': summary[:200],
                        'source': feed_name,
                        'published': entry.get('published', ''),
                    })
                    if len(articles) >= 5:
                        break
        except Exception:
            pass
    return articles


def screen_news_sentiment(
    tickers: list[str] | None = None,
    min_articles: int = 2,
    min_sentiment_score: int = 55,
) -> dict:
    """
    ニュース感情スクリーニング実行

    Returns: news_signal_candidates.json の構造と同じ dict
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ニューススクリーニング開始...")

    from insider_restrictions import filter_allowed_tickers
    scan_tickers = filter_allowed_tickers(build_news_scan_tickers(tickers))

    # FinBERT ロード（起動時1回）
    print("  FinBERT ロード中...")
    sentiment_pipeline = _load_finbert()
    if sentiment_pipeline:
        print("  FinBERT OK")
    else:
        print("  ルールベース感情分析使用")

    candidates = []
    market_sentiments = []
    trending = []

    for i, ticker in enumerate(scan_tickers):
        print(f"  [{i+1}/{len(scan_tickers)}] {ticker} ニュース取得中...", end='\r')

        # ニュース取得
        articles = fetch_ticker_news(ticker)
        if not articles:
            articles = fetch_general_news(ticker)

        if len(articles) < 1:
            continue

        # 感情分析
        bullish_count = 0
        bearish_count = 0
        neutral_count = 0
        total_score = 0
        top_headlines = []
        sources_used = set()
        last_article_at = ''

        for article in articles[:8]:
            text = article['title'] + ' ' + article.get('summary', '')
            label, score = analyze_sentiment(text, sentiment_pipeline)

            if label in ('positive', 'bullish'):
                bullish_count += 1
                total_score += score * 100
            elif label in ('negative', 'bearish'):
                bearish_count += 1
                total_score -= score * 100
            else:
                neutral_count += 1

            if article['title']:
                top_headlines.append(article['title'][:120])
            sources_used.add(article.get('source', 'Unknown'))
            if article.get('published') and not last_article_at:
                last_article_at = article['published'][:16]

        total_articles = bullish_count + bearish_count + neutral_count
        if total_articles == 0:
            continue

        # 感情スコア (-100 to +100)
        sentiment_score = int(total_score / total_articles) if total_articles > 0 else 0

        market_sentiments.append(sentiment_score)

        # シグナル判定
        signal = 'NEUTRAL'
        if bullish_count >= min_articles and sentiment_score >= min_sentiment_score:
            signal = 'BULLISH'
            trending.append(ticker)
        elif bearish_count >= min_articles and sentiment_score <= -min_sentiment_score:
            signal = 'BEARISH'

        if signal in ('BULLISH', 'BEARISH') or (total_articles >= 3 and abs(sentiment_score) >= 30):
            # 銘柄名取得
            try:
                import yfinance as yf
                name = yf.Ticker(ticker).info.get('shortName', ticker)
            except Exception:
                name = ticker

            candidates.append({
                'ticker': ticker,
                'name': name,
                'sentiment_score': sentiment_score,
                'bullish_count': bullish_count,
                'bearish_count': bearish_count,
                'neutral_count': neutral_count,
                'top_headlines': top_headlines[:3],
                'signal': signal,
                'sources': list(sources_used),
                'last_article_at': last_article_at,
                'total_articles': total_articles,
            })

        time.sleep(0.5)  # レートリミット対策

    # 市場全体の感情
    market_mood_score = int(sum(market_sentiments) / len(market_sentiments)) if market_sentiments else 0
    if market_mood_score >= 30:
        market_mood = 'BULLISH'
    elif market_mood_score <= -30:
        market_mood = 'BEARISH'
    else:
        market_mood = 'NEUTRAL'

    # スコア順ソート（強気優先）
    candidates.sort(key=lambda x: x['sentiment_score'], reverse=True)

    result = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'total_tickers_scanned': len(scan_tickers),
        'candidates': candidates,
        'trending': trending[:10],
        'market_mood': market_mood,
        'market_mood_score': market_mood_score,
    }

    # 保存
    with open(NEWS_OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ニューススクリーニング完了: {len(candidates)}件検出")

    # ニュースセンチメントの Telegram 通知は廃止。詳細は news_signal_candidates.json / Web UI を参照。
    bullish_candidates = [c for c in candidates if c['signal'] == 'BULLISH']
    if bullish_candidates:
        print(f"[news_screener] 強気シグナル {len(bullish_candidates)} 件（通知は UI で確認）")

    return result


if __name__ == '__main__':
    import sys
    from utils import init_yfinance_timeout
    init_yfinance_timeout()

    # コマンドライン引数でティッカー指定可能
    tickers = sys.argv[1:] if len(sys.argv) > 1 else None
    result = screen_news_sentiment(tickers=tickers)

    print(f"\n結果: {len(result['candidates'])}件")
    for c in result['candidates'][:5]:
        print(f"  {c['ticker']}: {c['signal']} (スコア{c['sentiment_score']}, {c['bullish_count']}強気/{c['bearish_count']}弱気)")

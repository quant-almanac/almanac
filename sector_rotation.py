import yfinance as yf
import json
import os

# セクターETFとティッカーのマッピング
SECTOR_ETFS = {
    'テクノロジー': 'XLK',
    'ヘルスケア':   'XLV',
    '金融':         'XLF',
    '消費財':       'XLY',
    '生活必需品':   'XLP',
    '資本財':       'XLI',
    'エネルギー':   'XLE',
    '素材':         'XLB',
    '通信':         'XLC',
    '公益':         'XLU',
    '不動産':       'XLRE',
}

# ティッカーのセクター分類
TICKER_SECTOR = {
    'AAPL':'テクノロジー','MSFT':'テクノロジー','NVDA':'テクノロジー',
    'GOOGL':'テクノロジー','GOOG':'テクノロジー','META':'テクノロジー',
    'ADBE':'テクノロジー','CRM':'テクノロジー','INTU':'テクノロジー',
    'AMD':'テクノロジー','INTC':'テクノロジー','QCOM':'テクノロジー',
    'TXN':'テクノロジー','ADI':'テクノロジー','AMAT':'テクノロジー',
    'PANW':'テクノロジー','ZS':'テクノロジー','CRWD':'テクノロジー',
    'TEAM':'テクノロジー','WDAY':'テクノロジー','GTLB':'テクノロジー',
    'JPM':'金融','BAC':'金融','GS':'金融','MS':'金融',
    'V':'金融','MA':'金融','SPGI':'金融','BLK':'金融',
    'JNJ':'ヘルスケア','UNH':'ヘルスケア','TMO':'ヘルスケア',
    'ABT':'ヘルスケア','ISRG':'ヘルスケア','VRTX':'ヘルスケア',
    'REGN':'ヘルスケア','BIIB':'ヘルスケア','GILD':'ヘルスケア',
    'MRK':'ヘルスケア','AMGN':'ヘルスケア','ILMN':'ヘルスケア',
    'AMZN':'消費財','TSLA':'消費財','HD':'消費財','MCD':'消費財',
    'BKNG':'消費財','SBUX':'消費財','NKE':'消費財','TGT':'消費財',
    'WMT':'生活必需品','PG':'生活必需品','KO':'生活必需品',
    'PEP':'生活必需品','COST':'生活必需品',
    'CAT':'資本財','DE':'資本財','RTX':'資本財','HON':'資本財',
    'UPS':'資本財','ODFL':'資本財','FAST':'資本財',
    'XOM':'エネルギー','CVX':'エネルギー',
    'LIN':'素材',
    'T':'通信','VZ':'通信',
}

def get_sector_strength():
    """各セクターの相対強度を計算（1ヶ月・3ヶ月モメンタム）"""
    results = {}
    spy_hist = yf.Ticker('SPY').history(period='3mo')
    spy_1m = (spy_hist['Close'].iloc[-1] - spy_hist['Close'].iloc[-22]) / spy_hist['Close'].iloc[-22] * 100
    spy_3m = (spy_hist['Close'].iloc[-1] - spy_hist['Close'].iloc[0]) / spy_hist['Close'].iloc[0] * 100

    for sector, etf in SECTOR_ETFS.items():
        try:
            hist = yf.Ticker(etf).history(period='3mo')
            if hist.empty or len(hist) < 22:
                continue
            mom_1m = (hist['Close'].iloc[-1] - hist['Close'].iloc[-22]) / hist['Close'].iloc[-22] * 100
            mom_3m = (hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0] * 100
            # SPY比の相対強度
            rel_1m = mom_1m - spy_1m
            rel_3m = mom_3m - spy_3m
            # 総合スコア
            score = rel_1m * 0.6 + rel_3m * 0.4
            results[sector] = {
                'etf': etf,
                'mom_1m': round(mom_1m, 1),
                'mom_3m': round(mom_3m, 1),
                'rel_1m': round(rel_1m, 1),
                'rel_3m': round(rel_3m, 1),
                'score': round(score, 2),
                'strong': score > 0  # SPYより強いか
            }
        except:
            pass

    # スコア順にソート
    results = dict(sorted(results.items(), key=lambda x: x[1]['score'], reverse=True))
    return results

def get_strong_sectors(top_n=4):
    """強いセクター上位N個を返す"""
    strength = get_sector_strength()
    strong = [s for s, v in strength.items() if v['strong']]
    return strong[:top_n]

def get_ticker_sector(ticker):
    """ティッカーのセクターを返す"""
    return TICKER_SECTOR.get(ticker, '不明')

def filter_by_sector_strength(candidates, top_n=4):
    """強いセクターの候補を優先する"""
    try:
        strong_sectors = get_strong_sectors(top_n)
        print(f"強いセクター: {strong_sectors}")

        # 強いセクターの候補を優先、それ以外は後回し
        priority = []
        others = []
        for c in candidates:
            sector = get_ticker_sector(c.get('ticker', ''))
            c['sector'] = sector
            if sector in strong_sectors or c.get('is_japan'):
                priority.append(c)
            else:
                others.append(c)

        return priority + others
    except Exception as e:
        print(f"セクターフィルター失敗: {e}")
        return candidates

def save_sector_report():
    """セクター強度レポートを保存"""
    strength = get_sector_strength()
    out = os.path.expanduser('~/portfolio-bot/sector_strength.json')
    # bool型をPython標準のboolに変換してシリアライズ
    serializable = {}
    for sector, v in strength.items():
        serializable[sector] = {k: val.item() if hasattr(val, 'item') else val for k, val in v.items()}
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False)
    return strength

if __name__ == "__main__":
    print("セクター強度分析中...")
    strength = get_sector_strength()
    print(f"\n{'セクター':<12} {'1M':>6} {'3M':>6} {'相対1M':>7} {'相対3M':>7} {'スコア':>7}")
    print("-" * 52)
    for sector, v in strength.items():
        mark = "★" if v['strong'] else " "
        print(f"{mark}{sector:<11} {v['mom_1m']:>+6.1f}% {v['mom_3m']:>+6.1f}% {v['rel_1m']:>+7.1f}% {v['rel_3m']:>+7.1f}% {v['score']:>+7.2f}")

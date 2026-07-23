import yfinance as yf
import json
import os

# S&P500・ナスダック100の主要銘柄を直接定義
SP500_MAJOR = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","BRK-B","LLY","AVGO",
    "TSLA","WMT","JPM","V","UNH","XOM","MA","ORCL","COST","HD",
    "PG","JNJ","ABBV","BAC","NFLX","CRM","CVX","MRK","AMD","ADBE",
    "KO","PEP","TMO","ACN","MCD","CSCO","ABT","LIN","DHR","TXN",
    "WFC","PM","NEE","INTU","AMGN","UPS","MS","GS","ISRG","CAT",
    "RTX","SPGI","BLK","SYK","BKNG","VRTX","T","AXP","DE","GILD",
    "PLD","MDT","ADI","REGN","SCHW","PANW","MU","LRCX","KLAC","SNPS",
    "CDNS","MELI","PYPL","CRWD","DDOG","ZS","SNOW","COIN","RBLX","UBER",
    "ABNB","DASH","SHOP","SQ","ROKU","LYFT","HOOD","PATH","U","GTLB"
]

NASDAQ100_EXTRA = [
    "QCOM","AMAT","ASML","MRVL","ON","NXPI","MPWR","ENPH","FSLR",
    "PCAR","ODFL","FAST","PAYX","VRSK","CTAS","CPRT","IDXX","ILMN",
    "BIIB","MRNA","ALGN","DXCM","ZM","TEAM","OKTA","SPLK","WDAY"
]

NIKKEI225_MAJOR = [
    "7203.T","9984.T","6861.T","8306.T","6758.T","9432.T","7974.T",
    "4063.T","8058.T","6954.T","9433.T","4452.T","8316.T","7267.T",
    "6367.T","4519.T","8035.T","9022.T","7751.T","6702.T","4568.T",
    "8411.T","6098.T","9020.T","2914.T","7832.T","4523.T","8766.T",
    "6501.T","7011.T","5108.T","3382.T","9613.T","4661.T","8801.T",
    "6752.T","6503.T","7733.T","4901.T","6971.T","7201.T","6902.T",
    "8031.T","8002.T","6724.T","4543.T","2802.T","9766.T","8830.T"
]

ETF_LIST = [
    "QQQ","SPY","IWM","DIA","XLK","XLF","XLE","XLV","XLI","XLY",
    "SOXL","TQQQ","SPXL","FNGU","ARKK","GLD","TLT","HYG","EEM","EWG",
    "EPOL","IEV","VNQ"
]

# 日本株 ETF (B1)。screener.py は tickers.json["all"] のみ読むため、ここを
# ソースに含めないと download_tickers.py 再実行で JP ETF が消える。
# 1570.T はレバレッジ ETF なので generic universe には入れない。
JP_ETF_LIST = ["1306.T", "1321.T", "1489.T", "1698.T"]

# 新規上場 (IPO) で生成元にまだ入っていない大型株を手動オンボード。
# 自動 IPO 取込が無いため、新規上場はここに足さないとユニバース不可視のままになる。
# SPCX: SpaceX (2026-06-12 NASDAQ 上場、史上最大 IPO、米国時価総額6位)。
# 285A.T: キオクシアHD。東証大型IPOだが日経225生成元にはまだ入らないため手動オンボード。
NEW_LISTINGS = ["SPCX", "285A.T"]

all_tickers = list(set(SP500_MAJOR + NASDAQ100_EXTRA + NIKKEI225_MAJOR + ETF_LIST + JP_ETF_LIST + NEW_LISTINGS))

TICKERS_PATH = os.path.expanduser('~/portfolio-bot/tickers.json')

# 既存 tickers.json を読み込み、専用 universe を破壊しないようにマージする。
# この関数が上書きするのは下記の管理キーのみ。long_term_universe /
# margin_long_universe / short_scan_tickers / russell2000_subset などの
# 手動・別スクリプト由来キーはそのまま保持する。
existing = {}
if os.path.exists(TICKERS_PATH):
    try:
        with open(TICKERS_PATH, encoding="utf-8") as f:
            existing = json.load(f)
        if not isinstance(existing, dict):
            existing = {}
    except Exception:
        existing = {}

MANAGED_KEYS = {"sp500_major", "nasdaq100_extra", "nikkei225_major", "etf_list", "all"}

output = dict(existing)  # 既存キーを保持
output["sp500_major"]    = SP500_MAJOR
output["nasdaq100_extra"] = NASDAQ100_EXTRA
output["nikkei225_major"] = NIKKEI225_MAJOR
# etf_list は JP ETF を含めて拡張（既存に他キーから追加された ETF があれば保持）
output["etf_list"] = sorted(set(ETF_LIST) | set(JP_ETF_LIST) | set(existing.get("etf_list", [])))
# all は全ソース + 既存 all の和集合（他ユニバースの銘柄も維持）
output["all"] = sorted(set(all_tickers) | set(existing.get("all", [])))

# long_term_universe に 1489.T を確実に含める（既存があれば維持して追加）
_ltu = set(existing.get("long_term_universe", []))
_ltu.add("1489.T")
output["long_term_universe"] = sorted(_ltu)

with open(TICKERS_PATH, 'w', encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

_preserved = [k for k in existing if k not in MANAGED_KEYS and k != "long_term_universe"]
print(f"銘柄リスト保存完了: all={len(output['all'])}銘柄 / etf_list={len(output['etf_list'])} / "
      f"JP ETF={JP_ETF_LIST} / 保持キー={_preserved}")

"""
expand_tickers.py — tickers.json をフル S&P 500 + 日経225 へ拡張するワンショットスクリプト

実行: cd ~/portfolio-bot && python expand_tickers.py

入力:
  - 既存 tickers.json (sp500_major / nikkei225_major / etf_list / short_scan_tickers / nasdaq100_extra)
  - long_term_screener.py の WATCHLIST（移行）
  - margin_long_screener.py の DEFAULT_SCAN_TICKERS（移行）
  - Wikipedia: S&P 500 / Nikkei 225 銘柄（pandas.read_html）

出力:
  - tickers.json を上書き保存
  - long_term_meta.json を新規作成（name/sector/note メタデータ）

NOTE:
  - S&P 500 は Wikipedia の頻繁な更新があるため、四半期 1 回程度の再実行が望ましい
  - 日経225 構成銘柄は東証/Wikipedia から取得し .T suffix を付与
  - 失敗時は既存 tickers.json はそのまま保持
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pandas as pd
import requests

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _wiki_html(url: str) -> str:
    """User-Agent 付きで Wikipedia ページを取得"""
    r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
    r.raise_for_status()
    return r.text

BASE_DIR = Path(__file__).parent
TICKERS_FILE = BASE_DIR / "tickers.json"
META_FILE    = BASE_DIR / "long_term_meta.json"

SP500_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NIKKEI_WIKI = "https://en.wikipedia.org/wiki/Nikkei_225"
NIKKEI_OFFICIAL = "https://indexes.nikkei.co.jp/nkave/index/component?idx=nk225"

# Russell 2000 上位 50 銘柄（市場代表的な小型成長株サブセット）
RUSSELL2000_SUBSET = [
    # Tech / Cloud
    "FN", "FOUR", "MSTR", "POWL", "FRPT", "DUOL", "ELF", "AGYS", "EXPI",
    "GOLF", "EXLS", "KRYS", "VRRM", "RUSHA", "INSP", "AAON", "MEDP", "ENS",
    "ATGE", "STRL",
    # Biotech / Healthcare
    "RVMD", "TGTX", "CRNX", "ARWR", "INSM", "SUPN", "OSCR", "ANIP", "GKOS",
    "KAI", "PRG", "PI", "BCC", "WTS", "AWI",
    # Industrials / Energy
    "HALO", "MMSI", "GMS", "PRIM", "CRC", "IDT", "PLMR", "RXST", "ITGR",
    # Consumer / Retail
    "SHAK", "CAVA", "PLNT", "WIX", "BMBL", "OLLI",
]

# 主要 S&P 500 不足銘柄（Wikipedia が取れない場合のバックアップ）
SP500_MANUAL_BACKUP = [
    # ヘルスケア追加
    "UNH", "ELV", "CI", "HUM", "MOH", "BSX", "EW", "ZBH", "BAX", "BDX",
    "HCA", "DGX", "LH", "IQV", "MTD", "WAT", "PKI", "STE",
    # 金融追加
    "C", "USB", "PNC", "TFC", "COF", "BK", "STT", "TRV", "ALL", "MET",
    "PRU", "AIG", "AFL", "CB", "PGR", "MSCI", "ICE", "CME", "MCO", "AON",
    "AJG", "MMC", "WTW",
    # 資本財・サービス追加
    "HON", "GE", "MMM", "ITW", "EMR", "ETN", "ROK", "PH", "SWK", "DOV",
    "FDX", "LUV", "DAL", "AAL", "CSX", "NSC", "UNP", "WM", "RSG", "VRSK",
    "PWR", "URI", "EFX", "EXPD", "CHRW", "UAL",
    # エネルギー追加
    "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "OXY", "BKR", "HAL", "DVN",
    "FANG", "OKE", "WMB", "KMI", "TRGP",
    # 素材・REIT追加
    "APD", "SHW", "ECL", "FCX", "NEM", "DOW", "DD", "PPG", "ALB", "NUE",
    "CTVA", "EMN", "MOS",
    "EQIX", "AMT", "CCI", "PSA", "WELL", "SPG", "DLR", "O", "VTR", "ARE",
    "EXR", "AVB", "EQR", "ESS", "MAA", "UDR", "INVH",
    # 公共・通信
    "DUK", "SO", "AEP", "SRE", "D", "PEG", "EXC", "XEL", "ED", "PCG",
    "EIX", "WEC", "ES", "AWK", "FE", "CMS", "DTE", "ETR",
    "VZ", "CMCSA", "DIS", "TMUS", "CHTR", "PARA", "WBD",
    # 一般消費財・必需品
    "LOW", "TJX", "DG", "DLTR", "BBY", "TGT", "ULTA", "ROST", "AZO", "ORLY",
    "GM", "F", "HMC", "CMG", "YUM", "QSR", "DPZ",
    "MDLZ", "CL", "KMB", "CHD", "CLX", "GIS", "K", "HSY", "KHC", "MNST",
    "STZ", "SJM", "TAP", "TSN", "ADM", "BG", "CAG", "HRL", "CPB", "TYL",
    # その他テック追加
    "INTC", "IBM", "HPQ", "DELL", "STX", "WDC", "JNPR", "FFIV", "NTAP",
    "ADSK", "ANSS", "CDNS", "FTNT", "ROP", "FICO", "EPAM", "GLW", "TER",
]

# 日経 225 主要銘柄（Wikipedia で取れない場合のバックアップ — 主要 100 銘柄程度）
NIKKEI225_MANUAL_BACKUP = [
    # 自動車・輸送
    "7203.T", "7267.T", "7269.T", "7270.T", "7272.T", "7201.T", "7211.T",
    "7202.T", "7261.T", "7259.T", "9101.T", "9104.T", "9107.T",
    "9020.T", "9021.T", "9022.T", "9201.T", "9202.T",
    # 電機・精密
    "6758.T", "6861.T", "6098.T", "6367.T", "6501.T", "6503.T", "6506.T",
    "6645.T", "6701.T", "6702.T", "6724.T", "6752.T", "6762.T", "6770.T",
    "6857.T", "6902.T", "6920.T", "6954.T", "6971.T", "6976.T", "7011.T",
    "7733.T", "7741.T", "7751.T", "7752.T", "7832.T",
    # 化学・薬品
    "4063.T", "4452.T", "4502.T", "4503.T", "4506.T", "4507.T", "4519.T",
    "4523.T", "4530.T", "4543.T", "4568.T", "4578.T", "4661.T", "4684.T",
    "4686.T", "4751.T", "4755.T", "4901.T", "4902.T", "4911.T",
    "4151.T", "4188.T", "4205.T", "4208.T", "4324.T",
    # 金融
    "8001.T", "8002.T", "8031.T", "8035.T", "8053.T", "8058.T", "8267.T",
    "8306.T", "8316.T", "8411.T", "8591.T", "8601.T", "8604.T", "8628.T",
    "8630.T", "8725.T", "8750.T", "8766.T", "8801.T", "8802.T", "8830.T",
    # 商社・小売
    "2914.T", "3382.T", "3086.T", "3099.T", "3092.T", "9843.T", "9983.T",
    "8267.T", "9962.T", "9831.T", "8233.T", "3088.T",
    # その他
    "1605.T", "1721.T", "1801.T", "1802.T", "1803.T", "1808.T", "1812.T",
    "1925.T", "1928.T", "1963.T", "2002.T", "2269.T", "2282.T", "2502.T",
    "2503.T", "2531.T", "2768.T", "2801.T", "2802.T", "2871.T", "3289.T",
    "3401.T", "3402.T", "3405.T", "3407.T", "3436.T", "3861.T", "3863.T",
    "5019.T", "5020.T", "5101.T", "5108.T", "5201.T", "5202.T", "5214.T",
    "5232.T", "5233.T", "5301.T", "5332.T", "5333.T", "5401.T", "5406.T",
    "5411.T", "5541.T", "5631.T", "5703.T", "5706.T", "5707.T", "5711.T",
    "5713.T", "5714.T", "5801.T", "5802.T", "5803.T", "6113.T", "6178.T",
    "6301.T", "6302.T", "6305.T", "9999.T", "6361.T", "6471.T", "6472.T",
    "6473.T", "6479.T", "6504.T", "6526.T", "6594.T", "6645.T", "6674.T",
    "6701.T", "6841.T", "6952.T", "6963.T", "6988.T",
    "9001.T", "9005.T", "9007.T", "9008.T", "9009.T",
    "9301.T", "9302.T", "9412.T", "9432.T", "9433.T", "9434.T",
    "9501.T", "9502.T", "9503.T", "9531.T", "9532.T",
    "9602.T", "9613.T", "9735.T", "9766.T", "9983.T", "9984.T",
]


def _fetch_sp500_from_wiki() -> list[str]:
    """Wikipedia から S&P 500 銘柄を取得"""
    try:
        html = _wiki_html(SP500_WIKI)
        tables = pd.read_html(io.StringIO(html))
        df = tables[0]
        symbols = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        symbols = [s.strip() for s in symbols if s.strip() and s.strip() != "nan"]
        print(f"  ✓ Wikipedia S&P 500: {len(symbols)} 銘柄取得")
        return symbols
    except Exception as e:
        print(f"  ⚠️ Wikipedia S&P 500 取得失敗: {e} — バックアップ使用")
        return SP500_MANUAL_BACKUP


def _fetch_nikkei225_from_wiki() -> list[str]:
    """Nikkei 公式インデックスページから日経225銘柄コードを取得し .T を付与。
    Wikipedia の英語ページには components 表が無いため、
    indexes.nikkei.co.jp/nkave/index/component を一次ソースとする。
    """
    import re
    try:
        r = requests.get(NIKKEI_OFFICIAL, headers={"User-Agent": _UA}, timeout=30)
        r.raise_for_status()
        # ページ内の 4 桁コードを全抽出（components 表のセル値として展開されている）
        codes = re.findall(r"\b(\d{4})\b", r.text)
        # 同一コードの重複除去・順序維持
        seen, uniq = set(), []
        for c in codes:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        if len(uniq) >= 200:
            # ノイズ除去：年号 (19xx/20xx) は除外
            uniq = [c for c in uniq if not (c.startswith(("19", "20")) and 1900 <= int(c) <= 2099)]
            symbols = [f"{c}.T" for c in uniq]
            print(f"  ✓ Nikkei 公式: {len(symbols)} 銘柄取得")
            return symbols
        print(f"  ⚠️ Nikkei 公式の銘柄数 {len(uniq)} 不足 — バックアップ使用")
        return NIKKEI225_MANUAL_BACKUP
    except Exception as e:
        print(f"  ⚠️ Nikkei 公式取得失敗: {e} — バックアップ使用")
        return NIKKEI225_MANUAL_BACKUP


def _load_long_term_watchlist() -> dict[str, dict]:
    """long_term_screener.py の WATCHLIST を import で抽出"""
    try:
        spec = importlib.util.spec_from_file_location("lts", BASE_DIR / "long_term_screener.py")
        module = importlib.util.module_from_spec(spec)  # type: ignore
        spec.loader.exec_module(module)  # type: ignore
        watchlist = getattr(module, "WATCHLIST", {})
        print(f"  ✓ long_term WATCHLIST: {len(watchlist)} 銘柄")
        return watchlist
    except Exception as e:
        print(f"  ⚠️ WATCHLIST 読込失敗: {e}")
        return {}


def _load_margin_long_tickers() -> list[str]:
    """margin_long_screener.py の DEFAULT_SCAN_TICKERS を抽出"""
    try:
        spec = importlib.util.spec_from_file_location("mls", BASE_DIR / "margin_long_screener.py")
        module = importlib.util.module_from_spec(spec)  # type: ignore
        spec.loader.exec_module(module)  # type: ignore
        tickers = getattr(module, "DEFAULT_SCAN_TICKERS", [])
        print(f"  ✓ margin_long DEFAULT_SCAN_TICKERS: {len(tickers)} 銘柄")
        return tickers
    except Exception as e:
        print(f"  ⚠️ DEFAULT_SCAN_TICKERS 読込失敗: {e}")
        return []


def main() -> int:
    print("📊 tickers.json 拡張開始 ...")

    # 既存 tickers.json
    if not TICKERS_FILE.exists():
        print(f"❌ {TICKERS_FILE} が見つかりません")
        return 1
    with open(TICKERS_FILE) as f:
        existing = json.load(f)
    print(f"  📂 既存: sp500_major={len(existing.get('sp500_major',[]))} "
          f"nikkei225_major={len(existing.get('nikkei225_major',[]))} "
          f"all={len(existing.get('all',[]))}")

    # ── データ取得 ─────────────────────────
    # NOTE: sp500/nikkei は「公式構成銘柄リスト」として上書き。
    # 旧データを残すと delisted や入替後の古い銘柄が混入し、yfinance 401/404 を増やすため。
    print("\n[1/4] S&P 500 取得 ...")
    sp500_merged = _fetch_sp500_from_wiki()

    print("\n[2/4] Nikkei 225 取得 ...")
    nikkei_merged = _fetch_nikkei225_from_wiki()

    print("\n[3/4] WATCHLIST / DEFAULT_SCAN_TICKERS 移行 ...")
    watchlist = _load_long_term_watchlist()
    long_term_universe = list(watchlist.keys())
    margin_long_universe = _load_margin_long_tickers()

    print("\n[4/4] tickers.json 構築 ...")
    new_tickers = {
        "sp500_major":          sp500_merged,
        "nasdaq100_extra":      existing.get("nasdaq100_extra", []),
        "nikkei225_major":      nikkei_merged,
        "etf_list":             existing.get("etf_list", []),
        "short_scan_tickers":   existing.get("short_scan_tickers", []),
        "russell2000_subset":   RUSSELL2000_SUBSET,
        "long_term_universe":   long_term_universe,
        "margin_long_universe": margin_long_universe,
    }
    # all を再生成（union, 順序維持）
    seen: set[str] = set()
    all_tickers: list[str] = []
    for key in ("sp500_major", "nasdaq100_extra", "nikkei225_major", "etf_list",
                "short_scan_tickers", "russell2000_subset", "long_term_universe",
                "margin_long_universe"):
        for t in new_tickers[key]:
            if t and t not in seen:
                seen.add(t)
                all_tickers.append(t)
    new_tickers["all"] = all_tickers

    # 保存
    with open(TICKERS_FILE, "w", encoding="utf-8") as f:
        json.dump(new_tickers, f, indent=2, ensure_ascii=False)
    print(f"\n✅ tickers.json 更新完了:")
    for k, v in new_tickers.items():
        print(f"   {k:25} {len(v):4} 銘柄")

    # long_term_meta.json 生成
    if watchlist:
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(watchlist, f, indent=2, ensure_ascii=False)
        print(f"✅ long_term_meta.json 生成: {len(watchlist)} エントリ")
    else:
        print(f"⚠️ long_term_meta.json はスキップ（WATCHLIST 取得失敗）")

    return 0


if __name__ == "__main__":
    sys.exit(main())

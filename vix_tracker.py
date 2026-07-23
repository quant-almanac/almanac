"""
ALMANAC - VIX・ボラティリティ・マクロ市場指標トラッカー
yfinance から VIX / 原油 / 金利 / セクターETF を一括取得し、
vix_state.json にキャッシュ（TTL: 15分）。
"""

import sys
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np

from utils import load_json, atomic_write_json, init_yfinance_timeout
from vix_classification import classify_vix

BASE_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / "vix_state.json"
CACHE_TTL = 60 * 15  # 15分

# ── ティッカー定義 ──────────────────────────────────────
VIX_TICKER = "^VIX"
VIX3M_TICKER = "^VIX3M"
OIL_TICKER = "CL=F"
TNX_TICKER = "^TNX"       # 10年債利回り
IRX_TICKER = "^IRX"       # 3ヶ月 T-bill（2Y のプロキシ）
SPY_TICKER = "SPY"
TYX_TICKER = "^TYX"        # 30年債利回り
DXY_TICKER = "DX-Y.NYB"   # ドル指数
USDCNY_TICKER = "USDCNY=X" # 人民元
HG_TICKER = "HG=F"         # 銅先物
CPC_TICKER = "^CPC"        # Put/Callレシオ
HYG_TICKER = "HYG"         # HYG (HYスプレッドプロキシ)

SECTOR_ETFS = ["XLK", "XLE", "XLF", "XLV", "XLI", "XLP", "XLU"]

# 1mo データ取得対象（VIX〜セクターETF）
BATCH_TICKERS_1MO = [
    VIX_TICKER, VIX3M_TICKER, OIL_TICKER,
    TNX_TICKER, IRX_TICKER, SPY_TICKER,
    TYX_TICKER, DXY_TICKER, USDCNY_TICKER, HG_TICKER, HYG_TICKER,
] + SECTOR_ETFS

# Fear & Greed スコアの重み
FG_WEIGHT_VIX = 0.40
FG_WEIGHT_TERM = 0.20
FG_WEIGHT_MA50 = 0.20
FG_WEIGHT_DISP = 0.20


# ============================================================
# キャッシュ管理
# ============================================================

def _is_cache_fresh() -> bool:
    """キャッシュが TTL 内なら True"""
    try:
        if not CACHE_FILE.exists():
            return False
        data = load_json(CACHE_FILE, {})
        cached_at = data.get("cached_at", "")
        if not cached_at:
            return False
        age = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds()
        return age < CACHE_TTL
    except Exception:
        return False


# ============================================================
# ヘルパー: 安全な値取得
# ============================================================

def _safe_last(series):
    """pandas Series から最新の非 NaN 値を取得。失敗時 None。"""
    try:
        s = series.dropna()
        if len(s) == 0:
            return None
        return float(s.iloc[-1])
    except Exception:
        return None


def _safe_pct_change(series, periods: int):
    """periods 日前からの変化率（%）。失敗時 None。"""
    try:
        s = series.dropna()
        if len(s) < periods + 1:
            return None
        return float((s.iloc[-1] / s.iloc[-(periods + 1)] - 1) * 100)
    except Exception:
        return None


def _compute_vix_decay_from_peak(series, window: int = 5) -> float | None:
    """
    直近 `window` 営業日の VIX ピークからの減衰率（%）。
    負値 = ピークから下落（恐怖後退）、正値 = ピーク更新中（恐怖拡大）。
    DCA ラダー T1 条件「VIX > 25 かつピーク -10% 以上減衰」で使う。
    """
    try:
        if series is None:
            return None
        s = series.dropna()
        if len(s) < 2:
            return None
        sub = s.iloc[-window:] if len(s) >= window else s
        peak = float(sub.max())
        last = float(sub.iloc[-1])
        if peak <= 0:
            return None
        return round((last / peak - 1.0) * 100.0, 2)
    except Exception:
        return None


def _classify_vix(level: float | None) -> str:
    """VIX レベルを分類ラベルに変換"""
    return classify_vix(level)


# ============================================================
# データ取得 & 計算
# ============================================================

def _fetch_all() -> dict | None:
    """yfinance から全指標を一括取得して計算結果 dict を返す。"""
    try:
        import yfinance as yf
        init_yfinance_timeout()
    except ImportError:
        print("[vix_tracker] yfinance 未インストール")
        return None

    warnings.filterwarnings("ignore", category=FutureWarning)

    result: dict = {}

    # ── 1mo バッチダウンロード（VIX / 原油 / 金利 / SPY / セクター）──
    try:
        df_1mo = yf.download(
            " ".join(BATCH_TICKERS_1MO),
            period="1mo",
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception as e:
        print(f"[vix_tracker] 1mo バッチ取得エラー: {e}")
        df_1mo = None

    # ── 3mo SPY（MA50 用）──
    try:
        df_spy_3mo = yf.download(
            SPY_TICKER,
            period="3mo",
            progress=False,
        )
    except Exception as e:
        print(f"[vix_tracker] SPY 3mo 取得エラー: {e}")
        df_spy_3mo = None

    def _get_close(df, ticker):
        """バッチ DataFrame から ticker の Close を取得"""
        try:
            if df is None:
                return None
            # group_by="ticker" の場合: df[ticker]["Close"]
            return df[ticker]["Close"]
        except Exception:
            return None

    # ── VIX 指標 ──
    vix_close = _get_close(df_1mo, VIX_TICKER)
    vix_level = _safe_last(vix_close)
    # DCA ラダー: 直近 5 営業日ピークからの減衰率（ピーク減衰はバウンス検知に有効）
    decay_from_peak_5d = _compute_vix_decay_from_peak(vix_close, window=5)
    decay_from_peak_10d = _compute_vix_decay_from_peak(vix_close, window=10)
    result["vix"] = {
        "level": round(vix_level, 2) if vix_level else None,
        "classification": _classify_vix(vix_level),
        "change_1d": _round_or_none(_safe_pct_change(vix_close, 1)),
        "change_5d": _round_or_none(_safe_pct_change(vix_close, 5)),
        # 新規: ピーク減衰（例: -12.5 → 直近 5 日ピークから 12.5% 下がった）
        "decay_from_peak_5d_pct":  decay_from_peak_5d,
        "decay_from_peak_10d_pct": decay_from_peak_10d,
    }

    # ── VIX ターム構造 ──
    vix3m_close = _get_close(df_1mo, VIX3M_TICKER)
    vix3m_level = _safe_last(vix3m_close)
    if vix_level and vix3m_level and vix3m_level != 0:
        ratio = vix_level / vix3m_level
        structure = "backwardation" if ratio > 1.0 else "contango"
    else:
        ratio = None
        structure = "unknown"
    result["vix_term_structure"] = {
        "vix3m": round(vix3m_level, 2) if vix3m_level else None,
        "ratio": round(ratio, 4) if ratio else None,
        "structure": structure,
    }

    # ── 原油（WTI）──
    oil_close = _get_close(df_1mo, OIL_TICKER)
    result["oil"] = {
        "price": _round_or_none(_safe_last(oil_close)),
        "change_1d_pct": _round_or_none(_safe_pct_change(oil_close, 1)),
        "change_5d_pct": _round_or_none(_safe_pct_change(oil_close, 5)),
    }

    # ── 金利 & イールドカーブ ──
    tnx_close = _get_close(df_1mo, TNX_TICKER)
    irx_close = _get_close(df_1mo, IRX_TICKER)
    yield_10y = _safe_last(tnx_close)
    yield_3m = _safe_last(irx_close)

    if yield_10y is not None and yield_3m is not None:
        spread = yield_10y - yield_3m
    else:
        spread = None

    # スプレッドの5日前との変化
    spread_change_5d = None
    try:
        if tnx_close is not None and irx_close is not None:
            tnx_s = tnx_close.dropna()
            irx_s = irx_close.dropna()
            if len(tnx_s) >= 6 and len(irx_s) >= 6:
                spread_now = float(tnx_s.iloc[-1]) - float(irx_s.iloc[-1])
                spread_5d = float(tnx_s.iloc[-6]) - float(irx_s.iloc[-6])
                spread_change_5d = round(spread_now - spread_5d, 4)
    except Exception:
        pass

    result["yields"] = {
        "us_10y": round(yield_10y, 3) if yield_10y else None,
        "us_3m": round(yield_3m, 3) if yield_3m else None,
        "spread_10y_3m": round(spread, 3) if spread is not None else None,
        "yield_curve": "inverted" if (spread is not None and spread < 0) else "normal",
        "spread_change_5d": spread_change_5d,
    }

    # ── 30年債 & 30Y-10Y スプレッド ──
    tyx_close = _get_close(df_1mo, TYX_TICKER)
    yield_30y = _safe_last(tyx_close)
    if yield_30y is not None and yield_10y is not None:
        result["yields"]["us_30y"] = round(yield_30y, 3)
        result["yields"]["spread_30y_10y"] = round(yield_30y - yield_10y, 3)
    else:
        result["yields"]["us_30y"] = None
        result["yields"]["spread_30y_10y"] = None

    # ── SPY & MA50 ──
    spy_close_1mo = _get_close(df_1mo, SPY_TICKER)
    spy_price = _safe_last(spy_close_1mo)

    spy_ma50 = None
    spy_vs_ma50 = None
    try:
        if df_spy_3mo is not None:
            spy_3mo_close = df_spy_3mo["Close"].dropna()
            if len(spy_3mo_close) >= 50:
                spy_ma50 = float(spy_3mo_close.rolling(50).mean().iloc[-1])
                if spy_price and spy_ma50:
                    spy_vs_ma50 = round((spy_price / spy_ma50 - 1) * 100, 2)
    except Exception:
        pass

    result["spy"] = {
        "price": round(spy_price, 2) if spy_price else None,
        "ma50": round(spy_ma50, 2) if spy_ma50 else None,
        "vs_ma50_pct": spy_vs_ma50,
    }

    # SPY 1D/5D 変化率（バブル崩壊シグナル用）
    result["spy"]["change_1d"] = _round_or_none(_safe_pct_change(spy_close_1mo, 1))
    result["spy"]["change_5d"] = _round_or_none(_safe_pct_change(spy_close_1mo, 5))

    # ── セクターフロー（5日リターン、SPY 相対）──
    spy_5d_ret = _safe_pct_change(spy_close_1mo, 5)
    sector_flows = {}
    for etf in SECTOR_ETFS:
        etf_close = _get_close(df_1mo, etf)
        abs_ret = _safe_pct_change(etf_close, 5)
        if abs_ret is not None and spy_5d_ret is not None:
            rel_ret = round(abs_ret - spy_5d_ret, 2)
        else:
            rel_ret = None
        sector_flows[etf] = {
            "return_5d_pct": _round_or_none(abs_ret),
            "vs_spy_5d_pct": rel_ret,
        }
    result["sector_flows"] = sector_flows

    # ── DXY ドル指数 ──
    dxy_close = _get_close(df_1mo, DXY_TICKER)
    result["dxy"] = {
        "level": _round_or_none(_safe_last(dxy_close)),
        "change_1d_pct": _round_or_none(_safe_pct_change(dxy_close, 1)),
        "change_5d_pct": _round_or_none(_safe_pct_change(dxy_close, 5)),
    }

    # ── USD/CNY ──
    usdcny_close = _get_close(df_1mo, USDCNY_TICKER)
    result["usdcny"] = {
        "level": _round_or_none(_safe_last(usdcny_close)),
        "change_5d_pct": _round_or_none(_safe_pct_change(usdcny_close, 5)),
    }

    # ── 銅先物 (HG=F) ──
    hg_close = _get_close(df_1mo, HG_TICKER)
    result["copper"] = {
        "price": _round_or_none(_safe_last(hg_close)),
        "change_5d_pct": _round_or_none(_safe_pct_change(hg_close, 5)),
    }

    # ── Put/Call レシオ ──
    # ^CPC は yfinance 非対応のため VIX ベース推定を使用
    # CBOE P/C 比率と VIX の歴史的相関:
    #   VIX < 20 → ~0.70, VIX 20-25 → ~0.90, VIX 25-30 → ~1.10,
    #   VIX 30-40 → ~1.40, VIX > 40 → ~1.75 (extreme fear; threshold=1.5)
    _vix_lvl = result.get("vix", {}).get("level")
    if _vix_lvl is not None:
        if _vix_lvl < 20:
            result["put_call_ratio"] = 0.70
        elif _vix_lvl < 25:
            result["put_call_ratio"] = 0.90
        elif _vix_lvl < 30:
            result["put_call_ratio"] = 1.10
        elif _vix_lvl < 40:
            result["put_call_ratio"] = 1.40
        else:
            result["put_call_ratio"] = 1.75
    else:
        result["put_call_ratio"] = None

    # ── HY スプレッド推定 (bps) ──
    # FREDが利用可能ならBAMLH0A0HYM2、なければVIXベース推定
    result["hy_spread_bps"] = _estimate_hy_spread(result.get("vix", {}).get("level"))

    # セクター分散度（標準偏差）
    abs_returns = [
        sector_flows[e]["return_5d_pct"]
        for e in SECTOR_ETFS
        if sector_flows[e]["return_5d_pct"] is not None
    ]
    sector_dispersion = round(float(np.std(abs_returns)), 2) if len(abs_returns) >= 3 else None
    result["sector_dispersion"] = sector_dispersion

    # ── Fear & Greed 合成スコア（0=極度の恐怖, 100=極度の楽観）──
    result["fear_greed"] = _calc_fear_greed(
        vix_level=vix_level,
        term_ratio=ratio,
        spy_vs_ma50=spy_vs_ma50,
        sector_dispersion=sector_dispersion,
    )

    return result


def _estimate_hy_spread(vix_level: float | None) -> float | None:
    """
    HYスプレッドをVIXから推定（FREDデータなし時のフォールバック）。
    FRED BAMLH0A0HYM2 と VIX の歴史的相関に基づく粗い推定。
    VIX < 15  → ~300bps, VIX 15-20 → ~350bps, VIX 20-30 → ~450bps,
    VIX 30-40 → ~600bps, VIX > 40 → ~800bps
    精度は低いが方向性は正しい。シナリオ500bps閾値の判定に使用。
    """
    # FRED BAMLH0A0HYM2 を試みる
    try:
        import os
        from fredapi import Fred
        api_key = os.environ.get("FRED_API_KEY", "")
        if api_key:
            fred = Fred(api_key=api_key)
            s = fred.get_series("BAMLH0A0HYM2").dropna()
            if len(s) > 0:
                return round(float(s.iloc[-1]) * 100, 0)  # % → bps
    except Exception:
        pass
    # VIXベース推定
    if vix_level is None:
        return None
    if vix_level < 15:
        return 300.0
    elif vix_level < 20:
        return 350.0
    elif vix_level < 25:
        return 400.0
    elif vix_level < 30:
        return 500.0
    elif vix_level < 40:
        return 650.0
    else:
        return 850.0


def _round_or_none(val, digits=2):
    """None でなければ round して返す"""
    if val is None:
        return None
    return round(val, digits)


def _calc_fear_greed(
    vix_level: float | None,
    term_ratio: float | None,
    spy_vs_ma50: float | None,
    sector_dispersion: float | None,
) -> dict:
    """
    Fear & Greed 合成スコア（0-100）を計算。
    各コンポーネントを 0-100 に正規化してから重み付け平均。
    0 = 極度の恐怖、100 = 極度の楽観
    """
    components = {}
    weighted_sum = 0.0
    total_weight = 0.0

    # (1) VIX レベル → スコア（VIX 低い = 楽観）
    if vix_level is not None:
        # VIX 10→100, VIX 40→0 の線形マッピング
        score = max(0, min(100, (40 - vix_level) / 30 * 100))
        components["vix_score"] = round(score, 1)
        weighted_sum += score * FG_WEIGHT_VIX
        total_weight += FG_WEIGHT_VIX

    # (2) ターム構造 → スコア（コンタンゴ = 楽観）
    if term_ratio is not None:
        # ratio 0.8→100（強コンタンゴ）, ratio 1.2→0（強バックワーデーション）
        score = max(0, min(100, (1.2 - term_ratio) / 0.4 * 100))
        components["term_structure_score"] = round(score, 1)
        weighted_sum += score * FG_WEIGHT_TERM
        total_weight += FG_WEIGHT_TERM

    # (3) SPY vs MA50 → スコア（MA50 上 = 楽観）
    if spy_vs_ma50 is not None:
        # +10% → 100, -10% → 0
        score = max(0, min(100, (spy_vs_ma50 + 10) / 20 * 100))
        components["spy_ma50_score"] = round(score, 1)
        weighted_sum += score * FG_WEIGHT_MA50
        total_weight += FG_WEIGHT_MA50

    # (4) セクター分散度 → スコア
    # Low dispersion = herd behavior (fear in downturns), mid = healthy, high = selective
    if sector_dispersion is not None:
        dispersion_score = max(0.0, min(100.0, (sector_dispersion - 1.5) / 4.0 * 100))
    else:
        dispersion_score = 50.0
    if sector_dispersion is not None:
        components["dispersion_score"] = round(dispersion_score, 1)
        weighted_sum += dispersion_score * FG_WEIGHT_DISP
        total_weight += FG_WEIGHT_DISP

    # 合成
    if total_weight > 0:
        composite = round(weighted_sum / total_weight, 1)
    else:
        composite = None

    # ラベル
    if composite is None:
        label = "UNKNOWN"
    elif composite >= 75:
        label = "EXTREME_GREED"
    elif composite >= 55:
        label = "GREED"
    elif composite >= 45:
        label = "NEUTRAL"
    elif composite >= 25:
        label = "FEAR"
    else:
        label = "EXTREME_FEAR"

    return {
        "score": composite,
        "label": label,
        "components": components,
    }


# ============================================================
# 公開 API
# ============================================================

def get_vix_context() -> dict:
    """
    VIX・ボラティリティ指標辞書を返す。
    キャッシュが 15分以内なら再利用、そうでなければ yfinance から再取得。

    Returns:
        {
          vix:                dict   # level, classification, change_1d/5d
          vix_term_structure: dict   # vix3m, ratio, structure
          oil:                dict   # price, change_1d/5d_pct
          yields:             dict   # us_10y, us_3m, spread, yield_curve
          spy:                dict   # price, ma50, vs_ma50_pct
          sector_flows:       dict   # ETF別 5日リターン・対SPY
          sector_dispersion:  float  # セクター間分散度
          fear_greed:         dict   # score(0-100), label, components
          cached_at:          str    # ISO timestamp
          source:             str    # "yfinance"|"cache"|"error"
        }
    """
    # キャッシュ確認
    if _is_cache_fresh():
        cached = load_json(CACHE_FILE, {})
        cached["source"] = "cache"
        return cached

    # yfinance 取得
    data = _fetch_all()
    if data:
        data["cached_at"] = datetime.now().isoformat()
        atomic_write_json(CACHE_FILE, data)
        data["source"] = "yfinance"
        return data

    # フォールバック: 古いキャッシュがあれば返す
    old_cache = load_json(CACHE_FILE, {})
    if old_cache:
        old_cache["source"] = "stale_cache"
        return old_cache

    # 完全にデータなし
    return {
        "vix": {"level": None, "classification": "UNKNOWN", "change_1d": None, "change_5d": None},
        "vix_term_structure": {"vix3m": None, "ratio": None, "structure": "unknown"},
        "oil": {"price": None, "change_1d_pct": None, "change_5d_pct": None},
        "yields": {"us_10y": None, "us_3m": None, "spread_10y_3m": None, "yield_curve": "unknown", "spread_change_5d": None, "us_30y": None, "spread_30y_10y": None},
        "spy": {"price": None, "ma50": None, "vs_ma50_pct": None, "change_1d": None, "change_5d": None},
        "sector_flows": {},
        "sector_dispersion": None,
        "fear_greed": {"score": None, "label": "UNKNOWN", "components": {}},
        "dxy": {"level": None, "change_1d_pct": None, "change_5d_pct": None},
        "usdcny": {"level": None, "change_5d_pct": None},
        "copper": {"price": None, "change_5d_pct": None},
        "put_call_ratio": None,
        "hy_spread_bps": None,
        "source": "error",
    }


def format_vix_summary(ctx: dict) -> str:
    """ブリーフィング・Telegram 向け短文サマリー"""
    lines = []

    vix = ctx.get("vix", {})
    if vix.get("level") is not None:
        chg = f" ({vix['change_1d']:+.1f}%)" if vix.get("change_1d") is not None else ""
        lines.append(f"VIX: {vix['level']:.1f} [{vix['classification']}]{chg}")

    ts = ctx.get("vix_term_structure", {})
    if ts.get("ratio") is not None:
        lines.append(f"VIX Term: {ts['ratio']:.3f} ({ts['structure']})")

    oil = ctx.get("oil", {})
    if oil.get("price") is not None:
        chg = f" ({oil['change_1d_pct']:+.1f}%)" if oil.get("change_1d_pct") is not None else ""
        lines.append(f"WTI: ${oil['price']:.1f}{chg}")

    yld = ctx.get("yields", {})
    if yld.get("spread_10y_3m") is not None:
        inv = " inverted" if yld["yield_curve"] == "inverted" else ""
        lines.append(f"10Y-3M: {yld['spread_10y_3m']:+.3f}%{inv}")

    fg = ctx.get("fear_greed", {})
    if fg.get("score") is not None:
        lines.append(f"Fear&Greed: {fg['score']:.0f}/100 [{fg['label']}]")

    src = f" [{ctx.get('source', '?')}]"
    return " / ".join(lines) + src if lines else f"VIXデータ未取得{src}"


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    force = "--force" in sys.argv
    if force and CACHE_FILE.exists():
        CACHE_FILE.unlink()
        print("キャッシュ削除 -> 強制再取得")

    ctx = get_vix_context()

    print(f"\n{'='*50}")
    print(f"  ALMANAC VIX Tracker")
    print(f"{'='*50}")
    print(f"source: {ctx.get('source')}")
    print(f"cached_at: {ctx.get('cached_at', 'N/A')}")

    vix = ctx.get("vix", {})
    print(f"\n--- VIX ---")
    print(f"  Level:    {vix.get('level')} [{vix.get('classification')}]")
    print(f"  1D Chg:   {vix.get('change_1d')}%")
    print(f"  5D Chg:   {vix.get('change_5d')}%")

    ts = ctx.get("vix_term_structure", {})
    print(f"\n--- Term Structure ---")
    print(f"  VIX3M:    {ts.get('vix3m')}")
    print(f"  Ratio:    {ts.get('ratio')} ({ts.get('structure')})")

    oil = ctx.get("oil", {})
    print(f"\n--- Oil (WTI) ---")
    print(f"  Price:    ${oil.get('price')}")
    print(f"  1D Chg:   {oil.get('change_1d_pct')}%")
    print(f"  5D Chg:   {oil.get('change_5d_pct')}%")

    yld = ctx.get("yields", {})
    print(f"\n--- Yields ---")
    print(f"  10Y:      {yld.get('us_10y')}%")
    print(f"  3M:       {yld.get('us_3m')}%")
    print(f"  Spread:   {yld.get('spread_10y_3m')}% ({yld.get('yield_curve')})")
    print(f"  Sprd 5D:  {yld.get('spread_change_5d')}")

    spy = ctx.get("spy", {})
    print(f"\n--- SPY ---")
    print(f"  Price:    ${spy.get('price')}")
    print(f"  MA50:     ${spy.get('ma50')}")
    print(f"  vs MA50:  {spy.get('vs_ma50_pct')}%")

    fg = ctx.get("fear_greed", {})
    print(f"\n--- Fear & Greed ---")
    print(f"  Score:    {fg.get('score')}/100 [{fg.get('label')}]")
    comps = fg.get("components", {})
    for k, v in comps.items():
        print(f"    {k}: {v}")

    print(f"\n--- Sector Flows (5D vs SPY) ---")
    for etf, flow in ctx.get("sector_flows", {}).items():
        rel = flow.get("vs_spy_5d_pct")
        abs_r = flow.get("return_5d_pct")
        print(f"  {etf}: {abs_r}% (vs SPY: {rel:+.2f}%)" if rel is not None else f"  {etf}: N/A")

    print(f"  Dispersion: {ctx.get('sector_dispersion')}")

    print(f"\n--- Summary ---")
    print(format_vix_summary(ctx))
    print()

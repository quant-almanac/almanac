"""
ALMANAC v5.0 - マクロ経済指標フェッチャー
FRED API（無料）から金利・インフレ・イールドカーブを取得。
macro_state.json にキャッシュ（TTL: 6時間）。
FRED_API_KEY 未設定時はキャッシュ値にフォールバック。
"""

import json
import os
from pathlib import Path
from datetime import datetime

from vix_classification import vix_macro_status

BASE_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / "macro_state.json"
CACHE_TTL = 3600 * 6  # 6時間（fallback）

def _get_cache_ttl() -> int:
    """tunable_params: macro_cache_hours を優先。なければ CACHE_TTL。"""
    try:
        from tunable_params import get as _tp_get
        v = _tp_get("macro_cache_hours")
        return int(v) * 3600 if v is not None else CACHE_TTL
    except Exception:
        return CACHE_TTL

# FRED シリーズ定義
FRED_SERIES = {
    "fed_rate":     "FEDFUNDS",    # FF金利（%）
    "yield_10y":    "DGS10",       # 10年債利回り（%）
    "yield_2y":     "DGS2",        # 2年債利回り（%）
    # Headline CPI YoY is conventionally reported from the not-seasonally
    # adjusted all-items index.  CPIAUCSL made the stored figure difficult to
    # reconcile with the BLS release.
    "cpi_index":    "CPIAUCNS",
    "unemp_rate":   "UNRATE",      # 失業率（%）
    # ── 2026-04 追加（DCA ラダー発動判定用） ─────────────────
    "hy_oas_bps":   "BAMLH0A0HYM2",   # ICE BofA US HY OAS（%、*100 で bps 換算）
}

# ============================================================
# キャッシュ管理
# ============================================================

def _load_cache() -> dict:
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_at = data.get("cached_at", "")
            if cached_at:
                age = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds()
                if age < _get_cache_ttl():
                    return data
    except Exception:
        pass
    return {}


def _save_cache(data: dict) -> None:
    data["cached_at"] = datetime.now().isoformat()
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ============================================================
# FRED データ取得
# ============================================================

def _fetch_fred() -> dict | None:
    """FRED API から最新値を取得。失敗時は None。"""
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        return None

    try:
        from fredapi import Fred
        fred = Fred(api_key=api_key)

        result = {}
        series_cache = {}
        series_provenance = {}

        # 最新値取得
        for key, series_id in FRED_SERIES.items():
            try:
                s = fred.get_series(series_id).dropna()
                series_cache[key] = s
                result[key] = float(s.iloc[-1])
                observation = s.index[-1]
                observation_date = (
                    observation.date().isoformat()
                    if hasattr(observation, "date")
                    else str(observation)[:10]
                )
                series_provenance[key] = {
                    "source": f"FRED:{series_id}",
                    "observation_date": observation_date,
                }
            except Exception:
                result[key] = None

        # CPI 前年比（%）
        try:
            import pandas as pd

            cpi = series_cache.get("cpi_index")
            if cpi is None or cpi.empty:
                raise ValueError("CPI series unavailable")
            latest_date = pd.Timestamp(cpi.index[-1])
            prior_date = latest_date - pd.DateOffset(years=1)
            if prior_date not in cpi.index:
                result["cpi_yoy"] = None
                series_provenance["cpi_yoy"] = {
                    "source": "FRED:CPIAUCNS",
                    "observation_date": latest_date.date().isoformat(),
                    "comparison_date": prior_date.date().isoformat(),
                    "status": "prior_year_observation_missing",
                }
            else:
                result["cpi_yoy"] = float(
                    (float(cpi.loc[latest_date]) / float(cpi.loc[prior_date]) - 1) * 100
                )
                series_provenance["cpi_yoy"] = {
                    "source": "FRED:CPIAUCNS",
                    "observation_date": latest_date.date().isoformat(),
                    "comparison_date": prior_date.date().isoformat(),
                    "status": "ok",
                }
        except Exception:
            result["cpi_yoy"] = None
        result["series_provenance"] = series_provenance

        # HY OAS は FRED 上 % 単位（5.40=540bps）。bps に統一して保存。
        if result.get("hy_oas_bps") is not None:
            try:
                result["hy_oas_bps"] = round(float(result["hy_oas_bps"]) * 100, 1)
            except Exception:
                result["hy_oas_bps"] = None

        # イールドスプレッド（10Y - 2Y）
        y10 = result.get("yield_10y")
        y2  = result.get("yield_2y")
        if y10 is not None and y2 is not None:
            result["yield_spread"] = round(y10 - y2, 3)
            result["yield_inverted"] = result["yield_spread"] < 0
        else:
            result["yield_spread"] = None
            result["yield_inverted"] = False

        return result

    except Exception as e:
        print(f"[macro_fetcher] FRED取得エラー: {e}")
        return None


# ============================================================
# VIX データ取得
# ============================================================

def _fetch_vix() -> float | None:
    """yfinance から ^VIX の最新値を取得。失敗時は None。"""
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX").fast_info
        return float(vix["lastPrice"])
    except Exception:
        return None


# ============================================================
# Fear & Greed / Put-Call / HY OAS（DCA ラダー用）
# ============================================================

def _fetch_fear_greed() -> int | None:
    """
    CNN Fear & Greed Index を取得。
    一次ソースは非公式 API（https://production.dataviz.cnn.io/index/fearandgreed/graphdata）。
    失敗時は VIX ベースの合成値で代替（後段 _synthesize_fear_greed で処理）。
    """
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        score = data.get("fear_and_greed", {}).get("score")
        if score is None:
            return None
        return int(round(float(score)))
    except Exception:
        return None


def _synthesize_fear_greed(vix: float | None, hy_oas_bps: float | None,
                            put_call: float | None) -> int | None:
    """
    CNN Fear&Greed が取れない場合の代替合成スコア（0-100）。
    高恐怖 = 低スコア。VIX / HY OAS / Put-Call から近似。
    """
    score = 50.0
    if vix is not None:
        # VIX 15 → +20 (greed), 40 → -30 (extreme fear)
        score -= max(-30, min(20, (vix - 20) * -2))
    if hy_oas_bps is not None:
        # 350bps 中立、700bps → -20
        score -= max(-25, min(15, (hy_oas_bps - 350) / 20))
    if put_call is not None:
        # 1.0 中立、1.3 → -15
        score -= max(-20, min(15, (put_call - 1.0) * 50))
    score = max(0.0, min(100.0, score))
    return int(round(score))


def _fetch_put_call_ratio() -> float | None:
    """
    CBOE Total Put/Call Ratio（^CPC）を yfinance 経由で取得。
    1.0 中立、1.2+ で恐怖、0.8- で強気。
    """
    try:
        import yfinance as yf
        t = yf.Ticker("^CPC")
        hist = t.history(period="5d")
        if hist.empty:
            return None
        return round(float(hist["Close"].dropna().iloc[-1]), 3)
    except Exception:
        return None


def _calc_vix_fields(vix: float | None) -> dict:
    """VIX 値からステータスフィールドを計算する。"""
    if vix is None:
        return {
            "vix": None,
            "vix_capitulation": False,
            "vix_fear": False,
            "vix_status": "unknown",
        }
    status = vix_macro_status(vix)
    return {
        "vix": round(vix, 2),
        "vix_capitulation": status == "capitulation",
        "vix_fear": status in {"fear", "capitulation"},
        "vix_status": status,
    }


# ============================================================
# 公開 API
# ============================================================

def get_macro_context() -> dict:
    """
    マクロ指標辞書を返す。FRED_API_KEY 未設定時はキャッシュ or デフォルト値。

    Returns:
        {
          fed_rate:         float|None  # FF金利（%）
          yield_10y:        float|None  # 10年債利回り（%）
          yield_2y:         float|None  # 2年債利回り（%）
          yield_spread:     float|None  # イールドスプレッド（10Y-2Y, %）
          yield_inverted:   bool        # True = 逆イールド
          cpi_yoy:          float|None  # CPI前年比（%）
          unemp_rate:       float|None  # 失業率（%）
          vix:              float|None  # VIX恐怖指数
          vix_capitulation: bool        # True if VIX > 40
          vix_fear:         bool        # True if VIX > 30
          vix_status:       str         # "capitulation"|"fear"|"elevated"|"normal"|"unknown"
          macro_adj:        int         # get_macro_score() への加算値（-3 〜 0）
          source:           str         # "fred"|"cache"|"default"
        }
    """
    # キャッシュ確認
    cached = _load_cache()
    if cached:
        cached["source"] = "cache"
        return cached

    # FRED 取得
    data = _fetch_fred()
    if data:
        vix_val = _fetch_vix()
        data.update(_calc_vix_fields(vix_val))
        # DCA ラダー用: Put/Call, Fear&Greed（CNN→合成フォールバック）
        data["put_call_ratio"] = _fetch_put_call_ratio()
        fg = _fetch_fear_greed()
        if fg is None:
            fg = _synthesize_fear_greed(
                vix=data.get("vix"),
                hy_oas_bps=data.get("hy_oas_bps"),
                put_call=data.get("put_call_ratio"),
            )
            data["fear_greed_source"] = "synthesized"
        else:
            data["fear_greed_source"] = "cnn"
        data["fear_greed"] = fg
        data["macro_adj"] = _calc_adjustment(data)
        _save_cache(data)
        data["source"] = "fred"
        return data

    # FRED なし: VIX だけ取得してデフォルト返却
    vix_val = _fetch_vix()
    result = {
        "fed_rate": None, "yield_10y": None, "yield_2y": None,
        "yield_spread": None, "yield_inverted": False,
        "cpi_yoy": None, "unemp_rate": None,
        "hy_oas_bps": None,
        "macro_adj": 0, "source": "default",
    }
    result.update(_calc_vix_fields(vix_val))
    result["put_call_ratio"] = _fetch_put_call_ratio()
    fg = _fetch_fear_greed()
    if fg is None:
        fg = _synthesize_fear_greed(
            vix=result.get("vix"),
            hy_oas_bps=result.get("hy_oas_bps"),
            put_call=result.get("put_call_ratio"),
        )
        result["fear_greed_source"] = "synthesized"
    else:
        result["fear_greed_source"] = "cnn"
    result["fear_greed"] = fg
    result["macro_adj"] = _calc_adjustment(result)
    return result


def _calc_adjustment(data: dict) -> int:
    """
    マクロ指標からスコア加算値（-3〜0）を計算。
    analyzer.py の macro_score（0-10）に加算して使う。
    """
    adj = 0

    # 逆イールド（不況の先行指標）
    if data.get("yield_inverted"):
        adj -= 2

    # 高インフレ（Fed が利下げできない）
    cpi = data.get("cpi_yoy")
    if cpi is not None:
        if cpi > 4.0:
            adj -= 1
        elif cpi > 3.0:
            adj -= 0  # 軽微、今は加算なし

    # FF金利が高水準（タイト環境）
    fed = data.get("fed_rate")
    if fed is not None and fed > 4.5:
        adj -= 1  # 量的引き締め局面

    # VIX 高恐怖ゾーン（VIX > 40 は capitulation = 買い機会なのでペナルティなし）
    vix = data.get("vix")
    if vix is not None and vix > 30:
        adj -= 1  # 高恐怖ゾーン

    return adj


def format_macro_summary(ctx: dict) -> str:
    """Telegram / ブリーフィング向け短文サマリー"""
    lines = []
    if ctx.get("fed_rate") is not None:
        lines.append(f"FF金利: {ctx['fed_rate']:.2f}%")
    if ctx.get("yield_spread") is not None:
        inv = " ⚠️逆イールド" if ctx.get("yield_inverted") else ""
        lines.append(f"イールドスプレッド(10Y-2Y): {ctx['yield_spread']:+.2f}%{inv}")
    if ctx.get("cpi_yoy") is not None:
        lines.append(f"CPI前年比: {ctx['cpi_yoy']:.1f}%")
    if ctx.get("unemp_rate") is not None:
        lines.append(f"失業率: {ctx['unemp_rate']:.1f}%")
    if ctx.get("vix") is not None:
        vix_status = ctx.get("vix_status", "normal")
        status_label = {
            "capitulation": " 🚨恐慌(買い機会)",
            "fear": " ⚠️高恐怖",
            "elevated": " 注意",
            "normal": "",
        }.get(vix_status, "")
        lines.append(f"VIX: {ctx['vix']:.1f}{status_label}")
    # DCA ラダー指標
    if ctx.get("fear_greed") is not None:
        fg = ctx["fear_greed"]
        label = "極度の恐怖" if fg <= 25 else ("恐怖" if fg <= 45 else ("中立" if fg <= 55 else ("強欲" if fg <= 75 else "極度の強欲")))
        lines.append(f"F&G: {fg}({label})")
    if ctx.get("put_call_ratio") is not None:
        lines.append(f"P/C: {ctx['put_call_ratio']:.2f}")
    if ctx.get("hy_oas_bps") is not None:
        lines.append(f"HY OAS: {ctx['hy_oas_bps']:.0f}bps")
    src = f" [{ctx.get('source', '?')}]"
    return " / ".join(lines) + src if lines else f"マクロデータ未取得{src}"


def classify_panic(ctx: dict) -> dict:
    """
    DCA ラダー発動判定用の「恐怖/恐慌スコア」構造化。
    戻り値:
      {
        "panic_score": 0-100,           # 0=平穏, 100=極度恐怖
        "vix": float|None,
        "fear_greed": int|None,
        "put_call": float|None,
        "hy_oas_bps": float|None,
        "conditions": {                 # 個別条件フラグ（DCA エンジンで AND 評価）
            "vix_above_25": bool,
            "vix_above_40": bool,
            "fg_below_25": bool,
            "pc_above_1_2": bool,
            "hy_above_500": bool,
        }
      }
    """
    vix = ctx.get("vix")
    fg  = ctx.get("fear_greed")
    pc  = ctx.get("put_call_ratio")
    hy  = ctx.get("hy_oas_bps")

    score = 0.0
    if vix is not None:
        score += max(0, min(30, (vix - 15) * 2))
    if fg is not None:
        score += max(0, min(30, (50 - fg) * 0.6))
    if pc is not None:
        score += max(0, min(20, (pc - 0.9) * 50))
    if hy is not None:
        score += max(0, min(20, (hy - 350) / 20))
    panic = int(round(max(0.0, min(100.0, score))))

    return {
        "panic_score": panic,
        "vix": vix,
        "fear_greed": fg,
        "put_call": pc,
        "hy_oas_bps": hy,
        "conditions": {
            "vix_above_25":  (vix is not None and vix > 25),
            "vix_above_40":  (vix is not None and vix > 40),
            "fg_below_25":   (fg is not None and fg <= 25),
            "pc_above_1_2":  (pc is not None and pc > 1.2),
            "hy_above_500":  (hy is not None and hy > 500),
        },
    }


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import sys

    force = "--force" in sys.argv
    if force and CACHE_FILE.exists():
        CACHE_FILE.unlink()
        print("キャッシュ削除 → 強制再取得")

    ctx = get_macro_context()
    print(f"source: {ctx['source']}")
    print(f"FF金利:         {ctx.get('fed_rate')} %")
    print(f"10年債:         {ctx.get('yield_10y')} %")
    print(f"2年債:          {ctx.get('yield_2y')} %")
    print(f"イールドスプレッド: {ctx.get('yield_spread')} % (逆イールド: {ctx.get('yield_inverted')})")
    print(f"CPI前年比:      {ctx.get('cpi_yoy')} %")
    print(f"失業率:          {ctx.get('unemp_rate')} %")
    print(f"VIX:             {ctx.get('vix')} (status: {ctx.get('vix_status')}, capitulation: {ctx.get('vix_capitulation')}, fear: {ctx.get('vix_fear')})")
    print(f"macro_adj:       {ctx.get('macro_adj')}")

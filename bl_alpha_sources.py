"""
bl_alpha_sources.py — P2: Black-Litterman 独立 alpha source

Codex Round 2 の "confidence laundering" 指摘への応答:

  問題:
    既存 _extract_bl_views は Sonnet の (action, urgency) を期待リターンに写像し
    BL に view として注入していた。LLM の主観確率を量子化して同じ LLM が見直す
    循環構造 = "LLM の確信を quant 数字に化粧する" だけで、独立 alpha 源ではない。

  対策:
    BL の View 入力源を以下のうち選択 / 組み合わせ可能にする:
      1. analyst_consensus_alpha  : yfinance reco_mean を期待リターンに写像
      2. momentum_alpha           : 12-1 momentum (Asness 流) → forward return 推定
      3. factor_beta_alpha        : factor_attribution.json から βt を読み、factor premium を掛ける
    これらは全て LLM 出力を経由しない独立 source。

  使い方:
    from bl_alpha_sources import compute_independent_views
    views = compute_independent_views(tickers=['AAPL', '7203.T'], sources=['momentum', 'analyst_consensus'])
    # → 既存 bl_views.json と同一 schema を返す:
    #   {ticker: {bull_view, bear_view, macro_view, mean_view, variance, n_signals, ...}}

  本モジュールは alpha 源を提供するだけで、BL 注入は portfolio_optimizer.py / analyst/__init__.py 側。
  P1-16 で deweight された LLM views は引き続き使えるが、本モジュールの出力は
  独立 source なので Ω を deweight する必要はない (raw variance をそのまま使う)。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional

BASE_DIR = Path(__file__).parent


# ============================================================
# Source 1: Analyst consensus (yfinance reco_mean)
# ============================================================
#
# yfinance Ticker.info / Ticker.recommendations 由来の reco_mean を期待リターンに
# 写像する。reco_mean ∈ [1.0, 5.0] (1=Strong Buy, 5=Strong Sell)。
#
# 写像 (個人投資家向けに保守的なスケール):
#   1.0  →  +0.18 (年率 +18%)
#   2.0  →  +0.10
#   3.0  →   0.00
#   4.0  →  -0.10
#   5.0  →  -0.18
# 線形補間 + clamp。

def _reco_to_view(reco_mean: float) -> float:
    if reco_mean is None or math.isnan(reco_mean):
        return 0.0
    # 線形写像: y = -0.09 * (x - 3.0)
    view = -0.09 * (float(reco_mean) - 3.0)
    return max(-0.18, min(0.18, view))


def analyst_consensus_alpha(
    tickers: Iterable[str],
    *,
    fetcher: Optional[callable] = None,
) -> Dict[str, dict]:
    """
    各 ticker の analyst consensus (reco_mean) を取得し、期待リターンに変換。

    Args:
        fetcher: テスト用。`fetcher(ticker) -> {'reco_mean': float, 'analyst_count': int}` を返す callable。
                 None なら yfinance を実呼出 (実環境向け)。

    Returns:
        {ticker: {'view': float, 'reco_mean': float, 'n_analysts': int, 'source': 'analyst_consensus'}}
    """
    out: Dict[str, dict] = {}

    def _yf_fetcher(t: str) -> dict:
        try:
            import yfinance as yf
            info = yf.Ticker(t).info or {}
            return {
                "reco_mean":     info.get("recommendationMean"),
                "analyst_count": info.get("numberOfAnalystOpinions") or 0,
            }
        except Exception:
            return {"reco_mean": None, "analyst_count": 0}

    f = fetcher or _yf_fetcher

    for t in tickers:
        d = f(t) or {}
        reco = d.get("reco_mean")
        if reco is None:
            continue
        out[t] = {
            "view":       _reco_to_view(float(reco)),
            "reco_mean":  float(reco),
            "n_analysts": int(d.get("analyst_count") or 0),
            "source":     "analyst_consensus",
        }
    return out


# ============================================================
# Source 2: Momentum (12-1 Asness)
# ============================================================
#
# Asness "Momentum Everywhere" (2013) の 12-1 month momentum:
#   過去 12 ヶ月のリターンから直近 1 ヶ月を除く累積リターン。
#   forward return との相関は強いが、過剰な期待値はノイズなので clamp。

def _twelve_minus_one_return(prices: list) -> Optional[float]:
    """
    prices: 直近 252 営業日分の Close (時系列 ASC) を想定。長さ不足なら None。
    return: (P_{-21} / P_{-252}) - 1。直近 1 ヶ月 (≈21 営業日) を除く。
    """
    if not prices or len(prices) < 252:
        return None
    try:
        p_start = float(prices[-252])
        p_end   = float(prices[-22])  # 直近 1 ヶ月除いた末端
        if p_start <= 0:
            return None
        return p_end / p_start - 1.0
    except (TypeError, ValueError, IndexError):
        return None


def momentum_alpha(
    tickers: Iterable[str],
    *,
    price_loader: Optional[callable] = None,
    decay: float = 0.5,
) -> Dict[str, dict]:
    """
    Args:
        price_loader: テスト用。`price_loader(ticker) -> List[float]` を返す。
                      None なら data/ohlcv/{ticker}.parquet から Close を読む。
        decay:        forward return = past_return × decay (経験的に 0.3-0.6 程度)。

    Returns:
        {ticker: {'view': float, 'past_12_1': float, 'source': 'momentum'}}
    """
    def _parquet_loader(t: str) -> list:
        try:
            import pandas as pd
            p = BASE_DIR / "data" / "ohlcv" / f"{t}.parquet"
            if not p.exists():
                return []
            df = pd.read_parquet(p)
            if isinstance(df.columns, pd.MultiIndex):
                close_col = [c for c in df.columns if c[0] == "Close"]
                if not close_col:
                    return []
                close = df[close_col[0]]
            elif "Close" in df.columns:
                close = df["Close"]
            else:
                return []
            return [float(x) for x in close.dropna().tolist()]
        except Exception:
            return []

    loader = price_loader or _parquet_loader
    out: Dict[str, dict] = {}
    for t in tickers:
        prices = loader(t)
        past = _twelve_minus_one_return(prices)
        if past is None:
            continue
        view = past * decay
        # 過剰値を clamp (±25% を超える forward return は信頼しない)
        view = max(-0.25, min(0.25, view))
        out[t] = {
            "view":      view,
            "past_12_1": round(past, 4),
            "source":    "momentum",
        }
    return out


# ============================================================
# Source 3: Factor beta × premium
# ============================================================
#
# factor_attribution.json の β_t (各因子に対するエクスポージャ) と、
# その時点でのファクタープレミアム (long-run history mean) を掛けて
# 期待リターンを推定する。
#
# factor_attribution の出力スキーマ (推定):
#   {ticker: {beta_mom, beta_value, beta_qual, ...}, alpha, ...}
# 本モジュールは ticker 単位ではなく portfolio 全体 1 行のため、portfolio.tilt として 1 つだけ吐く。

# 米国株 long-run premium (年率、保守的なポイント推定):
FACTOR_PREMIUMS = {
    "MOM":  0.05,   # momentum
    "BAB":  0.02,   # betting against beta
    "QMJ":  0.03,   # quality minus junk
    "VAL":  0.02,   # value
    "SMB":  0.01,   # size
}


def factor_beta_alpha(
    factor_attribution: Optional[dict] = None,
    *,
    fa_path: Optional[Path] = None,
) -> Dict[str, dict]:
    """
    factor_attribution.json (portfolio-level) を読み、portfolio tilt 1 行を返す。
    """
    if factor_attribution is None:
        p = fa_path or (BASE_DIR / "factor_attribution.json")
        if not p.exists():
            return {}
        try:
            factor_attribution = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    if not isinstance(factor_attribution, dict):
        return {}
    if factor_attribution.get("error"):
        return {}

    betas = factor_attribution.get("betas") or {}
    if not isinstance(betas, dict):
        return {}

    # 期待リターン推定: sum(beta_f * premium_f) - 過大評価を防ぐため clamp ±0.10
    expected = 0.0
    used: list = []
    for factor, beta in betas.items():
        prem = FACTOR_PREMIUMS.get(str(factor).upper())
        if prem is None or beta is None:
            continue
        try:
            contrib = float(beta) * prem
            expected += contrib
            used.append({"factor": factor, "beta": float(beta), "premium": prem,
                         "contrib": round(contrib, 4)})
        except (TypeError, ValueError):
            continue

    if not used:
        return {}

    expected = max(-0.10, min(0.10, expected))
    return {
        "PORTFOLIO_TILT": {
            "view":         round(expected, 4),
            "decomposition": used,
            "source":       "factor_beta",
        }
    }


# ============================================================
# Aggregate: compute_independent_views
# ============================================================

def compute_independent_views(
    *,
    tickers: Iterable[str],
    sources: Optional[List[str]] = None,
    fetcher_analyst: Optional[callable] = None,
    loader_momentum: Optional[callable] = None,
    factor_attribution: Optional[dict] = None,
) -> Dict[str, dict]:
    """
    複数 alpha source を集約し、bl_views.json と同じ schema で返す。

    schema (per ticker):
      {
        'bull_view':  最も強気な独立 source の view (alpha)
        'bear_view':  最も弱気な独立 source の view
        'macro_view': sources の平均 (中立寄り)
        'mean_view':  3 つの中央値
        'variance':   sources 間の分散 (BL の Ω に直接使える)
        'n_signals':  集約された source 数
        'avg_confidence': 1.0 (独立 source なので deweight しない印)
        'sources':    [{...}, ...]  audit 用
      }

    Args:
        sources: 'analyst_consensus' | 'momentum' | 'factor_beta' のサブセット。
                 None なら全部。

    Returns:
        {ticker: schema_dict}
    """
    sources = sources or ["analyst_consensus", "momentum", "factor_beta"]
    tickers = list(tickers)

    all_views: Dict[str, List[dict]] = {t: [] for t in tickers}

    if "analyst_consensus" in sources:
        for t, v in analyst_consensus_alpha(tickers, fetcher=fetcher_analyst).items():
            all_views.setdefault(t, []).append(v)

    if "momentum" in sources:
        for t, v in momentum_alpha(tickers, price_loader=loader_momentum).items():
            all_views.setdefault(t, []).append(v)

    if "factor_beta" in sources:
        # factor は portfolio tilt のみ
        ft = factor_beta_alpha(factor_attribution=factor_attribution)
        for k, v in ft.items():
            all_views.setdefault(k, []).append(v)

    result: Dict[str, dict] = {}
    for t, vs in all_views.items():
        if not vs:
            continue
        values = [float(v["view"]) for v in vs]
        if not values:
            continue
        mean_v = sum(values) / len(values)
        bull_v = max(values)
        bear_v = min(values)
        # variance: 単純標本分散 + small floor で BL が落ちないように
        if len(values) > 1:
            mean_sq = sum((x - mean_v) ** 2 for x in values) / len(values)
            variance = max(0.0005, mean_sq)
        else:
            # 1 source のみの場合は信頼度を下げる (大きめ variance)
            variance = 0.02
        result[t] = {
            "bull_view":  round(bull_v, 4),
            "bear_view":  round(bear_v, 4),
            "macro_view": round(mean_v, 4),
            "mean_view":  round(mean_v, 4),
            "variance":   round(variance, 6),
            "n_signals":  len(values),
            "avg_confidence": 1.0,  # 独立 source なので deweight しない印
            "sources":    vs,
        }
    return result

"""
ALMANAC v4.0 - ポートフォリオ最適化エンジン
skfolio / Riskfolio-Lib / PyPortfolioOpt を使用した最適ウェイト計算
HMMレジーム調整・CVaR最小化・最大Sharpe比・等リスク配分
"""

import json
import os
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

warnings.filterwarnings('ignore')

BASE_DIR = Path(__file__).parent

try:
    from risk_engine import THEME_GROUPS, CONCENTRATION_LIMITS as _RISK_LIMITS
    _THEME_CAP = _RISK_LIMITS.get('single_theme', 0.25)
except ImportError:
    THEME_GROUPS = {}
    _THEME_CAP = 0.25

# ============================================================
# 設定
# ============================================================

# 最適化対象ティッカー（yfinanceで取得可能な銘柄）
SKIP_OPTIMIZE = {
    'SLIM_SP500', 'SLIM_ORCAN', 'MNXACT', 'IFREE_FANGPLUS', 'NOMURA_SEMI',
    'AVGO_特定', 'AVGO_一般',
}

# ウェイト制約
WEIGHT_CONSTRAINTS = {
    'min_weight':     0.02,    # 最小2%（ロングオンリー）
    'max_weight':     0.20,    # 最大20%（集中リスク防止）
    'espp_max':     0.10,    # 持株会（単一銘柄集中）最大10%
}

# レジーム別リスク許容度調整
REGIME_RISK_FACTOR = {
    'A_強気': 1.0,
    'B_中立': 0.9,   # 0.8→0.9: 中立局面でのポジション圧縮を緩和（機会損失抑制）
    'C_弱気': 0.7,   # 0.5→0.7: 弱気でも30%キャッシュ程度に抑制（過度な現金化防止）
}


# ============================================================
# データ取得
# ============================================================

def _load_regime() -> str:
    path = BASE_DIR / 'regime_state.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('regime', 'B_中立')
    return 'B_中立'


def _load_holdings_tickers() -> list[str]:
    """holdings.jsonから最適化対象ティッカーを抽出する。"""
    path = BASE_DIR / 'holdings.json'
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as f:
        holdings = json.load(f)

    tickers = []
    for key, info in holdings.items():
        if key in SKIP_OPTIMIZE:
            continue
        ticker = info.get('ticker', key)
        if ticker not in SKIP_OPTIMIZE and ticker not in tickers:
            tickers.append(ticker)
    # 持株会
    if '9999.T' not in tickers:
        tickers.append('9999.T')
    return tickers


def load_returns(
    tickers: list[str],
    lookback_days: int = 252,
) -> pd.DataFrame:
    """
    Parquetから日次リターン行列を返す。

    Returns:
        DataFrame: インデックス=日付、カラム=ティッカー、値=日次リターン
    """
    from data_fetcher import load_ohlcv

    frames = {}
    for ticker in tickers:
        df = load_ohlcv(ticker)
        if df is None or df.empty:
            continue
        # MultiIndex カラムを展開（yfinance v0.2+）
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        close = df['Close']
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) < 60:
            continue
        ret = close.pct_change().dropna()
        frames[ticker] = ret.tail(lookback_days)

    if not frames:
        return pd.DataFrame()

    result = pd.DataFrame(frames).dropna()
    # inf・極端値を除去（IPO初日の異常リターン等）
    result = result.replace([np.inf, -np.inf], np.nan).dropna()
    result = result.clip(lower=-0.30, upper=0.30)   # 日次±30%でクリップ
    return result


# ============================================================
# 最適化: PyPortfolioOpt（最大Sharpe・最小CVaR）
# ============================================================

def optimize_pypfopt(
    returns: pd.DataFrame,
    method:  str = 'max_sharpe',   # 'max_sharpe' / 'min_cvar' / 'equal_risk'
    risk_free_rate: float = 0.045,  # 無リスク金利（10年国債利回り概算）
) -> dict:
    """
    PyPortfolioOptで最適ウェイトを計算する。

    Returns:
        {
          'weights': {ticker: weight},
          'expected_return': 期待リターン（年率）,
          'volatility': ボラティリティ（年率）,
          'sharpe': シャープレシオ,
          'method': 使用手法,
        }
    """
    from pypfopt import EfficientFrontier, expected_returns, EfficientCVaR

    # mean_historical_return は価格系列を期待するためリターンから価格を復元
    prices = (1 + returns).cumprod()
    mu = expected_returns.mean_historical_return(prices)

    # 安定した共分散行列（pandas + 微小正則化でApple Silicon互換）
    cov_df = returns.cov() * 252
    cov_arr = cov_df.values
    cov_arr += np.eye(len(cov_arr)) * 1e-8   # 正定値保証
    cov = pd.DataFrame(cov_arr, index=cov_df.index, columns=cov_df.columns)

    wb = (WEIGHT_CONSTRAINTS['min_weight'], WEIGHT_CONSTRAINTS['max_weight'])

    try:
        if method == 'min_cvar':
            ef = EfficientCVaR(mu, returns, beta=0.95, weight_bounds=wb)
            ef.min_cvar()
        elif method == 'equal_risk':
            # 等リスク配分（リスクパリティ）
            vols = returns.std() * np.sqrt(252)
            inv_vol = 1.0 / vols
            raw_w = inv_vol / inv_vol.sum()
            raw = {t: round(float(raw_w[t]), 4) for t in returns.columns}
            return {
                'weights':         raw,
                'expected_return': round(float(mu.dot(pd.Series(raw))), 4),
                'volatility':      None,
                'sharpe':          None,
                'method':          'equal_risk',
            }
        else:
            ef = EfficientFrontier(mu, cov, weight_bounds=wb)
            ef.max_sharpe(risk_free_rate=risk_free_rate)

        weights = ef.clean_weights()
        perf    = ef.portfolio_performance(verbose=False, risk_free_rate=risk_free_rate)

        return {
            'weights':         {k: round(v, 4) for k, v in weights.items() if v > 0.001},
            'expected_return': round(float(perf[0]), 4),
            'volatility':      round(float(perf[1]), 4),
            'sharpe':          round(float(perf[2]), 4),
            'method':          method,
        }

    except Exception as e:
        # フォールバック: 等ウェイト
        n   = len(returns.columns)
        raw = {t: round(1/n, 4) for t in returns.columns}
        return {
            'weights':         raw,
            'expected_return': None,
            'volatility':      None,
            'sharpe':          None,
            'method':          'equal_weight_fallback',
            'error':           str(e),
        }


# ============================================================
# 最適化: skfolio（CVaR最小化・MRC最大分散化）
# ============================================================

def optimize_skfolio(
    returns: pd.DataFrame,
    method:  str = 'min_cvar',   # 'min_cvar' / 'max_diversification'
) -> dict:
    """
    skfolioで最適ウェイトを計算する（CVaR最小化）。

    Returns:
        {
          'weights': {ticker: weight},
          'cvar_95': CVaR（年率）,
          'method': 使用手法,
        }
    """
    try:
        from skfolio import Portfolio, RatioMeasure
        from skfolio.optimization import MeanCVaR, MaximumDiversification
        from skfolio.preprocessing import prices_to_returns

        prices = (1 + returns).cumprod()

        if method == 'max_diversification':
            model = MaximumDiversification(
                min_weights=WEIGHT_CONSTRAINTS['min_weight'],
                max_weights=WEIGHT_CONSTRAINTS['max_weight'],
            )
        else:
            model = MeanCVaR(
                min_weights=WEIGHT_CONSTRAINTS['min_weight'],
                max_weights=WEIGHT_CONSTRAINTS['max_weight'],
                cvar_beta=0.95,
            )

        model.fit(returns.values)
        w = model.weights_

        weights = {ticker: round(float(w[i]), 4)
                   for i, ticker in enumerate(returns.columns)
                   if w[i] > 0.001}

        return {
            'weights': weights,
            'method':  method,
        }

    except Exception as e:
        n   = len(returns.columns)
        raw = {t: round(1/n, 4) for t in returns.columns}
        return {
            'weights': raw,
            'method':  'equal_weight_fallback',
            'error':   str(e),
        }


# ============================================================
# 最適化: Black-Litterman + LLM Views (ICLR 2025)
# ============================================================

def bl_optimize(
    returns: pd.DataFrame,
    bl_views_path: Optional[Path] = None,
) -> dict:
    """
    Black-Litterman最適化: Sonnet Bull/Bear/Macroの3視点分散をΩ（信頼度行列）として使用。

    LLMビューの分散 = モデル不確実性 → Ω（view confidence matrix）
    μ_BL = [(τΣ)⁻¹ + PᵀΩ⁻¹P]⁻¹ [(τΣ)⁻¹Π + PᵀΩ⁻¹q]

    Returns:
        {
          'weights': {ticker: weight},
          'bl_mu': Black-Littermanリターン推定,
          'method': 'black_litterman',
          'views_used': 使用したビュー数,
        }
    """
    try:
        from pypfopt.black_litterman import BlackLittermanModel, market_implied_risk_aversion
        from pypfopt import expected_returns, EfficientFrontier
    except ImportError:
        return {'weights': {}, 'method': 'bl_fallback', 'error': 'pypfopt.black_litterman 未インストール'}

    if bl_views_path is None:
        bl_views_path = BASE_DIR / 'bl_views.json'

    # BLビューを読み込み
    views_dict: dict = {}
    if bl_views_path.exists():
        try:
            with open(bl_views_path, encoding='utf-8') as f:
                raw = json.load(f)
            views_dict = raw.get('views', raw) if isinstance(raw, dict) else {}
        except Exception:
            pass

    prices = (1 + returns).cumprod()
    cov_df = returns.cov() * 252
    cov_arr = cov_df.values
    cov_arr += np.eye(len(cov_arr)) * 1e-8
    cov = pd.DataFrame(cov_arr, index=cov_df.index, columns=cov_df.columns)

    # 市場均衡リターン（CAPM prior）
    mkt_weights = pd.Series({t: 1.0 / len(returns.columns) for t in returns.columns})
    delta = market_implied_risk_aversion(prices.iloc[-1])

    # BLビューとΩ構築
    # P2-11: investment_type 別のクランプ幅（long=0.20 / medium=0.15 / swing=0.25）
    # P1-16: LLM 由来 view を Ω scale で意図的に deweight する。
    #   BL では Ω が小さい = view の信頼度が高い = posterior 影響が強い。
    #   現状の bl_views.json は Sonnet の (action,urgency) を期待リターンに写像した
    #   "confidence laundering" であり、独立な alpha 源ではない。
    #   独立 alpha source (factor / analyst consensus) が入るまでは大きめの scale で posterior 影響を抑制する。
    BL_LLM_OMEGA_SCALE = float(os.environ.get("BL_LLM_OMEGA_SCALE", "25.0"))

    holdings = _load_holdings()
    absolute_views: dict = {}
    view_variances: dict = {}
    for ticker in returns.columns:
        v = views_dict.get(ticker, {})
        if not v:
            continue
        bull = v.get('bull_view', 0.0)
        bear = v.get('bear_view', 0.0)
        macro = v.get('macro_view', 0.0)
        valid = [x for x in [bull, bear, macro] if x != 0.0]
        if not valid:
            continue
        mean_view = float(np.mean(valid))
        # P2-11: holdings から investment_type を検索して clamp 幅を決定
        itype = _lookup_investment_type(ticker, holdings)
        max_abs = get_max_abs_view(itype)
        absolute_views[ticker] = max(-max_abs, min(max_abs, mean_view))
        # _extract_bl_views が計算済みの variance を優先使用 + LLM deweight scale
        raw_variance = max(0.001, v.get("variance", 0.01))
        view_variances[ticker] = raw_variance * BL_LLM_OMEGA_SCALE

    views_used = len(absolute_views)

    try:
        if views_used == 0:
            # ビューなし: 市場均衡のみ（CAPM prior weights）
            bl = BlackLittermanModel(cov, pi='market', market_caps=mkt_weights, risk_aversion=delta)
        else:
            omega = np.diag([view_variances[t] for t in absolute_views])
            bl = BlackLittermanModel(
                cov,
                pi='market',
                market_caps=mkt_weights,
                risk_aversion=delta,
                absolute_views=absolute_views,
                omega=omega,
            )

        bl_returns = bl.bl_returns()
        bl_cov = bl.bl_cov()

        wb = (WEIGHT_CONSTRAINTS['min_weight'], WEIGHT_CONSTRAINTS['max_weight'])
        ef = EfficientFrontier(bl_returns, bl_cov, weight_bounds=wb)
        ef.max_sharpe(risk_free_rate=0.045)
        weights = ef.clean_weights()
        # P2-12: 終端で 1 度だけ theme cap を適用（重複呼出は _theme_capped=1 で skip）
        weights = _apply_theme_cap(dict(weights))
        perf = ef.portfolio_performance(verbose=False, risk_free_rate=0.045)

        return {
            'weights':         {k: round(v, 4) for k, v in weights.items()
                                if v > 0.001 and k != '_theme_capped'},
            'expected_return': round(float(perf[0]), 4),
            'volatility':      round(float(perf[1]), 4),
            'sharpe':          round(float(perf[2]), 4),
            'bl_mu':           {k: round(float(v), 4) for k, v in bl_returns.items()},
            'method':          'black_litterman',
            'views_used':      views_used,
        }

    except Exception as e:
        # フォールバック: Max Sharpe
        n = len(returns.columns)
        raw = {t: round(1/n, 4) for t in returns.columns}
        return {
            'weights': raw,
            'method':  'bl_fallback',
            'views_used': views_used,
            'error':   str(e),
        }


# ============================================================
# BL clamp 幅 & holdings lookup（P2-11）
# ============================================================

# investment_type 別の BL view clamp 幅
# long=10 年保有 → 長期的に大きな view を許容 / swing=短期裁量 → より広い
# medium=中期 → 従来の ±15% を維持
_MAX_ABS_VIEW_BY_TYPE = {
    'long':   0.20,
    'medium': 0.15,
    'swing':  0.25,
}


def get_max_abs_view(investment_type: Optional[str]) -> float:
    """investment_type 別の BL view clamp 幅を返す。未指定は medium 扱い。"""
    if not investment_type:
        return _MAX_ABS_VIEW_BY_TYPE['medium']
    return _MAX_ABS_VIEW_BY_TYPE.get(investment_type.lower(), _MAX_ABS_VIEW_BY_TYPE['medium'])


def _load_holdings() -> dict:
    """holdings.json を読み込む（errorless）"""
    try:
        with open(BASE_DIR / 'holdings.json', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _lookup_investment_type(ticker: str, holdings: dict) -> Optional[str]:
    """ticker → investment_type を解決。holdings はキーと ticker フィールド両方で検索。"""
    if ticker in holdings:
        return holdings[ticker].get('investment_type')
    for key, info in holdings.items():
        if info.get('ticker') == ticker:
            return info.get('investment_type')
    return None


# ============================================================
# テーマ集中キャップ（25%上限・P2-12 で単一適用化）
# ============================================================

def _apply_theme_cap(weights: dict, cap: float = None) -> dict:
    """
    テーマグループの合計ウェイトを上限（デフォルト25%）以下に抑制。
    超過分は比例縮小し、_cashへ移す（もしくは均等再配分）。

    P2-12: 二重適用を防止するため '_theme_capped' マーカーを付ける。
    再呼出時はノーオペで返す（中間パイプラインで繰り返し呼ばれてもウェイトが
    縮小し続けることがない）。
    """
    result = dict(weights)

    # P2-12: 二重適用防止
    if result.get('_theme_capped') == 1:
        import logging
        logging.getLogger(__name__).debug('[theme_cap] 既に適用済 → skip')
        return result

    if cap is None:
        cap = _THEME_CAP

    for theme_key, theme_info in THEME_GROUPS.items():
        theme_tickers = theme_info['tickers']
        in_theme = {t: result[t] for t in result if t in theme_tickers and result[t] > 0}
        theme_total = sum(in_theme.values())

        if theme_total > cap and theme_total > 0:
            scale = cap / theme_total
            excess = theme_total - cap
            for t in in_theme:
                result[t] = round(result[t] * scale, 4)
            # 超過分を現金へ積み増し
            result['_cash'] = round(result.get('_cash', 0) + excess, 4)

    result['_theme_capped'] = 1
    return result


# ============================================================
# HMMレジーム調整ウェイト
# ============================================================

def regime_adjusted_weights(
    base_weights:    dict,
    regime:          str,
    holdings_info:   Optional[dict] = None,
) -> dict:
    """
    HMMレジームに基づきウェイトを調整する。

    C_弱気:  全銘柄ウェイトを70%に圧縮（残り30%を現金）
    B_中立:  全銘柄ウェイトを90%に圧縮（残り10%を現金）
    A_強気:  変更なし
    """
    factor = REGIME_RISK_FACTOR.get(regime, 1.0)

    if factor >= 1.0:
        return base_weights

    # base_weights から内部マーカー（_theme_capped 等）を除外
    adjusted = {t: round(w * factor, 4) for t, w in base_weights.items() if t != '_theme_capped'}
    cash_pct  = round(1 - sum(adjusted.values()), 4)

    result = {**adjusted, '_cash': cash_pct}
    # P2-12: base_weights がすでに theme cap 適用済みなら skip、未適用なら適用
    capped = _apply_theme_cap(result)
    # マーカーは外部には出さない
    return {k: v for k, v in capped.items() if k != '_theme_capped'}


# ============================================================
# 最適化: Sparse Mean-CVaR（L1正則化・研究レポート item⑦）
# ============================================================

def sparse_mean_cvar_optimize(returns: pd.DataFrame, cvar_beta: float = 0.95, l1_coef: float = 0.01) -> dict:
    """
    Autonomous Sparse Mean-CVaR最適化（研究レポート item⑦）
    L1正則化による自動スパース化（ゼロウェイト銘柄の自動除去）
    """
    try:
        from skfolio import RatioMeasure
        from skfolio.optimization import MeanRisk

        prices = (1 + returns).cumprod()
        model = MeanRisk(
            risk_measure=RatioMeasure.CVAR,
            l1_coef=l1_coef,
            min_weights=0.0,
            max_weights=0.3,
        )
        model.fit(prices)
        w = model.weights_
        tickers = returns.columns.tolist()
        weights = {t: float(w[i]) for i, t in enumerate(tickers) if w[i] > 0.005}
        total = sum(weights.values()) or 1.0
        weights = {k: v / total for k, v in weights.items()}
        n_zero = sum(1 for v in w if v < 0.005)
        return {
            "weights": weights,
            "method": "sparse_mean_cvar",
            "n_assets_zeroed": n_zero,
            "l1_coef": l1_coef,
        }
    except Exception as e:
        tickers = returns.columns.tolist()
        n = len(tickers)
        return {"weights": {t: 1/n for t in tickers}, "method": "sparse_mean_cvar_fallback", "error": str(e)}


# ============================================================
# 総合最適化
# ============================================================

# ============================================================
# Part E-8: Risk Parity Tier Allocation
# ============================================================

_TIER_CLAMPS = {
    'long':   (0.50, 0.70),
    'medium': (0.20, 0.35),
    'swing':  (0.05, 0.15),
}
_TIER_FALLBACK_VOL = {'long': 0.12, 'medium': 0.20, 'swing': 0.35}


def compute_risk_parity_weights(
    returns: pd.DataFrame | None = None,
    tier_vols: dict | None = None,
) -> dict:
    """
    投資ティアごとに 1/vol で重み付けし、tier 合計がクランプ範囲内になるよう
    正規化。

    Returns:
        {
          'tier_vols':     {'long': 0.12, 'medium': 0.20, 'swing': 0.35},
          'raw_weights':   {'long': 0.68, 'medium': 0.24, 'swing': 0.08},
          'clamped_weights': {...},  # 上下限でクランプしたあとの正規化済み
          'method': 'risk_parity_inv_vol',
        }
    """
    holdings = _load_holdings()
    if tier_vols is None:
        # returns が来ているなら tier 平均 vol を計測
        tier_vols = dict(_TIER_FALLBACK_VOL)
        if returns is not None and not returns.empty:
            try:
                import numpy as np
                tier_cols: dict[str, list[str]] = {'long': [], 'medium': [], 'swing': []}
                for tk in returns.columns:
                    it = _lookup_investment_type(tk, holdings)
                    if it in tier_cols:
                        tier_cols[it].append(tk)
                annualize = np.sqrt(252)
                for tier, cols in tier_cols.items():
                    if cols:
                        # 等加重ポート returns を日次で算出 → 年率 vol
                        tier_ret = returns[cols].mean(axis=1)
                        tv = float(tier_ret.std() * annualize)
                        if 0.001 < tv < 2.0:
                            tier_vols[tier] = tv
            except Exception:
                pass

    # 1/vol weights
    inv = {t: 1.0 / max(v, 1e-4) for t, v in tier_vols.items()}
    total = sum(inv.values()) or 1.0
    raw = {t: inv[t] / total for t in tier_vols}

    # Iterative clamp + redistribute: サチった枠は固定し、残りを正規化してから再クランプ
    clamped = dict(raw)
    fixed: dict[str, float] = {}
    for _ in range(10):
        free = {t: w for t, w in clamped.items() if t not in fixed}
        if not free:
            break
        remaining = 1.0 - sum(fixed.values())
        # free tiers を正規化
        s = sum(free.values()) or 1.0
        for t in free:
            clamped[t] = free[t] / s * remaining
        # 上下限を超える tier を fix
        newly_fixed = False
        for t, w in list(clamped.items()):
            if t in fixed:
                continue
            lo, hi = _TIER_CLAMPS.get(t, (0.0, 1.0))
            if w > hi + 1e-9:
                fixed[t] = hi
                clamped[t] = hi
                newly_fixed = True
            elif w < lo - 1e-9:
                fixed[t] = lo
                clamped[t] = lo
                newly_fixed = True
        if not newly_fixed:
            break

    return {
        'tier_vols':       {t: round(v, 4) for t, v in tier_vols.items()},
        'raw_weights':     {t: round(w, 4) for t, w in raw.items()},
        'clamped_weights': {t: round(w, 4) for t, w in clamped.items()},
        'method':          'risk_parity_inv_vol',
        'clamp_bounds':    _TIER_CLAMPS,
    }


def run_optimization(
    lookback_days: int = 252,
    methods: Optional[list] = None,
    persist: bool = False,
) -> dict:
    """
    複数手法で最適化を実行し、結果を比較する。

    Args:
        lookback_days: リターン計算の遡及日数
        methods:       最適化手法リスト (default: max_sharpe/min_cvar/equal_risk)
        persist:       True の場合 `portfolio_optimization.json` を追加で書き出す
                       (analyst/__init__.py alpha_context で参照される)

    Returns:
        {
          'tickers':   使用ティッカーリスト,
          'regime':    現在レジーム,
          'results':   {method: {weights, metrics, regime_weights}},
          'recommended': 推奨手法名,
          'vol_target':  Vol Targeting メタデータ,
          'risk_parity': tier 別 inv-vol 重み,
          'as_of':     計算日時,
        }
    """
    if methods is None:
        methods = ['max_sharpe', 'min_cvar', 'equal_risk']

    tickers = _load_holdings_tickers()
    regime  = _load_regime()
    returns = load_returns(tickers, lookback_days=lookback_days)

    if returns.empty or len(returns.columns) < 3:
        return {
            'error':   'リターンデータが不足しています（最低3銘柄・60日分必要）',
            'tickers': tickers,
            'regime':  regime,
            'as_of':   datetime.now().strftime('%Y-%m-%d %H:%M'),
        }

    used_tickers = list(returns.columns)
    results      = {}

    for method in methods:
        if method == 'sparse_mean_cvar':
            res = sparse_mean_cvar_optimize(returns)
        elif method in ('min_cvar', 'max_diversification'):
            res = optimize_skfolio(returns, method=method)
        elif method == 'black_litterman':
            res = bl_optimize(returns)
        else:
            res = optimize_pypfopt(returns, method=method)

        # レジーム調整
        res['regime_weights'] = regime_adjusted_weights(res['weights'], regime)
        results[method]       = res

    # 推奨: レジームに応じて手法を選択
    recommended = {
        'A_強気': 'max_sharpe',
        'B_中立': 'min_cvar',
        'C_弱気': 'equal_risk',
    }.get(regime, 'min_cvar')

    # Part E-2: Vol Targeting — 推奨手法の metrics.annualized_vol から
    # portfolio 全体の scale (0.7〜1.2) を算出して返り値に含める。
    vol_target: dict = {}
    try:
        from risk_engine import compute_vol_target_scale, TARGET_ANNUAL_VOL
        rec_metrics = results.get(recommended, {}).get('metrics') or {}
        predicted_vol = rec_metrics.get('annualized_vol') or rec_metrics.get('volatility')
        if predicted_vol:
            vt = compute_vol_target_scale(float(predicted_vol), TARGET_ANNUAL_VOL, persist=True)
            vol_target = vt
            # 各 method の regime_weights に vol scale を追加適用
            for m, r in results.items():
                rw = r.get('regime_weights') or {}
                scaled = {tk: w * vt['scale'] for tk, w in rw.items()}
                # 合計が 1 を超えないよう正規化（under-investment を許容）
                r['vol_scaled_weights'] = scaled
                r['vol_target_scale']   = vt['scale']
    except Exception as _e:
        vol_target = {'error': str(_e)[:160]}

    # Part E-8: tier 別 risk parity weights を同梱（Opus 参考値）
    try:
        risk_parity = compute_risk_parity_weights(returns=returns)
    except Exception as _e:
        risk_parity = {'error': str(_e)[:160]}

    payload = {
        'tickers':         used_tickers,
        'regime':          regime,
        'results':         results,
        'recommended':     recommended,
        'vol_target':      vol_target,
        'risk_parity':     risk_parity,
        'as_of':           datetime.now().strftime('%Y-%m-%d %H:%M'),
    }

    # Fix C (2026-04-20): alpha_context が参照する portfolio_optimization.json を
    # persist=True 時に書き出す。optimization_result.json は既存 CLI 経路維持。
    if persist:
        try:
            alpha_path = BASE_DIR / 'portfolio_optimization.json'
            with open(alpha_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            print(f'[optimizer] wrote {alpha_path.name} (risk_parity + vol_target for alpha_context)')
        except Exception as e:
            print(f'[optimizer] persist skip: {e}')

    return payload


# ============================================================
# 現在ポートフォリオとの比較
# ============================================================

def compare_with_current(
    opt_result: dict,
    method: Optional[str] = None,
    fx_rate: Optional[float] = None,
) -> list:
    if fx_rate is None:
        from utils import get_fx_rate_cached
        fx_rate, _ = get_fx_rate_cached()
    """
    最適ウェイトと現在保有比率を比較してアクションリストを返す。

    Returns:
        [{ticker, current_pct, optimal_pct, action, diff_pct, priority}]
    """
    if 'error' in opt_result:
        return []

    method   = method or opt_result.get('recommended', 'min_cvar')
    result   = opt_result['results'].get(method, {})
    weights  = result.get('regime_weights', result.get('weights', {}))

    # 現在の保有比率を portfolio_manager から取得
    try:
        import portfolio_manager as pm
        snapshot     = pm.build_portfolio_snapshot()
        total_jpy    = snapshot.get('total_jpy', 1)
        positions    = {p['ticker']: p['value_jpy'] / total_jpy
                        for p in snapshot['positions']}
    except Exception:
        positions = {}

    actions = []
    all_tickers = set(list(weights.keys()) + list(positions.keys())) - {'_cash'}

    for ticker in all_tickers:
        current = positions.get(ticker, 0.0)
        optimal = weights.get(ticker, 0.0)
        diff    = optimal - current

        # Tolerance band: ±2.5% 以内は hold（細切れリバランス抑制）。
        # ±5% 超で実トリガー、±2.5〜5% は minor 扱い。
        TOLERANCE_BAND = 0.025
        TRIGGER_BAND   = 0.05
        if abs(diff) < TOLERANCE_BAND:
            action = 'hold'
            priority = 3
        elif diff > TRIGGER_BAND:
            action   = 'increase'
            priority = 1
        elif diff < -TRIGGER_BAND:
            action   = 'decrease'
            priority = 2
        else:
            action   = 'minor'
            priority = 3

        actions.append({
            'ticker':      ticker,
            'current_pct': round(current * 100, 1),
            'optimal_pct': round(optimal * 100, 1),
            'diff_pct':    round(diff * 100, 1),
            'action':      action,
            'priority':    priority,
        })

    actions.sort(key=lambda x: (x['priority'], -abs(x['diff_pct'])))
    return actions


# ============================================================
# 保存・ロード
# ============================================================

def save_optimization(result: dict):
    path = BASE_DIR / 'optimization_result.json'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def load_optimization() -> Optional[dict]:
    path = BASE_DIR / 'optimization_result.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return None


# ============================================================
# CLI
# ============================================================

def _print_result(result: dict, method: Optional[str] = None):
    if 'error' in result:
        print(f'エラー: {result["error"]}')
        return

    method = method or result.get('recommended', 'min_cvar')
    res    = result['results'].get(method, {})
    regime = result['regime']

    print(f'\n=== ポートフォリオ最適化 {result["as_of"]} ===')
    print(f'レジーム: {regime} / 推奨手法: {result["recommended"]}')
    print(f'使用銘柄: {len(result["tickers"])}本')
    print(f'\n【{method}】')

    if res.get('expected_return'):
        print(f'  期待リターン: {res["expected_return"]*100:.2f}%')
    if res.get('volatility'):
        print(f'  ボラティリティ: {res["volatility"]*100:.2f}%')
    if res.get('sharpe'):
        print(f'  シャープレシオ: {res["sharpe"]:.3f}')

    print('\n  最適ウェイト:')
    weights = res.get('regime_weights', res.get('weights', {}))
    for ticker, w in sorted(weights.items(), key=lambda x: -x[1]):
        if ticker == '_cash':
            print(f'    {"現金":12s}: {w*100:.1f}%')
        else:
            print(f'    {ticker:12s}: {w*100:.1f}%')

    # 現在比較
    actions = compare_with_current(result, method)
    increase = [a for a in actions if a['action'] == 'increase']
    decrease = [a for a in actions if a['action'] == 'decrease']

    if increase:
        print('\n  増加推奨:')
        for a in increase:
            print(f'    ↑ {a["ticker"]}: {a["current_pct"]}% → {a["optimal_pct"]}% ({a["diff_pct"]:+.1f}%)')
    if decrease:
        print('\n  削減推奨:')
        for a in decrease:
            print(f'    ↓ {a["ticker"]}: {a["current_pct"]}% → {a["optimal_pct"]}% ({a["diff_pct"]:+.1f}%)')


if __name__ == '__main__':
    import sys
    method = sys.argv[1] if len(sys.argv) > 1 else None

    print('最適化を実行中...')
    result = run_optimization()
    _print_result(result, method)
    save_optimization(result)
    print(f'\n結果保存: optimization_result.json')

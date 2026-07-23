"""
A-7: Factor Attribution
------------------------
ポートフォリオ月次リターンを ETF プロキシの Fama-French 類似ファクターに
OLS 回帰し、α / β / R² を算出する。

ファクター定義（ETF プロキシ、日本から取得可能）:
  MKT  = SPY (US 市場)
  SMB  = IWM - SPY              （小型 − 大型）
  HML  = IVE - IVW              （バリュー − グロース）
  MOM  = MTUM - SPY             （モメンタム − 市場）
  QMJ  = QUAL - SPY             （クオリティ − 市場）
  LVOL = SPLV - SPY             （低ボラ − 市場）
  FX   = USDJPY=X 月次リターン  （円建てポートフォリオ向け）

日本株が多い場合 EWJ や 1321.T でカスタマイズ可能。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
ATTR_PATH = BASE_DIR / 'factor_attribution.json'

# 既定ファクター構成
# economic_rationale: 経済的因果ストーリー（学術文献での裏付け）
#   True  = リスクプレミアム or 行動バイアスとして十分に支持
#   False = データマイニング懸念ありの因子（参考表示のみ）
FACTOR_TICKERS = {
    'MKT':  {'long': 'SPY',  'short': None,  'economic_rationale': True,
             'description': 'CAPM 市場ファクター'},
    'SMB':  {'long': 'IWM',  'short': 'SPY', 'economic_rationale': True,
             'description': 'Fama-French Small-Big サイズプレミアム'},
    'HML':  {'long': 'IVE',  'short': 'IVW', 'economic_rationale': True,
             'description': 'Fama-French Value-Growth バリュープレミアム'},
    'MOM':  {'long': 'MTUM', 'short': 'SPY', 'economic_rationale': True,
             'description': 'Carhart モメンタム（Jegadeesh-Titman）'},
    'QMJ':  {'long': 'QUAL', 'short': 'SPY', 'economic_rationale': True,
             'description': 'AQR Quality-Minus-Junk クオリティプレミアム'},
    'LVOL': {'long': 'SPLV', 'short': 'SPY', 'economic_rationale': True,
             'description': 'Low-Volatility アノマリー'},
    # v5.1: BAB（Betting Against Beta, Frazzini-Pedersen 2014）
    # 真の BAB は低β銘柄ロング × 高β銘柄ショートをβ中立化したものだが、
    # 個人スケールでは SPLV (低β代理) - SPHB (高β代理) で近似
    'BAB':  {'long': 'SPLV', 'short': 'SPHB', 'economic_rationale': True,
             'description': 'Betting Against Beta (Frazzini-Pedersen 2014)'},
    'FX':   {'long': 'USDJPY=X', 'short': None, 'economic_rationale': True,
             'description': '円建てポートフォリオ向け為替ファクター'},
}


def _fetch_monthly_returns(ticker: str, months: int = 36):
    """月次リターン Series を返す（failable; None on error）

    v5.1 修正: 異なる ticker でタイムゾーンが混在すると pd.concat 時に
    インデックスがマッチせず dropna で全行消失するため、tz を strip して
    日付のみに正規化する（ティッカー間の月次集計に tz は不要）。
    """
    try:
        import yfinance as yf
        import pandas as pd
        end = datetime.now()
        start = end - timedelta(days=months * 31 + 30)
        hist = yf.Ticker(ticker).history(
            start=start.strftime('%Y-%m-%d'),
            end=end.strftime('%Y-%m-%d'),
            auto_adjust=True,
        )
        if hist is None or hist.empty:
            return None
        # 月末 close で月次リターン
        monthly = hist['Close'].resample('ME').last()
        # タイムゾーンを除去し、月初日に正規化（これで全ティッカーが揃う）
        if monthly.index.tz is not None:
            monthly.index = monthly.index.tz_localize(None)
        monthly.index = monthly.index.to_period('M').to_timestamp()
        return monthly.pct_change().dropna()
    except Exception:
        return None


def build_factor_panel(months: int = 36) -> dict:
    """ファクター月次リターンの DataFrame 相当を dict で返す"""
    import pandas as pd
    panel = {}
    for name, spec in FACTOR_TICKERS.items():
        long_ret = _fetch_monthly_returns(spec['long'], months)
        if long_ret is None:
            continue
        if spec['short']:
            short_ret = _fetch_monthly_returns(spec['short'], months)
            if short_ret is None:
                continue
            idx = long_ret.index.intersection(short_ret.index)
            panel[name] = (long_ret.loc[idx] - short_ret.loc[idx])
        else:
            panel[name] = long_ret
    if not panel:
        return {}
    df = pd.DataFrame(panel).dropna()
    return {'df': df, 'months': len(df)}


# 月次リターンが yfinance から取得不可な ticker（投信・キャッシュ等）。
# factor 回帰のウェイト集計から除外する（含めると 口数×単価 のスケール違いで
# ウェイトが歪み、betaが極端に小さくなる）
_FACTOR_SKIP_TICKERS = {
    "SLIM_SP500", "SLIM_ORCAN", "MNXACT", "IFREE_FANGPLUS", "NOMURA_SEMI",
    "CASH_JPY", "CASH_USD", "CASH_JPY_SBI", "GS_MMF_USD",
    "AVGO_特定", "AVGO_一般", "AVGO_toku",
}


def _estimate_portfolio_monthly_returns(
    holdings: dict,
    months: int = 36,
) -> Optional['pd.Series']:
    """
    保有銘柄の現在ウェイトを固定とみなした合成月次リターン。
    厳密には時系列でウェイトが変動するが、factor β 推定目的には十分な近似。

    v5.1 修正: 投信（SLIM_*, IFREE_*, MNXACT 等）は yfinance で取得不可な上、
    holdings.json の shares × entry_price が口数スケールで JPY 換算できておらず、
    ウェイト集計に含めると残りの個別株のウェイトが極端に小さくなる（10⁻⁵ オーダー）。
    そのため _FACTOR_SKIP_TICKERS でスキップし、回帰可能な ticker だけで再正規化する。
    """
    import pandas as pd

    weights = {}
    total = 0.0
    for key, info in holdings.items():
        ticker = info.get('ticker') or key
        if ticker in _FACTOR_SKIP_TICKERS or key in _FACTOR_SKIP_TICKERS:
            continue
        shares = float(info.get('shares', 0))
        price  = float(info.get('current_price', info.get('entry_price', 0)) or 0)
        val    = shares * price
        if info.get('currency') == 'USD':
            val *= 150  # 近似、相対ウェイト計算には影響小
        if val <= 0:
            continue
        weights[ticker] = weights.get(ticker, 0) + val
        total += val

    if total <= 0:
        return None
    weights = {k: v/total for k, v in weights.items()}

    # 月次リターンを取得
    series_list = []
    for ticker, w in weights.items():
        r = _fetch_monthly_returns(ticker, months)
        if r is None or r.empty:
            continue
        r.name = ticker
        series_list.append(r * w)
    if not series_list:
        return None
    df = pd.concat(series_list, axis=1).dropna()
    return df.sum(axis=1)


def run_ols(y, X):
    """
    Numpy-based OLS (no statsmodels dependency).
    Returns betas, alpha, R², t_stats (approx).
    """
    import numpy as np
    n = len(y)
    k = X.shape[1]
    X1 = np.column_stack([np.ones(n), X])  # intercept
    coef, *_ = np.linalg.lstsq(X1, y, rcond=None)
    alpha = coef[0]
    betas = coef[1:]
    y_hat = X1 @ coef
    residuals = y - y_hat
    ss_res = (residuals ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    # t-stats
    dof = n - k - 1
    if dof > 0:
        sigma2 = ss_res / dof
        try:
            cov = sigma2 * np.linalg.inv(X1.T @ X1)
            se = np.sqrt(np.diag(cov))
            t_stats = coef / se
        except np.linalg.LinAlgError:
            t_stats = np.zeros_like(coef)
    else:
        t_stats = np.zeros_like(coef)
    return {
        'alpha':       float(alpha),
        'alpha_tstat': float(t_stats[0]),
        'betas':       [float(b) for b in betas],
        'beta_tstats': [float(t) for t in t_stats[1:]],
        'r_squared':   float(r2),
        'dof':         int(dof),
        'n_obs':       int(n),
    }


def attribution_monthly(
    months: int = 36,
    holdings: Optional[dict] = None,
    persist: bool = True,
) -> dict:
    """
    ポートフォリオ月次リターンをファクターに回帰。

    Returns:
        {
          'alpha':       月次 α（小数）,
          'alpha_tstat': α の t 値,
          'betas':       {factor: beta, ...},
          'beta_tstats': {factor: t, ...},
          'r_squared':   R²,
          'n_months':    推定に使った月数,
          'verdict':     'positive_alpha' | 'neutral' | 'negative_alpha',
        }
    """
    import pandas as pd

    if holdings is None:
        try:
            hpath = BASE_DIR / 'holdings.json'
            holdings = json.loads(hpath.read_text(encoding='utf-8')) if hpath.exists() else {}
        except Exception:
            holdings = {}

    y = _estimate_portfolio_monthly_returns(holdings, months)
    if y is None or len(y) < 12:
        return {'error': 'Insufficient return history (<12 months)'}

    panel = build_factor_panel(months)
    if not panel:
        return {'error': 'Factor panel fetch failed'}
    factors = panel['df']

    # align
    idx = y.index.intersection(factors.index)
    y_a = y.loc[idx].values
    X_a = factors.loc[idx].values
    factor_names = list(factors.columns)

    if len(y_a) < 12:
        return {'error': f'Overlap < 12 months ({len(y_a)})'}

    res = run_ols(y_a, X_a)

    # verdict
    if res['alpha'] > 0 and res['alpha_tstat'] > 2.0:
        verdict = 'positive_alpha'
    elif abs(res['alpha_tstat']) < 1.0:
        verdict = 'neutral'
    elif res['alpha'] < 0 and res['alpha_tstat'] < -1.0:
        verdict = 'negative_alpha'
    else:
        verdict = 'uncertain'

    result = {
        'alpha':        round(res['alpha'], 6),
        'alpha_annual': round(res['alpha'] * 12, 4),
        'alpha_tstat':  round(res['alpha_tstat'], 3),
        'betas':        {n: round(b, 4) for n, b in zip(factor_names, res['betas'])},
        'beta_tstats':  {n: round(t, 3) for n, t in zip(factor_names, res['beta_tstats'])},
        'r_squared':    round(res['r_squared'], 4),
        'n_months':     res['n_obs'],
        'dof':          res['dof'],
        'verdict':      verdict,
        'as_of':        datetime.now().isoformat(),
        'factors_used': factor_names,
    }

    if persist:
        try:
            tmp = ATTR_PATH.with_suffix('.tmp')
            tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(ATTR_PATH)
        except Exception:
            pass

    return result


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'

    if cmd == 'run':
        months = int(sys.argv[2]) if len(sys.argv) > 2 else 36
        print(f'Running factor attribution with {months} months...')
        result = attribution_monthly(months=months)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == 'status':
        if ATTR_PATH.exists():
            print(ATTR_PATH.read_text(encoding='utf-8'))
        else:
            print('No attribution data — run `python factor_attribution.py run`')

    elif cmd == 'selftest':
        # 合成テスト: y = 0.002 + 0.8 * MKT + noise
        import numpy as np
        np.random.seed(42)
        n = 36
        mkt = np.random.randn(n) * 0.04
        smb = np.random.randn(n) * 0.02
        y = 0.002 + 0.8 * mkt + 0.1 * smb + np.random.randn(n) * 0.01
        X = np.column_stack([mkt, smb])
        res = run_ols(y, X)
        print(f'selftest: α={res["alpha"]:.4f} (expected ~0.002, t={res["alpha_tstat"]:.2f})')
        print(f'          β_MKT={res["betas"][0]:.3f} (expected ~0.8, t={res["beta_tstats"][0]:.2f})')
        print(f'          β_SMB={res["betas"][1]:.3f} (expected ~0.1, t={res["beta_tstats"][1]:.2f})')
        print(f'          R²={res["r_squared"]:.3f}')
        assert 0.7 < res['betas'][0] < 0.9, 'MKT beta wrong'
        assert res['r_squared'] > 0.5, 'R² too low'
        print('✅ OLS self-test pass')

    else:
        print('Usage: factor_attribution.py [run [months] | status | selftest]')

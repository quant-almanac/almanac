"""
ALMANAC v4.0 - リスク管理エンジン
Cornish-Fisher VaR/CVaR、HMMレジーム検知、GJR-GARCH、Copula、集中リスク管理
"""

import numpy as np
import pandas as pd
from scipy import stats
from scipy.special import comb
from typing import Optional
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 定数・設定
# ============================================================

CONCENTRATION_LIMITS = {
    'single_stock_long_term':  0.20,   # 15%→20%: 高確信銘柄の集中投資余地を拡大
    'single_stock_short_term': 0.08,   # 5%→8%: 戦術枠の柔軟性向上
    'espp_plan_max':         0.10,   # 人的資本リスク考慮（変更なし）
    'correlated_group':        0.30,
    'single_sector':           0.30,
    'single_theme':            0.25,   # テーマ集中上限（半導体/欧州など）
}

# テーマグループ定義（ファクター集中リスク管理用）
THEME_GROUPS = {
    'semiconductor_ai': {
        'tickers': {'NVDA', 'AVGO', 'AVGO_特定', 'AVGO_一般', 'NOMURA_SEMI', 'IFREE_FANGPLUS'},
        'label': '半導体・AI',
    },
    'europe': {
        'tickers': {'IEV', 'EWG', 'EPOL'},
        'label': '欧州',
    },
    'japan_domestic': {
        'tickers': {'1489.T', '9999.T'},
        'label': '日本国内',
    },
    'us_broad_market': {
        'tickers': {'SLIM_SP500', 'SLIM_ORCAN', 'MNXACT', 'GLD'},
        'label': '米国広域・コア',
    },
}

BEHAVIORAL_GUARDRAILS = {
    'daily_loss_limit':    -0.04,   # 1日-4%: 新規エントリー禁止（2026-04改訂）
    'monthly_loss_limit':  -0.08,   # 月間-8%: トレード停止（2026-04改訂）
    'max_short_positions':  3,
    # max_active_trades は廃止 — ポジション数制限なし（2026-04改訂）
    'override_logging':    True,
}

DRAWDOWN_ALERTS = {
    'warning':  -0.25,   # -25%: 全ポジション50%縮小アラート
    'critical': -0.35,   # -35%: 全現金化推奨
}

STRESS_SCENARIOS = {
    'リーマン再来':      {'SP500': -0.50, 'USDJPY': -0.27},   # 80円換算
    '半導体バブル崩壊':  {'NVDA': -0.60, 'AVGO': -0.50},
    '急激な円高':        {'USDJPY': -0.11},                    # 120円換算（想定140円→120円）
    '対象銘柄急落':        {'9999.T': -0.40},
}

# ============================================================
# 1. Cornish-Fisher VaR / CVaR
# ============================================================

def calculate_var_cornish_fisher(
    returns: pd.Series,
    confidence: float = 0.95,
    portfolio_value: float = 35_000_000
) -> dict:
    """
    Cornish-Fisher補正付きVaR計算
    正規分布の仮定を外しテールリスクを正確に捕捉

    Args:
        returns: 日次リターン系列
        confidence: 信頼水準（デフォルト95%）
        portfolio_value: ポートフォリオ総額（円）

    Returns:
        {
          'var_pct': VaR（%）,
          'var_jpy': VaR（円）,
          'z_cf': Cornish-Fisher調整後z値,
          'skewness': 歪度,
          'kurtosis': 尖度（超過）,
          'normal_var_pct': 正規分布VaR（比較用）,
        }
    """
    returns = returns.dropna()
    if len(returns) < 20:
        return {'error': 'データ不足（20件以上必要）'}

    mu    = returns.mean()
    sigma = returns.std()
    if sigma <= 0:
        return {'error': 'ボラティリティがゼロ（全リターンが同値）'}
    skew  = stats.skew(returns)
    kurt  = stats.kurtosis(returns)   # excess kurtosis

    # Cornish-Fisher展開
    z_alpha = stats.norm.ppf(1 - confidence)
    z_cf = (
        z_alpha
        + (z_alpha**2 - 1) * skew / 6
        + (z_alpha**3 - 3 * z_alpha) * kurt / 24
        - (2 * z_alpha**3 - 5 * z_alpha) * skew**2 / 36
    )

    raw_var_pct    = -(mu + sigma * z_cf)
    raw_normal_var = -(mu + sigma * z_alpha)
    # Extremely skewed realized-trade samples can make Cornish-Fisher produce
    # a negative VaR. VaR is a loss magnitude, so expose it as non-negative.
    var_pct        = max(0.0, raw_var_pct)
    normal_var_pct = max(0.0, raw_normal_var)
    var_jpy        = portfolio_value * var_pct

    return {
        'var_pct':        round(var_pct, 6),
        'var_jpy':        round(var_jpy, 0),
        'z_cf':           round(z_cf, 4),
        'skewness':       round(skew, 4),
        'kurtosis':       round(kurt, 4),
        'normal_var_pct': round(normal_var_pct, 6),
        'raw_var_pct':    round(raw_var_pct, 6),
        'cf_adjustment':  round(var_pct - normal_var_pct, 6),   # CF補正量
        'confidence':     confidence,
    }


def calculate_cvar(
    returns: pd.Series,
    confidence: float = 0.95,
    portfolio_value: float = 35_000_000
) -> dict:
    """
    CVaR (Expected Shortfall) = VaRを超えた損失の平均

    P1-7: ヒストリカル percentile を主の閾値として使用（CF は補助出力）。
    - cvar_pct:    主出力 = tail（percentile 以下）の平均損失
    - cvar_cf_pct: 補助出力 = CF ベースの閾値で算出した CVaR
    - cvar_unstable: tail_observations < 10 の場合 True（サンプル不足警告）

    Args:
        returns: 日次リターン系列
        confidence: 信頼水準
        portfolio_value: ポートフォリオ総額（円）

    Returns:
        {
          'cvar_pct':    CVaR（%）主、ヒストリカル,
          'cvar_cf_pct': CVaR（%）補助、Cornish-Fisher,
          'cvar_jpy':    CVaR（円）,
          'var_pct':     VaR（%）CF 参照用,
          'var_hist_pct': VaR（%）ヒストリカル,
          'tail_observations': テール観測件数,
          'cvar_unstable':     True = サンプル不足で不安定,
          'method':      'historical',
        }
    """
    returns = returns.dropna()
    if len(returns) < 20:
        return {'error': 'データ不足（20件以上必要）'}

    # ── 主: ヒストリカル閾値 ─────────────────────────────
    alpha = 1 - confidence
    hist_threshold = np.percentile(returns, alpha * 100)   # 例: 5パーセンタイル値（負値が期待される）
    var_hist_pct   = max(0.0, -hist_threshold)
    tail_hist      = returns[returns <= hist_threshold]
    n_tail         = len(tail_hist)

    if n_tail == 0:
        cvar_pct = var_hist_pct
    else:
        cvar_pct = -tail_hist.mean()

    # ── 補助: Cornish-Fisher 閾値（従来互換）──────────────
    cf_result = calculate_var_cornish_fisher(returns, confidence, portfolio_value)
    var_cf_pct = cf_result['var_pct']
    tail_cf    = returns[returns < -var_cf_pct]
    if len(tail_cf) == 0:
        cvar_cf_pct = var_cf_pct
    else:
        cvar_cf_pct = -tail_cf.mean()

    return {
        'cvar_pct':          round(cvar_pct, 6),
        'cvar_cf_pct':       round(cvar_cf_pct, 6),
        'cvar_jpy':          round(portfolio_value * cvar_pct, 0),
        'var_pct':           var_cf_pct,
        'var_hist_pct':      round(var_hist_pct, 6),
        'var_jpy':           cf_result['var_jpy'],
        'tail_observations': n_tail,
        'cvar_unstable':     bool(n_tail < 10),
        'confidence':        confidence,
        'method':            'historical',
    }


def calculate_var_historical(
    returns: pd.Series,
    confidence: float = 0.95,
    portfolio_value: float = 35_000_000
) -> dict:
    """ヒストリカルVaR（比較用）"""
    returns = returns.dropna()
    if len(returns) < 20:
        return {'error': 'データ不足'}

    var_pct = -np.percentile(returns, (1 - confidence) * 100)
    return {
        'var_pct': round(var_pct, 6),
        'var_jpy': round(portfolio_value * var_pct, 0),
        'method':  'historical',
    }


# ============================================================
# 2. ドローダウン管理
# ============================================================

def calculate_drawdown(returns: pd.Series) -> dict:
    """
    最大ドローダウン・現在ドローダウン・アラートレベルを計算

    Returns:
        {
          'current_dd': 現在のドローダウン（%）,
          'max_dd': 最大ドローダウン（%）,
          'alert_level': 'normal' / 'warning' / 'critical',
          'action': 推奨アクション,
          'drawdown_series': pd.Series,
        }
    """
    returns = returns.dropna()
    cum_returns = (1 + returns).cumprod()
    rolling_max = cum_returns.cummax()
    drawdown    = (cum_returns - rolling_max) / rolling_max

    current_dd = drawdown.iloc[-1] if len(drawdown) > 0 else 0.0
    max_dd     = drawdown.min()

    if current_dd <= DRAWDOWN_ALERTS['critical']:
        alert_level = 'critical'
        action      = '全現金化を推奨。損失拡大リスクが高い。'
    elif current_dd <= DRAWDOWN_ALERTS['warning']:
        alert_level = 'warning'
        action      = '全ポジション50%縮小を推奨。'
    else:
        alert_level = 'normal'
        action      = '通常運用継続。'

    return {
        'current_dd':      round(current_dd, 4),
        'max_dd':          round(max_dd, 4),
        'alert_level':     alert_level,
        'action':          action,
        'drawdown_series': drawdown,
    }


# ============================================================
# 3. HMM 3状態レジーム検知
# ============================================================

def detect_regime_hmm(returns: pd.Series, n_states: int = 3) -> dict:
    """
    HMM（Hidden Markov Model）による市場レジーム検知
    状態: 0=強気（Bull）/ 1=中立（Neutral）/ 2=弱気・クライシス（Bear/Crisis）

    Returns:
        {
          'current_state': 現在の状態番号,
          'current_label': 'Bull' / 'Neutral' / 'Bear',
          'state_probs': 現在の状態確率,
          'means': 各状態の平均リターン,
          'stds': 各状態のボラティリティ,
          'state_series': pd.Series（時系列状態）,
        }
    """
    try:
        from hmmlearn import hmm
    except ImportError:
        return {'error': 'hmmlearn未インストール'}

    returns = returns.dropna()
    if len(returns) < 60:
        return {'error': 'データ不足（60件以上必要）'}

    X = returns.values.reshape(-1, 1)

    model = hmm.GaussianHMM(
        n_components=n_states,
        covariance_type='full',
        n_iter=200,
        random_state=42,
    )
    model.fit(X)
    states = model.predict(X)

    # 各状態を平均リターンでソート（強気=最高リターン順）
    state_means = {s: X[states == s].mean() for s in range(n_states)}
    sorted_states = sorted(state_means.items(), key=lambda x: x[1], reverse=True)
    state_map = {orig: new for new, (orig, _) in enumerate(sorted_states)}
    mapped_states = np.array([state_map[s] for s in states])

    labels = {0: 'Bull', 1: 'Neutral', 2: 'Bear'}
    means  = [X[states == orig].mean() for orig, _ in sorted_states]
    stds   = [X[states == orig].std()  for orig, _ in sorted_states]

    current_state = int(mapped_states[-1])
    log_probs, _ = model.score_samples(X[-1:])
    state_probs  = np.exp(model.predict_proba(X[-1:]))[0]

    return {
        'current_state': current_state,
        'current_label': labels[current_state],
        'state_probs':   {labels[i]: round(float(p), 4) for i, p in enumerate(state_probs)},
        'means':         {labels[i]: round(float(m), 6) for i, m in enumerate(means)},
        'stds':          {labels[i]: round(float(s), 6) for i, s in enumerate(stds)},
        'state_series':  pd.Series(mapped_states, index=returns.index),
    }


# ============================================================
# 3b. BBAPT 行動バイアス補正（研究レポート item⑥）
# ============================================================

def behavioral_bias_adjustment(
    regime_probs: dict,
    base_position_scale: float = 1.0,
) -> dict:
    """
    BBAPT行動バイアス補正（研究レポート item⑥ 近似実装）
    HMMレジーム確率から行動バイアス（過信・損失回避）を補正する。

    Args:
        regime_probs: detect_regime_hmm()の出力 {"bull_prob": float, "bear_prob": float, "neutral_prob": float}
                      または state_probs の Bull/Bear/Neutral キーを受け付ける
        base_position_scale: ベースポジションスケール（デフォルト1.0）

    Returns:
        dict with:
          position_scale: 補正後ポジションスケール（0.5〜1.2）
          bias_type: "overconfidence" | "loss_aversion" | "neutral"
          confidence_damper: 過信ダンパー比率
          loss_aversion_buffer: 損失回避バッファ比率
          rationale: str（説明）
    """
    # detect_regime_hmm() の state_probs 形式にも対応
    bull_p = regime_probs.get("bull_prob",
             regime_probs.get("Bull", 0.33))
    bear_p = regime_probs.get("bear_prob",
             regime_probs.get("Bear", 0.33))

    # 過信（強気レジーム > 0.7 → 12%ポジション縮小）
    if bull_p > 0.70:
        damper = 0.12
        scale = base_position_scale * (1.0 - damper)
        return {
            "position_scale": round(max(0.5, scale), 3),
            "bias_type": "overconfidence",
            "confidence_damper": damper,
            "loss_aversion_buffer": 0.0,
            "rationale": f"強気レジーム確率{bull_p:.0%}→過信ダンパー{damper:.0%}適用",
        }

    # 損失回避（弱気レジーム > 0.65 → 確認バッファ追加、売り急ぎ防止）
    if bear_p > 0.65:
        buffer = 0.08
        scale = base_position_scale * (1.0 + buffer * 0.5)
        return {
            "position_scale": round(min(1.2, scale), 3),
            "bias_type": "loss_aversion",
            "confidence_damper": 0.0,
            "loss_aversion_buffer": buffer,
            "rationale": f"弱気レジーム確率{bear_p:.0%}→損失回避バッファ{buffer:.0%}（売り急ぎ防止）",
        }

    # 中立
    return {
        "position_scale": base_position_scale,
        "bias_type": "neutral",
        "confidence_damper": 0.0,
        "loss_aversion_buffer": 0.0,
        "rationale": "中立レジーム→バイアス補正なし",
    }


# ============================================================
# 4. GJR-GARCH ボラティリティ予測
# ============================================================

def estimate_gjr_garch(returns: pd.Series, use_ginn: bool = True) -> dict:
    """
    GJR-GARCH(1,1)モデル: 負ショックが正ショックより1.5倍ボラを出す非対称効果

    Returns:
        {
          'forecast_vol': 翌日の予測ボラティリティ（年率換算）,
          'current_vol': 直近30日の実現ボラ（年率）,
          'leverage_effect': レバレッジ効果（gamma係数）,
          'regime_multiplier': レジーム乗数（ポジションサイジング用）,
        }
    """
    try:
        from arch import arch_model
    except ImportError:
        return {'error': 'arch未インストール'}

    returns = returns.dropna()
    if len(returns) < 60:
        return {'error': 'データ不足（60件以上必要）'}

    # GJR-GARCH(1,1)
    model = arch_model(
        returns * 100,    # %換算でスケール
        vol='Garch',
        p=1, o=1, q=1,    # o=1がGJR項
        dist='skewt',
        mean='Zero',
    )
    try:
        res = model.fit(disp='off', show_warning=False)
        forecast = res.forecast(horizon=1)
        forecast_vol = np.sqrt(forecast.variance.values[-1, 0]) / 100 * np.sqrt(252)

        gamma = float(res.params.get('gamma[1]', 0))
        current_vol = returns.tail(30).std() * np.sqrt(252)

        # レジーム乗数: ボラが高いほどポジション縮小
        vol_ratio = forecast_vol / (current_vol + 1e-9)
        regime_multiplier = max(0.3, min(1.5, 1 / vol_ratio))

        # GINN強化: GJR-GARCHを物理制約としてLSTMで精度改善
        garch_forecast = round(float(forecast_vol), 4)
        model_name = 'GJR-GARCH(1,1)-skewt'

        if use_ginn:
            try:
                from ginn_model import forecast_ginn
                ginn_vol = forecast_ginn(returns, garch_sigma=garch_forecast)
                garch_forecast = ginn_vol
                model_name = 'GINN+GJR-GARCH'
            except Exception:
                pass  # GJR-GARCHフォールバック継続

        return {
            'forecast_vol':      garch_forecast,
            'current_vol':       round(float(current_vol), 4),
            'leverage_effect':   round(gamma, 4),
            'regime_multiplier': round(regime_multiplier, 4),
            'model':             model_name,
        }
    except Exception as e:
        return {'error': f'GARCH推定失敗: {str(e)}'}


# ============================================================
# 5. Copulaベース依存モデリング（テール依存性）
# ============================================================

def analyze_tail_dependence(returns_df: pd.DataFrame, n_simulations: int = 100_000) -> dict:
    """
    Student-t Copulaによるテール依存性分析とモンテカルロシミュレーション
    NVDA・AVGO・META等の「同時急落」リスクを定量化

    Args:
        returns_df: 各銘柄のリターン系列（columns=銘柄名）
        n_simulations: モンテカルロシミュレーション回数

    Returns:
        {
          'tail_dependence': テール依存係数（下方）,
          'correlation_matrix': 相関行列,
          'joint_crash_prob': 同時50%急落の確率（モンテカルロ）,
          'var_portfolio_mc': モンテカルロVaR（95%）,
        }
    """
    returns_df = returns_df.dropna()
    if len(returns_df) < 60 or returns_df.shape[1] < 2:
        return {'error': 'データ不足または銘柄数不足'}

    try:
        from copulae import StudentCopula
        use_copulae = True
    except ImportError:
        use_copulae = False

    corr_matrix = returns_df.corr()

    if use_copulae:
        try:
            cop = StudentCopula(dim=returns_df.shape[1])
            # 一様分布に変換（empirical CDF）
            u = returns_df.rank(pct=True).values
            cop.fit(u)
            df_param = float(cop.params.get('df', 5))

            # テール依存係数（t-copula の下方テール依存係数）
            from scipy.stats import t as t_dist
            rho = np.array(corr_matrix)
            tail_dep = {}
            cols = list(returns_df.columns)
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    rho_ij = rho[i, j]
                    # t-copula の下方テール依存係数公式
                    lambda_L = 2 * t_dist.cdf(
                        -np.sqrt((df_param + 1) * (1 - rho_ij) / (1 + rho_ij)),
                        df_param + 1
                    )
                    tail_dep[f'{cols[i]}-{cols[j]}'] = round(float(lambda_L), 4)

            # モンテカルロシミュレーション（t-copula サンプリング）
            simulated = cop.random(n_simulations)
        except Exception:
            use_copulae = False

    if not use_copulae:
        # フォールバック: 正規Copula近似
        cov = returns_df.cov().values
        cov += np.eye(cov.shape[0]) * 1e-8   # 正則化（特異行列対策）
        mean = returns_df.mean().values
        try:
            simulated_raw = np.random.multivariate_normal(mean, cov, size=n_simulations)
        except np.linalg.LinAlgError:
            return {'error': '共分散行列が特異行列（データ不足または完全相関）'}
        tail_dep = {}
        cols = list(returns_df.columns)
        corr = np.array(corr_matrix)
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                tail_dep[f'{cols[i]}-{cols[j]}'] = round(float(corr[i, j]) * 0.7, 4)
        simulated = simulated_raw

    # 同時急落確率（全銘柄が同時に日次-5%以下の確率）
    if use_copulae:
        # 一様分布を実際のリターン分布に変換
        quantiles_5pct = returns_df.quantile(0.05).values
        thresholds = stats.norm.ppf(0.05)
        joint_crash_count = np.sum(np.all(simulated < 0.05, axis=1))
    else:
        threshold_5pct = returns_df.quantile(0.05).values
        joint_crash_count = np.sum(
            np.all(simulated < threshold_5pct, axis=1)
        )
    joint_crash_prob = joint_crash_count / n_simulations

    # モンテカルロVaR（等ウェイトポートフォリオ）
    if not use_copulae:
        portfolio_returns_mc = simulated_raw.mean(axis=1)
        var_mc = -np.percentile(portfolio_returns_mc, 5)
    else:
        var_mc = None

    return {
        'tail_dependence':   tail_dep,
        'correlation_matrix': corr_matrix.round(4).to_dict(),
        'joint_crash_prob':   round(float(joint_crash_prob), 6),
        'var_portfolio_mc':   round(float(var_mc), 6) if var_mc is not None else None,
        'n_simulations':      n_simulations,
        'method':             'Student-t Copula' if use_copulae else 'Normal Copula (fallback)',
    }


# ============================================================
# 6. 複合ポジションサイジング
# ============================================================

def calculate_position_size(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    portfolio_value: float,
    current_vol: float,
    regime_multiplier: float = 1.0,
    current_dd: float = 0.0,
    max_position_pct: float = 0.07,
) -> dict:
    """
    ハーフケリー × ボラティリティ調整 × レジーム乗数 × ドローダウン乗数
    最大7%ハードキャップ（1銘柄）

    Args:
        win_rate: 勝率（0-1）
        avg_win: 平均利益率
        avg_loss: 平均損失率（正の値で渡す）
        portfolio_value: ポートフォリオ総額（円）
        current_vol: 現在のボラティリティ（年率）
        regime_multiplier: HMMレジーム乗数
        current_dd: 現在のドローダウン（負の値）
        max_position_pct: 最大ポジションサイズ上限

    Returns:
        {
          'kelly_full': フルケリー比率,
          'half_kelly': ハーフケリー比率,
          'vol_adjustment': ボラティリティ調整係数,
          'dd_multiplier': ドローダウン乗数,
          'final_pct': 最終ポジションサイズ（%）,
          'final_jpy': 最終ポジションサイズ（円）,
        }
    """
    # ケリー基準
    if avg_loss == 0:
        return {'error': '平均損失が0'}

    odds = avg_win / avg_loss
    kelly_full = win_rate - (1 - win_rate) / odds
    half_kelly = kelly_full / 2

    # ボラティリティ調整（年率20%を基準とした逆比例）
    baseline_vol = 0.20
    vol_adjustment = min(1.5, baseline_vol / (current_vol + 1e-9))

    # ドローダウン乗数（ドローダウンが深いほどポジション縮小）
    if current_dd <= -0.35:
        dd_multiplier = 0.0    # 全現金化水準
    elif current_dd <= -0.25:
        dd_multiplier = 0.25   # 50%縮小 → 0.25倍相当
    elif current_dd <= -0.15:
        dd_multiplier = 0.5
    elif current_dd <= -0.10:
        dd_multiplier = 0.75
    else:
        dd_multiplier = 1.0

    # 最終サイズ
    raw_size = half_kelly * vol_adjustment * regime_multiplier * dd_multiplier
    final_pct = min(max(raw_size, 0), max_position_pct)
    final_jpy = portfolio_value * final_pct

    return {
        'kelly_full':      round(kelly_full, 4),
        'half_kelly':      round(half_kelly, 4),
        'vol_adjustment':  round(vol_adjustment, 4),
        'dd_multiplier':   dd_multiplier,
        'regime_multiplier': regime_multiplier,
        'final_pct':       round(final_pct, 4),
        'final_jpy':       round(final_jpy, 0),
        'capped':          raw_size > max_position_pct,
    }


# ============================================================
# 7. 集中リスク管630理
# ============================================================

def check_concentration_risk(
    positions: dict,
    portfolio_total: float,
    espp_value: float = 0,
    include_human_capital: bool = True,
    annual_salary: float = 0,
    years_to_retirement: int = 30,
) -> dict:
    """
    全ポジションの集中リスクチェック

    Args:
        positions: {ticker: value_jpy} のdict
        portfolio_total: ポートフォリオ総額
        espp_value: 持株会の評価額
        include_human_capital: 人的資本をリスクに含めるか
        annual_salary: 年収（人的資本計算用）
        years_to_retirement: 退職までの年数

    Returns:
        {
          'violations': ルール違反リスト,
          'espp_ratio': 持株会比率,
          'espp_alert': 持株会アラート,
          'position_ratios': 各銘柄の比率,
          'human_capital_risk': 人的資本リスクスコア,
        }
    """
    violations = []
    position_ratios = {}

    for ticker, value in positions.items():
        ratio = value / portfolio_total if portfolio_total > 0 else 0
        position_ratios[ticker] = round(ratio, 4)

        if ticker == '9999.T':
            limit = CONCENTRATION_LIMITS['espp_plan_max']
            if ratio > limit:
                violations.append({
                    'ticker':   ticker,
                    'type':     'espp_concentration',
                    'ratio':    round(ratio, 4),
                    'limit':    limit,
                    'excess':   round(ratio - limit, 4),
                    'message':  f'持株会が総ポートフォリオの{ratio*100:.1f}%（上限{limit*100:.0f}%）',
                })
        else:
            limit = CONCENTRATION_LIMITS['single_stock_long_term']
            if ratio > limit:
                violations.append({
                    'ticker':   ticker,
                    'type':     'single_stock',
                    'ratio':    round(ratio, 4),
                    'limit':    limit,
                    'excess':   round(ratio - limit, 4),
                    'message':  f'{ticker}が{ratio*100:.1f}%（上限{limit*100:.0f}%）',
                })

    # テーマ集中リスクチェック
    theme_limit = CONCENTRATION_LIMITS['single_theme']
    for theme_key, theme_info in THEME_GROUPS.items():
        theme_tickers = theme_info['tickers']
        theme_value = sum(v for t, v in positions.items() if t in theme_tickers)
        theme_ratio = theme_value / portfolio_total if portfolio_total > 0 else 0
        if theme_ratio > theme_limit:
            violations.append({
                'ticker':   f'[{theme_info["label"]}]',
                'type':     'theme_concentration',
                'ratio':    round(theme_ratio, 4),
                'limit':    theme_limit,
                'excess':   round(theme_ratio - theme_limit, 4),
                'message':  f'{theme_info["label"]}テーマが{theme_ratio*100:.1f}%（上限{theme_limit*100:.0f}%）',
                'tickers_in_theme': [t for t in positions if t in theme_tickers],
            })

    # 持株会専用チェック
    espp_ratio = (espp_value / portfolio_total) if portfolio_total > 0 else 0
    espp_alert = espp_ratio > CONCENTRATION_LIMITS['espp_plan_max']

    # 人的資本リスクスコア（勤務先非公開の場合）
    human_capital_risk = None
    if include_human_capital and annual_salary > 0:
        # 人的資本の現在価値（単純化: 年収×残存年数×0.7）
        human_capital_pv = annual_salary * years_to_retirement * 0.7
        total_wealth = portfolio_total + human_capital_pv
        espp_total_exposure = (espp_value + human_capital_pv) / total_wealth
        human_capital_risk = {
            'human_capital_pv':      round(human_capital_pv, 0),
            'total_wealth':          round(total_wealth, 0),
            'espp_total_exposure': round(espp_total_exposure, 4),
            'risk_level': (
                'critical' if espp_total_exposure > 0.5 else
                'warning'  if espp_total_exposure > 0.3 else
                'normal'
            ),
        }

    return {
        'violations':         violations,
        'espp_ratio':       round(espp_ratio, 4),
        'espp_alert':       espp_alert,
        'position_ratios':    position_ratios,
        'human_capital_risk': human_capital_risk,
        'limits_reference':   CONCENTRATION_LIMITS,
    }


# ============================================================
# 7b. 雇用主集中リスク（P3-15）
# ============================================================
#
# 持株会 + 個別 9999.T + 人的資本 の総合エクスポージャーを段階評価。
# 15% warn / 25% critical の二段アラート、critical では BL 減量バイアスを返す。

EMPLOYER_TICKER = '9999.T'
EMPLOYER_WARN_RATIO     = 0.15
EMPLOYER_CRITICAL_RATIO = 0.25


def employer_concentration_check(
    positions: dict,
    portfolio_total: float,
    espp_value: float = 0.0,
    annual_salary: float = 0.0,
    years_to_retirement: int = 30,
    include_human_capital: bool = True,
) -> dict:
    """
    雇用主株式（9999.T）への総合集中度を段階評価する。

    金融資産のみ: (持株会 + 個別 9999.T) / portfolio_total
    総資産ベース: (持株会 + 個別 9999.T + 人的資本 PV) / (portfolio_total + 人的資本 PV)

    金融資産ベース:
      >= 25%: critical (売却/分散を強く推奨、BL で減量バイアス)
      >= 15%: warn
      <  15%: ok

    Returns:
        {
          'financial_ratio':    金融資産内比率,
          'total_ratio':        人的資本込み総資産比率,
          'risk_level':         'ok' | 'warn' | 'critical',
          'message':            日本語メッセージ,
          'bl_reduction_bias':  critical で {ticker: -negative_view_pct} 辞書,
          'recommended_action': str,
        }
    """
    # 個別持ちの 9999.T 評価額
    employer_direct = float(positions.get(EMPLOYER_TICKER, 0.0))
    employer_financial = employer_direct + float(espp_value)
    financial_ratio = (employer_financial / portfolio_total) if portfolio_total > 0 else 0.0

    # 人的資本 PV（簡略: 年収 × 残存年数 × 0.7）
    human_capital_pv = annual_salary * years_to_retirement * 0.7 if include_human_capital and annual_salary > 0 else 0.0
    total_wealth = portfolio_total + human_capital_pv
    total_exposure = (employer_financial + human_capital_pv) / total_wealth if total_wealth > 0 else 0.0

    if financial_ratio >= EMPLOYER_CRITICAL_RATIO:
        level = 'critical'
        msg = (f'🚨 雇用主 ({EMPLOYER_TICKER}) 金融資産比率 {financial_ratio*100:.1f}%: 25% 超えクリティカル。'
               f'職・年収・持株が同一経済イベントに連動します。持株会の拠出停止 + 売却 + セクター分散を強く推奨。')
        action = '持株会拠出停止 + 9999.T 段階売却 + 他業種への分散'
        bl_bias = {EMPLOYER_TICKER: -0.10}   # 10% 下方 view を注入
    elif financial_ratio >= EMPLOYER_WARN_RATIO:
        level = 'warn'
        msg = (f'⚠️  雇用主 ({EMPLOYER_TICKER}) 金融資産比率 {financial_ratio*100:.1f}%: 15% 警戒ゾーン。'
               f'人的資本 ({human_capital_pv/1e8:.1f}億円 PV) と合算で {total_exposure*100:.1f}% が雇用主株式に依存。')
        action = '持株会の拠出額削減、新規 9999.T 購入の停止'
        bl_bias = {EMPLOYER_TICKER: -0.05}
    else:
        level = 'ok'
        msg = f'雇用主 ({EMPLOYER_TICKER}) 集中度 {financial_ratio*100:.1f}%: 健全'
        action = ''
        bl_bias = {}

    return {
        'employer_ticker':    EMPLOYER_TICKER,
        'financial_ratio':    round(financial_ratio, 4),
        'total_ratio':        round(total_exposure, 4),
        'employer_direct_jpy':  round(employer_direct, 0),
        'employer_plan_jpy':    round(espp_value, 0),
        'human_capital_pv_jpy': round(human_capital_pv, 0),
        'risk_level':         level,
        'message':            msg,
        'recommended_action': action,
        'bl_reduction_bias':  bl_bias,
        'thresholds': {
            'warn':     EMPLOYER_WARN_RATIO,
            'critical': EMPLOYER_CRITICAL_RATIO,
        },
    }


# ============================================================
# 8. ストレステスト
# ============================================================

def run_stress_test(
    positions: dict,
    portfolio_total: float,
    fx_rate: Optional[float] = None,
) -> dict:
    if fx_rate is None:
        from utils import get_fx_rate_cached
        fx_rate, _ = get_fx_rate_cached()
    """
    事前定義シナリオでポートフォリオへの影響を試算

    Args:
        positions: {ticker: {'value_jpy': ..., 'currency': 'USD'/'JPY'}}
        portfolio_total: ポートフォリオ総額（円）
        fx_rate: 現在のUSD/JPYレート

    Returns:
        {
          scenario_name: {
            'loss_jpy': 損失額（円）,
            'loss_pct': 損失率（%）,
            'survival': 生存評価,
          }
        }
    """
    results = {}

    for scenario_name, shocks in STRESS_SCENARIOS.items():
        total_loss_jpy = 0

        for ticker, pos_info in positions.items():
            value_jpy = pos_info.get('value_jpy', 0)
            currency  = pos_info.get('currency', 'JPY')

            # 個別銘柄ショック
            if ticker in shocks:
                total_loss_jpy += value_jpy * abs(shocks[ticker])

            # S&P500 相関ショック（米国株は70%相関を仮定）
            if 'SP500' in shocks and currency == 'USD' and ticker not in shocks:
                corr_factor = 0.70
                total_loss_jpy += value_jpy * abs(shocks['SP500']) * corr_factor

            # 円高ショック（USD建て資産は為替損が発生）
            if 'USDJPY' in shocks and currency == 'USD':
                fx_impact = abs(shocks['USDJPY'])
                total_loss_jpy += value_jpy * fx_impact

        loss_pct = total_loss_jpy / portfolio_total if portfolio_total > 0 else 0

        results[scenario_name] = {
            'loss_jpy': round(total_loss_jpy, 0),
            'loss_pct': round(loss_pct, 4),
            'survival': (
                'critical' if loss_pct > 0.35 else
                'warning'  if loss_pct > 0.25 else
                'normal'
            ),
        }

    return results


# ============================================================
# 9. 相関行列
# ============================================================

def calculate_correlation_matrix(returns_df: pd.DataFrame) -> pd.DataFrame:
    """
    各銘柄間の相関行列を計算

    Args:
        returns_df: 各銘柄のリターン系列（columns=銘柄名）

    Returns:
        相関行列（pd.DataFrame）
    """
    return returns_df.dropna().corr().round(4)


# ============================================================
# 10. 行動ガードレール評価
# ============================================================

def evaluate_behavioral_guardrails(
    daily_pnl_pct: float,
    monthly_pnl_pct: float,
    active_trades: int,
    short_positions: int,
) -> dict:
    """
    行動ガードレールの現在状態を評価

    Returns:
        {
          'new_entry_allowed': 新規エントリー可否,
          'trading_allowed': 取引可否,
          'alerts': アラートリスト,
        }
    """
    alerts = []
    new_entry_allowed = True
    trading_allowed   = True

    # Keep this legacy risk evaluator aligned with behavioral_guard.py.  The
    # previous fixed value (3) made Auto Tune appear effective in one path but
    # not in this one.
    max_short_positions = BEHAVIORAL_GUARDRAILS['max_short_positions']
    try:
        from tunable_params import get as _tp_get
        max_short_positions = int(_tp_get('max_short_positions', max_short_positions))
    except Exception:
        pass

    if daily_pnl_pct <= BEHAVIORAL_GUARDRAILS['daily_loss_limit']:
        new_entry_allowed = False
        alerts.append({
            'level':   'warning',
            'message': f'本日P&L {daily_pnl_pct*100:.1f}%（-4%閾値）: 新規エントリー禁止',
        })

    if monthly_pnl_pct <= BEHAVIORAL_GUARDRAILS['monthly_loss_limit']:
        trading_allowed = False
        alerts.append({
            'level':   'critical',
            'message': f'月間P&L {monthly_pnl_pct*100:.1f}%（-8%閾値）: トレード停止',
        })

    if short_positions >= max_short_positions:
        alerts.append({
            'level':   'info',
            'message': f'空売りポジション数 {short_positions}/{max_short_positions}上限に到達',
        })

    return {
        'new_entry_allowed': new_entry_allowed,
        'trading_allowed':   trading_allowed,
        'alerts':            alerts,
        'guardrails':        {**BEHAVIORAL_GUARDRAILS, 'max_short_positions': max_short_positions},
    }


# ============================================================
# 11. Sharpe比・各種パフォーマンス指標
# ============================================================

def calculate_performance_metrics(returns: pd.Series, risk_free_rate: float = 0.001) -> dict:
    """
    Sharpe比・Sortino比・カルマー比等の主要パフォーマンス指標

    Args:
        returns: 日次リターン系列
        risk_free_rate: 無リスク金利（日次換算、デフォルト年率0.1%）

    Returns:
        各指標のdict
    """
    returns = returns.dropna()
    if len(returns) < 20:
        return {'error': 'データ不足'}

    rf_daily      = risk_free_rate / 252
    excess_returns = returns - rf_daily

    annualized_return = returns.mean() * 252
    annualized_vol    = returns.std() * np.sqrt(252)
    sharpe            = excess_returns.mean() / excess_returns.std() * np.sqrt(252) if excess_returns.std() > 0 else 0

    # Sortino（下方リスクのみ）
    downside_returns = excess_returns[excess_returns < 0]
    downside_vol     = downside_returns.std() * np.sqrt(252) if len(downside_returns) > 0 else 1e-9
    sortino          = excess_returns.mean() * 252 / downside_vol if downside_vol > 0 else 0

    # カルマー
    dd_result = calculate_drawdown(returns)
    max_dd    = abs(dd_result['max_dd'])
    calmar    = annualized_return / max_dd if max_dd > 0 else 0

    return {
        'annualized_return': round(annualized_return, 4),
        'annualized_vol':    round(annualized_vol, 4),
        'sharpe_12m':        round(sharpe, 4),
        'sortino':           round(sortino, 4),
        'calmar':            round(calmar, 4),
        'max_dd':            round(dd_result['max_dd'], 4),
        'current_dd':        round(dd_result['current_dd'], 4),
        'win_rate':          round((returns > 0).mean(), 4),
        'observations':      len(returns),
    }


# ============================================================
# Part E-2: Volatility Targeting
# ============================================================
# 目標年率 vol を 15% に置き、予測 vol / target の比率でポジション・サイズを
# 動的スケールする。GARCH or realized vol を入力にできるよう、predicted_vol は
# 呼び出し元（portfolio_optimizer / analyst）が算出してこの関数に渡す。
#
# - ratio > 1.2 → 全ポジション 0.85× （信用建玉を優先縮小）
# - ratio < 0.8 → 全ポジション 1.1× （上限 1.2×）
# - 0.8 <= ratio <= 1.2 → 1.0 （ニュートラル）
# - 日次変更幅 ±15% にクランプ（whipsaw 防止）
# 結果は state file に保存して翌日の clamp 判定に使用。

from pathlib import Path as _VTPath
import json as _vt_json

TARGET_ANNUAL_VOL: float = 0.15
VOL_TARGET_STATE_FILE = _VTPath(__file__).parent / "vol_target_state.json"
_VT_DAILY_CLAMP = 0.15


def _vt_load_prev() -> dict:
    if not VOL_TARGET_STATE_FILE.exists():
        return {}
    try:
        return _vt_json.loads(VOL_TARGET_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _vt_save(state: dict) -> None:
    try:
        VOL_TARGET_STATE_FILE.write_text(
            _vt_json.dumps(state, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def compute_vol_target_scale(
    predicted_annual_vol: float,
    target_annual_vol: float = TARGET_ANNUAL_VOL,
    persist: bool = True,
) -> dict:
    """
    予測年率 vol → 全ポジション共通のスケーラー (0.7〜1.2) を返す。

    戻り値:
      {
        'scale':         float (クランプ後),
        'raw_scale':     float (クランプ前),
        'ratio':         predicted / target,
        'regime':        'high_vol' | 'normal' | 'low_vol',
        'prev_scale':    float (昨日の値),
        'clamp_applied': bool,
      }
    """
    prev = _vt_load_prev()
    prev_scale = float(prev.get("scale", 1.0))

    if predicted_annual_vol is None or predicted_annual_vol <= 0 or target_annual_vol <= 0:
        return {
            "scale":         prev_scale,
            "raw_scale":     prev_scale,
            "ratio":         None,
            "regime":        "unknown",
            "prev_scale":    prev_scale,
            "clamp_applied": False,
            "note":          "invalid vol input; returning prev scale",
        }

    ratio = float(predicted_annual_vol) / float(target_annual_vol)

    if ratio > 1.2:
        raw = 0.85
        regime = "high_vol"
    elif ratio < 0.8:
        # ratio が 0.5 → 1.2, 0.8 → 1.0 の線形補間 (上限 1.2)
        raw = min(1.2, 1.0 + (0.8 - ratio) * 0.5)
        regime = "low_vol"
    else:
        raw = 1.0
        regime = "normal"

    # 日次変更幅クランプ
    upper = prev_scale * (1.0 + _VT_DAILY_CLAMP)
    lower = prev_scale * (1.0 - _VT_DAILY_CLAMP)
    scale = max(lower, min(upper, raw))
    clamp_applied = abs(scale - raw) > 1e-6

    result = {
        "scale":         round(scale, 4),
        "raw_scale":     round(raw, 4),
        "ratio":         round(ratio, 4),
        "regime":        regime,
        "prev_scale":    round(prev_scale, 4),
        "clamp_applied": clamp_applied,
        "target_vol":    target_annual_vol,
        "predicted_vol": round(predicted_annual_vol, 4),
    }
    if persist:
        _vt_save({
            "scale":         result["scale"],
            "raw_scale":     result["raw_scale"],
            "ratio":         result["ratio"],
            "regime":        result["regime"],
            "updated_at":    pd.Timestamp.utcnow().isoformat(),
        })
    return result

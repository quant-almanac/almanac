"""
WFO最適化結果に基づくレジーム別パラメータ
生成: 2026-02-25 | backtest_wfo.py結果より

A_強気: WFO全期間統合パラメータ（440-478件ベース、過剰適合を避ける）
B_中立: レジーム別最適化結果
C_弱気: B_中立流用（件数不足のため）
"""

REGIME_PARAMS = {
    '逆張り': {
        'US': {
            'A_強気': {'rsi': 30, 'vol': 1.2, 'mom5d': -5,  'hold': 20, 'stop_mult': 1.5},  # WFO統合
            'B_中立': {'rsi': 25, 'vol': 1.3, 'mom5d': -8,  'hold': 10, 'stop_mult': 1.5},  # 緩和: rsi20→25, vol1.5→1.3
            'C_弱気': {'rsi': 28, 'vol': 1.3, 'mom5d': -8,  'hold': 10, 'stop_mult': 1.5},  # 緩和: より発見しやすく
        },
        'JP': {
            # 2026-05-16: 日本株は米国より値動き小さく RSI<30 まで売られるのが稀。+5pt 緩和して候補拡張。
            'A_強気': {'rsi': 35, 'vol': 1.1, 'mom5d': -8,  'hold': 15, 'stop_mult': 2.0},
            'B_中立': {'rsi': 35, 'vol': 1.3, 'mom5d': -6,  'hold': 15, 'stop_mult': 1.5},
            'C_弱気': {'rsi': 35, 'vol': 1.3, 'mom5d': -6,  'hold': 15, 'stop_mult': 1.5},
        },
    },
    'モメンタム': {
        'US': {
            'A_強気': {'rsi_min': 55, 'ma50_min': 8, 'ma50_max': 20, 'vol': 1.5, 'hold': 14, 'stop_mult': 1.5},  # vol緩和
            'B_中立': {'rsi_min': 45, 'ma50_min': 5, 'ma50_max': 18, 'vol': 1.2, 'hold': 5,  'stop_mult': 2.5},  # 緩和: rsi50→45, ma50_min8→5, vol1.5→1.2
            'C_弱気': None,
        },
        'JP': {
            # 2026-05-16: 米国基準と同等に rsi_min/vol 緩和。日本株低ボラに合わせ ma50_max 緩和。
            'A_強気': {'rsi_min': 55, 'ma50_min': 5, 'ma50_max': 18, 'vol': 1.5, 'hold': 7,  'stop_mult': 2.0},
            'B_中立': {'rsi_min': 48, 'ma50_min': 3, 'ma50_max': 18, 'vol': 1.2, 'hold': 14, 'stop_mult': 2.5},
            'C_弱気': None,  # WFO PF=0.73 → 無効
        },
    },
    'ギャップダウン': {
        'US': {
            'A_強気': {'gap': -5, 'vol': 1.2, 'hold': 10, 'stop_mult': 1.5},  # WFO統合(-5が安定)
            'B_中立': {'gap': -5, 'vol': 1.2, 'hold': 10, 'stop_mult': 1.5},  # 緩和: gap-7→-5
            'C_弱気': {'gap': -4, 'vol': 1.5, 'hold': 7,  'stop_mult': 2.5},  # 緩和: vol2.0→1.5
        },
        'JP': {
            'A_強気': {'gap': -5, 'vol': 1.2, 'hold': 5,  'stop_mult': 2.0},
            'B_中立': {'gap': -3, 'vol': 1.5, 'hold': 10, 'stop_mult': 2.0},  # 緩和: vol2.5→1.5
            'C_弱気': {'gap': -5, 'vol': 1.2, 'hold': 10, 'stop_mult': 1.5},  # 緩和: gap-7→-5, vol1.5→1.2
        },
    },
    'イベントドリブン後': {
        'US': {
            'A_強気': {'change': -7,  'vol': 2.0, 'hold': 7,  'stop_mult': 2.0},  # WFO統合(件数確保)
            'B_中立': {'change': -10, 'vol': 2.0, 'hold': 5,  'stop_mult': 2.0},  # 緩和: change-15→-10, vol3.0→2.0
            'C_弱気': {'change': -7,  'vol': 1.5, 'hold': 14, 'stop_mult': 2.5},  # 緩和: vol2.0→1.5
        },
        'JP': {
            'A_強気': {'change': -5, 'vol': 2.0, 'hold': 14, 'stop_mult': 1.5},  # WFO統合
            'B_中立': {'change': -5, 'vol': 2.0, 'hold': 14, 'stop_mult': 1.5},  # 緩和: vol3.0→2.0
            'C_弱気': {'change': -5, 'vol': 2.0, 'hold': 14, 'stop_mult': 1.5},  # 緩和: vol3.0→2.0
        },
    },
}

def get_params(strategy, market, regime):
    """レジームに応じたパラメータを返す。Noneの場合はシグナルなし"""
    return REGIME_PARAMS.get(strategy, {}).get(market, {}).get(regime)

def get_regime(macro_score, spy_above_ma50):
    """マクロスコアとSPY位置からレジームを判定"""
    if macro_score <= 2:
        return 'C_弱気'
    elif macro_score >= 7 and spy_above_ma50:
        return 'A_強気'
    else:
        return 'B_中立'

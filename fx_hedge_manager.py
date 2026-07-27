"""
A-4: Dynamic FX Hedge Manager
-----------------------------
regime × VIX × IV で目標ヘッジ比率を計算し、受動的（JPY ヘッジ付き ETF）/
能動的（先物）の提案を返す。固定比率を避け、状況依存で 0〜70% レンジで動く。

whipsaw 防止のため日次変更幅 ±10% にクランプ。

Stage 3 是正 (2026-07): 以下は配線しない (このモジュールは現状どこからも
import されていない、スタンドアロン CLI 専用)。
  - 「簡易（JPY 積み増し）」を提案手段から削除した。通貨配分の変更であって
    ヘッジ overlay ではなく、実装もされていなかったため。
  - usdjpy_mom_1m は検証済み係数もバックテストも無いため比率計算に使わない
    (受け取って何もしない no-op 分岐だったものを削除。パラメータ自体は
    後方互換のため残すが、inputs への記録のみで比率には影響しない —
    delegate 前提の破壊的 API 変更を避けるため)。
  - 商品リストを JPX・発行体資料 (2026-07 時点) で再確認して是正:
    1655.T は「円建て」であって「円ヘッジ」ではない (無ヘッジ) ため除外。
    2631.T も無ヘッジ (ヘッジ版は 2632.T) のため除外し 2632.T に差し替え。
    1545.T は無ヘッジ (ヘッジ版は 2845) のため除外。
    2040.T は NYダウ2倍ブルの ETN (レバレッジ商品) であり、為替ヘッジ目的の
    素朴な JPY ヘッジ付き ETF ではないため除外。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR        = Path(__file__).parent
HEDGE_STATE     = BASE_DIR / 'hedge_target.json'

# ── 境界 ──────────────────────────────────────────────
MIN_HEDGE  = 0.00
MAX_HEDGE  = 0.70
MAX_DAILY_DELTA = 0.10   # 日次変更幅

# ── 追加トリガーの閾値 ─────────────────────────────────
JPY_WEAKNESS_VS_90SMA = 0.08   # 90 日 SMA +8% 超 → +10%
JPY_WEAKNESS_VS_5Y    = 0.25   # 5 年平均 +25% 超 → +10%

# 受動的ヘッジ候補（JPY ヘッジ付き ETF。2026-07 に JPX・発行体資料で確認済み）
PASSIVE_HEDGE_ETFS = {
    'sp500':  ['2634.T'],   # NEXT FUNDS S&P500指数(為替ヘッジあり)
    'nasdaq': ['2632.T'],   # MAXISナスダック100上場投信(為替ヘッジあり)
}

# アクティブヘッジ候補
#
# 6J (CME 日本円先物) は「USD per JPY」建て — 一般的な「USDJPY」相場
# （JPY per USD）とは逆向きの気配値。円高（USDJPY 下落）で 6J は上昇するため、
# USD 資産の円高ヘッジとして機能させるには 6J は買い建てが正しい
# (他の2命令「USDJPY 売」とは表記上の建玉方向が逆になるが、経済的には
# 同一方向のヘッジ)。旧「6J 先物売」は方向が逆で、ヘッジどころか
# 損失を増幅させる誤りだった。
ACTIVE_HEDGE_INSTRUMENTS = [
    'CME 6J 先物買い（標準サイズ 12.5M JPY、USDJPY建てとは逆気配のため買いが円高ヘッジ）',
    'くりっく365 USDJPY 売',
    'IG 証券 / GMOクリック USDJPY 売（CFD）',
]


# ============================================================
# state
# ============================================================

def _load_state() -> dict:
    if HEDGE_STATE.exists():
        try:
            return json.loads(HEDGE_STATE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {'last_target': 0.0, 'history': []}


def _save_state(state: dict) -> None:
    tmp = HEDGE_STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(HEDGE_STATE)


# ============================================================
# core: target 比率の算出
# ============================================================

def _base_target_from_regime(regime: str, vix: float, usdjpy_iv_1m: float) -> float:
    """
    regime ∈ {'bull','neutral','bear','crisis'} と VIX / IV の組合せでベース目標比率。
    """
    r = (regime or 'neutral').lower()
    if r == 'bull' and vix < 20:
        return 0.00
    if r == 'neutral' and 20 <= vix < 25:
        return 0.10
    if r == 'neutral' and 25 <= vix < 35:
        return 0.25
    if r == 'bear' and (vix > 30 or usdjpy_iv_1m > 0.12):
        return 0.40
    if r == 'crisis':
        return 0.60
    # fallback: 25
    if vix >= 30 or usdjpy_iv_1m > 0.12:
        return 0.30
    if vix >= 25:
        return 0.20
    if vix >= 20:
        return 0.10
    return 0.0


def compute_target_hedge_ratio(
    regime: str,
    vix: float,
    usdjpy: float,
    usdjpy_iv_1m: float = 0.10,
    usdjpy_mom_1m: float = 0.0,
    usdjpy_sma_90d: Optional[float] = None,
    usdjpy_avg_5y: Optional[float] = None,
    current_hedge_ratio: Optional[float] = None,
) -> dict:
    """
    目標ヘッジ比率を算出する。

    Args:
        regime:          'bull' | 'neutral' | 'bear' | 'crisis'
        vix:             VIX 指数
        usdjpy:          USD/JPY スポット
        usdjpy_iv_1m:    USDJPY 1 ヶ月 IV（小数、0.12 = 12%）
        usdjpy_mom_1m:   [非推奨・比率計算には使用しない] USDJPY 月次モメンタム
                         （小数、-0.05 = -5%）。検証済み係数もバックテストも
                         無いため比率へは反映しない。inputs への記録のみ。
                         後方互換のため signature には残すが、将来的に
                         有効なモデルへ差し替わるまで無効のまま。
        usdjpy_sma_90d:  USDJPY 90 日 SMA
        usdjpy_avg_5y:   USDJPY 5 年平均
        current_hedge_ratio: 前回の目標比率（whipsaw 防止用）

    Returns:
        {
          'target_hedge_ratio':   最終クランプ済比率,
          'raw_target':           クランプ前,
          'base_target':          regime ベース,
          'addons':               {reason: +値, ...},
          'rationale':            人間可読説明,
          'method':               受動 / 能動の提案,
          'inputs':               入力サマリ,
          'delta_vs_prev':        前回からの変化,
        }
    """
    base = _base_target_from_regime(regime, vix, usdjpy_iv_1m)

    addons: dict = {}

    # 円安過熱 1: 90 日 SMA +8% 超
    if usdjpy_sma_90d and usdjpy_sma_90d > 0:
        ratio_vs_sma = usdjpy / usdjpy_sma_90d - 1
        if ratio_vs_sma >= JPY_WEAKNESS_VS_90SMA:
            addons[f'JPY 90d SMA +{ratio_vs_sma*100:.1f}%'] = 0.10

    # 円安過熱 2: 5 年平均 +25% 超
    if usdjpy_avg_5y and usdjpy_avg_5y > 0:
        ratio_vs_5y = usdjpy / usdjpy_avg_5y - 1
        if ratio_vs_5y >= JPY_WEAKNESS_VS_5Y:
            addons[f'JPY 5y avg +{ratio_vs_5y*100:.1f}%'] = 0.10

    raw_target = base + sum(addons.values())
    clamped = max(MIN_HEDGE, min(MAX_HEDGE, raw_target))

    # 日次変化幅クランプ
    if current_hedge_ratio is None:
        state = _load_state()
        current_hedge_ratio = float(state.get('last_target', 0.0))

    delta = clamped - current_hedge_ratio
    if abs(delta) > MAX_DAILY_DELTA:
        delta = MAX_DAILY_DELTA if delta > 0 else -MAX_DAILY_DELTA
        smoothed = current_hedge_ratio + delta
    else:
        smoothed = clamped

    smoothed = round(max(MIN_HEDGE, min(MAX_HEDGE, smoothed)), 4)

    # 受動 / 能動の提案
    method = _recommend_method(smoothed, regime, usdjpy_iv_1m)

    # rationale
    addon_text = ', '.join(f'{k}=+{v*100:.0f}%' for k, v in addons.items()) or 'なし'
    rationale = (
        f'regime={regime} VIX={vix:.1f} USDJPY_IV={usdjpy_iv_1m*100:.1f}% → '
        f'base={base*100:.0f}% + [{addon_text}] = {raw_target*100:.0f}% '
        f'→ clamp={clamped*100:.0f}% → daily={smoothed*100:.0f}% '
        f'(prev={current_hedge_ratio*100:.0f}%)'
    )

    result = {
        'target_hedge_ratio': smoothed,
        'raw_target':         round(raw_target, 4),
        'base_target':        round(base, 4),
        'clamped_target':     round(clamped, 4),
        'addons':             addons,
        'delta_vs_prev':      round(smoothed - current_hedge_ratio, 4),
        'rationale':          rationale,
        'method':             method,
        'inputs': {
            'regime':          regime,
            'vix':             vix,
            'usdjpy':          usdjpy,
            'usdjpy_iv_1m':    usdjpy_iv_1m,
            'usdjpy_mom_1m':   usdjpy_mom_1m,
            'usdjpy_sma_90d':  usdjpy_sma_90d,
            'usdjpy_avg_5y':   usdjpy_avg_5y,
        },
        'as_of': datetime.now().isoformat(),
    }
    return result


def _recommend_method(target: float, regime: str, iv: float) -> dict:
    """実装手段の推奨"""
    if target < 0.05:
        return {
            'primary':      'none',
            'description':  'ヘッジ不要（現状維持）',
            'instruments':  [],
        }

    # 受動優先: 低～中比率、個人口座向き
    if target <= 0.30:
        return {
            'primary':      'passive',
            'description':  'JPY ヘッジ付き ETF に S&P500 相当の一部を移管',
            'instruments':  PASSIVE_HEDGE_ETFS['sp500'] + PASSIVE_HEDGE_ETFS['nasdaq'],
            'rationale':    'コスト低・税務単純・反転対応が容易',
        }

    # 中～高比率: 能動も検討（IV が高い場合はオプション売りもペイ）
    if target <= 0.50:
        return {
            'primary':      'passive_or_active',
            'description':  'ヘッジ付き ETF 拡大 or USDJPY 先物売（CFD/くりっく365）',
            'instruments':  PASSIVE_HEDGE_ETFS['sp500'] + ACTIVE_HEDGE_INSTRUMENTS[:2],
            'rationale':    '比率が 30% 超では能動手段の方が機動的、ただしロールコストに注意',
        }

    # 高比率（crisis 相当）
    return {
        'primary':      'active',
        'description':  'USDJPY 先物売 / CFD で比率 50%+ を機動的に構築',
        'instruments':  ACTIVE_HEDGE_INSTRUMENTS,
        'rationale':    f'Crisis / IV={iv*100:.0f}%: 受動 ETF では追随遅延、能動ヘッジで即時に比率を上げる',
    }


def persist_target(result: dict) -> None:
    """次回日次変化幅クランプのため state を保存"""
    state = _load_state()
    state['last_target'] = result['target_hedge_ratio']
    state.setdefault('history', []).append({
        'as_of':  result['as_of'],
        'target': result['target_hedge_ratio'],
        'inputs': result['inputs'],
    })
    # 直近 60 件のみ保持
    state['history'] = state['history'][-60:]
    _save_state(state)


# ============================================================
# rebalance 提案（現状比率 vs 目標）
# ============================================================

def suggest_hedge_rebalance(
    target: float,
    current: float,
    portfolio_value_jpy: float,
    usd_exposure_jpy: float,
    delta_threshold: float = 0.02,
) -> Optional[dict]:
    """
    現状の USD 無ヘッジ比率と目標比率の差が delta_threshold を超えるなら
    priority_actions 向けの提案を返す。
    """
    diff = target - current
    if abs(diff) < delta_threshold:
        return None

    # ヘッジすべき/解除すべき金額
    hedge_amount_jpy = diff * usd_exposure_jpy
    direction = 'increase' if diff > 0 else 'decrease'

    return {
        'direction':         direction,
        'target_ratio':      target,
        'current_ratio':     current,
        'diff':              round(diff, 4),
        'hedge_amount_jpy':  round(hedge_amount_jpy, 0),
        'urgency':           'high' if abs(diff) >= 0.20 else ('medium' if abs(diff) >= 0.10 else 'low'),
        'message': (
            f'FX ヘッジ比率を {current*100:.0f}% → {target*100:.0f}% に '
            f'{"増加" if direction=="increase" else "縮小"} '
            f'（対象 USD エクスポージャ ¥{hedge_amount_jpy/10000:,.0f}万）'
        ),
    }


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'compute'

    if cmd == 'compute':
        # デフォルト: macro_state.json から regime と VIX を取得
        try:
            macro = json.loads((BASE_DIR / 'macro_state.json').read_text(encoding='utf-8'))
        except Exception:
            macro = {}
        regime = (sys.argv[2] if len(sys.argv) > 2 else macro.get('regime', 'neutral')).lower()
        vix    = float(sys.argv[3]) if len(sys.argv) > 3 else float(macro.get('vix', 18))
        usdjpy = float(sys.argv[4]) if len(sys.argv) > 4 else float(macro.get('usdjpy', 150))

        result = compute_target_hedge_ratio(
            regime=regime, vix=vix, usdjpy=usdjpy,
            usdjpy_iv_1m=float(macro.get('usdjpy_iv_1m', 0.10)),
            usdjpy_mom_1m=float(macro.get('usdjpy_mom_1m', 0.0)),
            usdjpy_sma_90d=macro.get('usdjpy_sma_90d'),
            usdjpy_avg_5y=macro.get('usdjpy_avg_5y'),
        )
        persist_target(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == 'status':
        state = _load_state()
        print(json.dumps(state, ensure_ascii=False, indent=2))

    else:
        print('Usage: fx_hedge_manager.py [compute|status] [regime vix usdjpy]')

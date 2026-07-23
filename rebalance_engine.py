"""
ALMANAC v4.0 - リバランスエンジン
通貨配分・セクター配分の逸脱検出と優先順位付き売買指示を生成する。
"""

import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from utils import atomic_write_json

BASE_DIR = Path(__file__).parent

# ============================================================
# 目標配分・閾値
# ============================================================

CURRENCY_TARGETS = {
    'USD': {'min': 0.60, 'max': 0.70, 'ideal': 0.65},
    'JPY': {'min': 0.30, 'max': 0.40, 'ideal': 0.35},
}

SECTOR_TARGETS = {
    'Technology':         {'max': 0.35, 'ideal': 0.30},
    'Financial Services': {'max': 0.20, 'ideal': 0.15},
    'Healthcare':         {'max': 0.20, 'ideal': 0.15},
    'Industrials':        {'max': 0.15, 'ideal': 0.10},
    'Basic Materials':    {'max': 0.20, 'ideal': 0.15},
    'Energy':             {'max': 0.10, 'ideal': 0.05},
    'Consumer Defensive': {'max': 0.10, 'ideal': 0.05},
    'Other':              {'max': 0.15, 'ideal': 0.05},
}

# リバランストリガー閾値
CURRENCY_TRIGGER    = 0.05   # ±5%逸脱でトリガー
SECTOR_TRIGGER      = 0.35   # 35%超でトリガー

# 最小取引額（総資産比率）。これ未満の単発推奨は deferred=True で繰越扱いにし、
# 1株売買の連発と手数料負けを抑制する。Phase 1 改修。
MIN_TRADE_PCT       = 0.005  # 総資産の0.5% (¥10M なら ¥50K)
MIN_TRADE_FLOOR_JPY = 50_000 # 総資産が小さい場合のフロア（¥5万未満は常時繰越）

# 欧州集中リスク（EWG + IEV + EPOL の合計ポートフォリオ比率）
EUROPEAN_TICKERS    = {'EWG', 'IEV', 'EPOL'}
EUROPEAN_THRESHOLD  = 0.15   # 15%超で地政学集中警告

# NISA口座キーワード（売りリバランス対象外）
NISA_ACCOUNT_KEYWORDS = {'NISA', 'nisa', 'つみたて', '成長投資枠'}

# 調整優先順位（1=最優先）
# 1: 新規資金で不足資産を購入
# 2: 利確時の再投資先を変更
# 3: 強制売却（最終手段）
REBALANCE_PRIORITY = {
    'buy_underweight':       1,
    'redirect_new_cash':     1,
    'avoid_overweight_buy':  2,
    'sell_overweight':       3,
}


# ============================================================
# コア計算
# ============================================================

def _load_nisa_data() -> dict:
    path = BASE_DIR / 'nisa_portfolio.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


# リバランス対象tier。通貨/セクター判定はこのtierに限定する。
# 地理的集中度 (analyze_geographic_concentration) と NISA売却保護
# (check_nisa_sell_protection) はポートフォリオ全体を見るべき懸念のため、
# これらには build_core_snapshot() の出力ではなく元の snapshot を渡すこと。
CORE_REBALANCE_TIER = 'long'

# 持株会 (勤務先) 銘柄: settlement window 5〜10営業日で売買自由度が低く、月次積立で
# 自動的に増え続けるため、通貨/セクター配分・リバランス目標の「母数」から除外する
# (2026-07-07)。除外は配分計算上のみで、集中度リスク・地理的集中・NISA保護など
# ポートフォリオ全体を見るべき判定は元 snapshot を使うため影響しない。
EMPLOYER_STOCK_TICKERS = {'9999.T'}


def build_core_snapshot(snapshot: dict, *, investment_type: str = CORE_REBALANCE_TIER,
                        exclude_employer: bool = True) -> dict:
    """
    指定 investment_type (デフォルト 'long') のみで再集計したスナップショットを返す。

    api/routes/rebalance.py と calculate_rebalance_actions() の両方がこの関数を
    共通ヘルパーとして使うことで、通貨/セクター判定のtierスコープを一元管理する。

    exclude_employer=True (デフォルト) で持株会銘柄 (EMPLOYER_STOCK_TICKERS) を
    母数から除外する — 売買自由度の低い自動積立資産が JPY 配分を占有し、
    他の日本株買いのヘッドルームを潰すのを防ぐ。
    """
    positions = [
        p for p in snapshot.get('positions', [])
        if p.get('investment_type') == investment_type
        and not (exclude_employer and str(p.get('ticker') or '') in EMPLOYER_STOCK_TICKERS)
    ]
    total = sum(p.get('value_jpy', 0) for p in positions)

    cur_vals: dict = {}
    for p in positions:
        cur = p.get('currency', 'USD')
        cur_vals[cur] = cur_vals.get(cur, 0) + p.get('value_jpy', 0)
    currency_breakdown = {
        cur: {'value_jpy': val, 'ratio': round(val / total, 4) if total > 0 else 0}
        for cur, val in cur_vals.items()
    }

    sec_vals: dict = {}
    for p in positions:
        sec = p.get('sector', 'Other') or 'Other'
        sec_vals[sec] = sec_vals.get(sec, 0) + p.get('value_jpy', 0)
    sector_breakdown = {
        sec: {'value_jpy': val, 'ratio': round(val / total, 4) if total > 0 else 0}
        for sec, val in sorted(sec_vals.items(), key=lambda x: -x[1])
    }

    return {
        **snapshot,
        'positions': positions,
        'total_jpy': total,
        'currency_breakdown': currency_breakdown,
        'sector_breakdown': sector_breakdown,
    }


def analyze_currency_balance(snapshot: dict, targets: dict | None = None) -> dict:
    """
    通貨配分の逸脱を分析する。

    Args:
        snapshot: portfolio スナップショット (core/long 限定を渡すこと)
        targets:  通貨目標 {'USD': {min,max,ideal}, ...}。None なら static CURRENCY_TARGETS。
                  AI 動的方針を適用する場合は currency_policy.resolve_effective_targets()
                  の解決結果を呼び出し側が渡す (fail-closed で static に戻る)。

    Returns:
        {
          'status': 'ok' / 'warning' / 'action_needed',
          'currencies': {USD: {ratio, target_min, target_max, deviation, ...}, ...},
          'actions': [アクションリスト],
        }
    """
    targets     = targets if targets is not None else CURRENCY_TARGETS
    total       = snapshot.get('total_jpy', 0)
    cb          = snapshot.get('currency_breakdown', {})
    actions     = []
    worst_level = 'ok'

    currencies = {}
    for ccy, target in targets.items():
        vals     = cb.get(ccy, {'value_jpy': 0, 'ratio': 0})
        ratio    = vals['ratio']
        value    = vals['value_jpy']
        t_min    = target['min']
        t_max    = target['max']
        t_ideal  = target['ideal']
        deviation = ratio - t_ideal

        if ratio < t_min:
            level = 'action_needed'
            diff_jpy = (t_ideal - ratio) * total
            actions.append({
                'priority': REBALANCE_PRIORITY['buy_underweight'],
                'level':    'warning',
                'type':     'buy',
                'currency': ccy,
                'message':  f'{ccy}比率が{ratio*100:.1f}%（目標{t_min*100:.0f}〜{t_max*100:.0f}%）。'
                            f'¥{diff_jpy/10000:.0f}万分 {ccy}資産を追加購入推奨。',
                'amount_jpy': round(diff_jpy, 0),
            })
        elif ratio > t_max:
            level = 'action_needed'
            diff_jpy = (ratio - t_ideal) * total
            actions.append({
                'priority': REBALANCE_PRIORITY['avoid_overweight_buy'],
                'level':    'warning',
                'type':     'reduce',
                'currency': ccy,
                'message':  f'{ccy}比率が{ratio*100:.1f}%（目標{t_min*100:.0f}〜{t_max*100:.0f}%）。'
                            f'新規{ccy}購入を控え、¥{diff_jpy/10000:.0f}万分を他通貨へ。',
                'amount_jpy': round(diff_jpy, 0),
            })
        else:
            level = 'ok'

        currencies[ccy] = {
            'ratio':      round(ratio, 4),
            'value_jpy':  value,
            'target_min': t_min,
            'target_max': t_max,
            'deviation':  round(deviation, 4),
            'level':      level,
        }
        if level == 'action_needed':
            worst_level = 'action_needed'

    return {
        'status':     worst_level,
        'currencies': currencies,
        'actions':    sorted(actions, key=lambda x: x['priority']),
    }


def analyze_sector_balance(snapshot: dict) -> dict:
    """
    セクター配分の逸脱を分析する。

    Returns:
        {
          'status': 'ok' / 'warning' / 'action_needed',
          'sectors': {セクター名: {ratio, target, deviation, level}, ...},
          'actions': [アクションリスト],
        }
    """
    total   = snapshot.get('total_jpy', 0)
    sb      = snapshot.get('sector_breakdown', {})
    actions = []
    worst_level = 'ok'

    sectors = {}
    for sector, vals in sb.items():
        ratio    = vals['ratio']
        value    = vals['value_jpy']
        target   = SECTOR_TARGETS.get(sector, {'max': 0.20, 'ideal': 0.10})
        t_max    = target['max']
        t_ideal  = target['ideal']
        deviation = ratio - t_ideal

        if ratio > SECTOR_TRIGGER:
            level    = 'action_needed'
            diff_jpy = (ratio - t_ideal) * total
            worst_level = 'action_needed'
            actions.append({
                'priority':   REBALANCE_PRIORITY['avoid_overweight_buy'],
                'level':      'critical' if ratio > 0.50 else 'warning',
                'type':       'reduce',
                'sector':     sector,
                'message':    f'{sector}が{ratio*100:.1f}%と集中（閾値{SECTOR_TRIGGER*100:.0f}%）。'
                              f'新規購入を停止し、¥{diff_jpy/10000:.0f}万分を分散。',
                'amount_jpy': round(diff_jpy, 0),
            })
        elif ratio > t_max:
            level = 'warning'
            if worst_level == 'ok':
                worst_level = 'warning'
        else:
            level = 'ok'

        sectors[sector] = {
            'ratio':     round(ratio, 4),
            'value_jpy': value,
            'target':    t_ideal,
            'max':       t_max,
            'deviation': round(deviation, 4),
            'level':     level,
        }

    return {
        'status':  worst_level,
        'sectors': sectors,
        'actions': sorted(actions, key=lambda x: x['priority']),
    }


def _filter_micro_actions(
    actions:   list,
    total_jpy: float,
    min_pct:   float = MIN_TRADE_PCT,
) -> tuple:
    """
    細かすぎる売買推奨（手数料負けする少額アクション）を分離する。

    総資産の min_pct 未満の amount_jpy を持つ推奨を deferred 側に切り分ける。
    - active   側: 通常通り action_plan に積まれて発注対象
    - deferred 側: 'deferred=True' と 'defer_reason' を付与して繰越（次の発火で再評価）

    通貨アクションは「比率超過のため新規購入を控える」抑制ガイドが
    主目的なので、type=='reduce' でも金額判定は適用する（小額の reduce 警告も抑制）。
    """
    if total_jpy <= 0:
        return actions, []

    threshold = max(total_jpy * min_pct, MIN_TRADE_FLOOR_JPY)
    active, deferred = [], []
    for a in actions:
        amt = abs(float(a.get('amount_jpy') or 0))
        if amt and amt < threshold:
            a = dict(a)
            a['deferred']     = True
            a['defer_reason'] = (
                f'最小取引額 ¥{threshold/10000:.0f}万 未満 '
                f'(amount=¥{amt/10000:.1f}万) → 繰越'
            )
            deferred.append(a)
        else:
            active.append(a)
    return active, deferred


def calculate_rebalance_actions(
    snapshot:        dict,
    available_cash:  float = 0,
    monthly_budget:  float = 200_000,   # クレカ積立¥20万
    currency_targets: dict | None = None,
) -> dict:
    """
    具体的なリバランスアクションを生成する。

    Args:
        snapshot:       portfolio_manager.build_portfolio_snapshot() の出力
        available_cash: 追加投資可能な現金（円）
        monthly_budget: 毎月の新規投資予算（円）

    Returns:
        {
          'summary':         全体サマリー,
          'currency_result': 通貨配分分析,
          'sector_result':   セクター配分分析,
          'action_plan':     優先順位付きアクションリスト,
          'buy_candidates':  購入推奨銘柄リスト,
          'as_of':           分析日時,
        }
    """
    # 通貨目標: 呼び出し側が AI 動的方針を注入できる。None なら static (現行挙動)。
    # AI 方針は basis=long_tier のみ適用され、無効/期限切れは呼び出し側が static に
    # 解決済みで渡す (currency_policy.resolve_effective_targets)。
    currency_targets = currency_targets if currency_targets is not None else CURRENCY_TARGETS

    total      = snapshot.get('total_jpy', 0)
    # H1: 通貨/セクター判定は core (long) tier に限定する。geo/NISA保護は元のsnapshot(全体)を使う。
    core_snap  = build_core_snapshot(snapshot)
    core_total = core_snap.get('total_jpy', 0)
    cur   = analyze_currency_balance(core_snap, targets=currency_targets)
    sec   = analyze_sector_balance(core_snap)

    # 全アクションを統合して優先度でソート
    all_actions = cur['actions'] + sec['actions']
    all_actions.sort(key=lambda x: x['priority'])

    # NISA口座ポジションへの売り推奨に保護注記を付与（全tier対象なので元のsnapshotを使う）
    all_actions = check_nisa_sell_protection(snapshot, all_actions)

    # 購入推奨セクター（不足・目標以下のセクター）。gap_jpy は core(long)総額基準。
    buy_sectors = []
    for sector, info in sec['sectors'].items():
        if info['ratio'] < info['target']:
            gap_jpy = (info['target'] - info['ratio']) * core_total
            if gap_jpy > 50_000:
                buy_sectors.append({
                    'sector':   sector,
                    'gap_jpy':  round(gap_jpy, 0),
                    'current':  f'{info["ratio"]*100:.1f}%',
                    'target':   f'{info["target"]*100:.1f}%',
                })
    buy_sectors.sort(key=lambda x: -x['gap_jpy'])

    # 購入推奨通貨（不足通貨）。gap_jpy は core(long)総額基準。
    buy_currencies = []
    for ccy, info in cur['currencies'].items():
        if info['ratio'] < info['target_min']:
            gap_jpy = (currency_targets[ccy]['ideal'] - info['ratio']) * core_total
            buy_currencies.append({
                'currency': ccy,
                'gap_jpy':  round(gap_jpy, 0),
                'current':  f'{info["ratio"]*100:.1f}%',
                'target':   f'{currency_targets[ccy]["min"]*100:.0f}〜{currency_targets[ccy]["max"]*100:.0f}%',
            })

    # 新規資金の振り分け提案
    new_cash_plan = _plan_new_cash_allocation(
        buy_sectors, buy_currencies, available_cash, monthly_budget
    )

    # 欧州集中リスク分析
    geo = analyze_geographic_concentration(snapshot)
    if geo['status'] == 'warning' and geo['action']:
        all_actions.insert(0, {
            'priority': REBALANCE_PRIORITY['avoid_overweight_buy'],
            'level':    'warning',
            'type':     'reduce',
            'geo':      'europe',
            'message':  geo['action'],
            'amount_jpy': round((geo['european_ratio'] - EUROPEAN_THRESHOLD) * total),
        })

    # 細切れリバランス抑制: 総資産0.5%未満の単発推奨は繰越扱いに分離（全ソース統合後に適用）
    all_actions, deferred_actions = _filter_micro_actions(all_actions, total)

    # テック集中度
    tech = sec['sectors'].get('Technology', {})
    tech_ratio = tech.get('ratio', 0)

    # 四半期リバランス判定
    today          = date.today()
    quarter_months = {1, 4, 7, 10}
    is_quarter     = today.month in quarter_months and today.day <= 15

    overall_status = 'action_needed' if (
        cur['status'] == 'action_needed' or sec['status'] == 'action_needed'
    ) else ('warning' if (cur['status'] == 'warning' or sec['status'] == 'warning') else 'ok')

    return {
        'summary': {
            'overall_status':        overall_status,
            'currency_status':       cur['status'],
            'sector_status':         sec['status'],
            'geographic_status':     geo['status'],
            'tech_ratio':            round(tech_ratio, 4),
            'tech_concentration':    tech_ratio > SECTOR_TRIGGER,
            'european_ratio':        geo['european_ratio'],
            'european_concentration': geo['status'] == 'warning',
            'quarterly_check_due':   is_quarter,
            'available_cash':        available_cash,
            'monthly_budget':        monthly_budget,
            'total_jpy':             total,
            'core_total_jpy':        core_total,
            'core_position_count':   len(core_snap.get('positions', [])),
        },
        'currency_result':    cur,
        'sector_result':      sec,
        'geographic_result':  geo,
        'action_plan':        all_actions,
        'deferred_actions':   deferred_actions,
        'buy_candidates': {
            'sectors':    buy_sectors,
            'currencies': buy_currencies,
        },
        'new_cash_plan': new_cash_plan,
        'as_of':         datetime.now().strftime('%Y-%m-%d %H:%M'),
    }


def _plan_new_cash_allocation(
    buy_sectors:    list,
    buy_currencies: list,
    available_cash: float,
    monthly_budget: float,
) -> list:
    """
    新規資金（クレカ積立売却資金・給与）の振り分け提案を生成する。
    """
    plan = []
    budget = available_cash + monthly_budget

    if not buy_sectors and not buy_currencies:
        plan.append({
            'action': '現状維持',
            'detail': 'すべての配分が目標レンジ内です。引き続き現在の購入方針を継続してください。',
        })
        return plan

    # 通貨不足を最優先
    for bc in buy_currencies:
        amt = min(bc['gap_jpy'], budget * 0.5)
        plan.append({
            'action': f'{bc["currency"]}資産を追加購入',
            'detail': f'現在{bc["current"]}→目標{bc["target"]}。¥{amt/10000:.0f}万分を{bc["currency"]}建て資産へ投資。',
            'amount': round(amt, 0),
        })

    # セクター不足を次に対処
    for bs in buy_sectors[:3]:   # 上位3セクターのみ
        plan.append({
            'action': f'{bs["sector"]}セクターを補充',
            'detail': f'現在{bs["current"]}→目標{bs["target"]}。次の新規投資で{bs["sector"]}銘柄を優先。',
            'gap_jpy': bs['gap_jpy'],
        })

    return plan


# ============================================================
# Medium 層ドリフト分析（個別ポジション目標ウェイト）
# ============================================================

#: Medium 層各ポジションの相対選好ウェイト（Medium 層内での割合）。
# basis: Medium 層内比率。calculate_medium_drift 内で保有銘柄間に正規化され
# target 合計は常に 100% になる（相対 basis）。
MEDIUM_TARGET_WEIGHTS: dict[str, float] = {
    "META":    0.20,   # メタ (特定+一般口座合算)
    "6762.T":  0.08,   # TDK — 日本テクノロジー
}

#: Medium 層 hard cap（層内比率の上限）。Codex round4 設計判断:
# 相対選好だけだと縮退時 (META+6762.T のみ等) に META が 71.4% になり
# 「META 20% 上限」というユーザー意図と乖離する。cap は正規化後に適用し、
# 超過分を未 cap 銘柄へ water-fill 再配分する。再配分先が不足する縮退局面では
# 警告のみで degraded_target_model=True を立て、強制リバランスしない。
# Codex round7 B: cap は集中リスクの高い銘柄のみに限定する。6762.T(TDK)の
# 集中は個別 hard cap ではなく sector/portfolio-level の集中度・通常 drift で見る。
MEDIUM_MAX_WEIGHTS: dict[str, float] = {
    "META":   0.20,
}

#: ドリフトアラート閾値（目標比との乖離）
MEDIUM_DRIFT_TRIGGER = 0.10   # ±10%で要確認


def _is_nisa_account(account) -> bool:
    """NISA 口座 (成長投資枠 / つみたて投資枠) かを判定する。"""
    return "NISA" in str(account or "")


def _apply_max_weight_caps(raw_targets: dict[int, float],
                           idx_to_ticker: dict[int, str]) -> tuple[dict[int, float], bool]:
    """
    正規化済み target に hard cap を water-fill 適用する。

    Codex round5 #1: cap は **ticker 合算** に適用する。同一 ticker を複数口座で
    保有していても、合算 target が cap を超えないようにする (META_特定 + META_一般 が
    各 0.20 で合計 0.40 になる per-position バグを防ぐ)。cap 後は各口座行へ元の
    value 比 (= 元 raw_targets の ticker 内比率) で配分し直す。

    超過分は未 cap (cap 余地のある) ticker へ現 weight 比で再配分。再配分先が
    無ければ degraded=True を返す（呼出側で警告のみ・実行抑制）。
    Returns (capped_targets_per_idx, degraded)。
    """
    if not MEDIUM_MAX_WEIGHTS:
        return raw_targets, False

    # 1) ticker 合算 target と、ticker 内の idx→share を計算
    ticker_target: dict[str, float] = {}
    ticker_idxs: dict[str, list] = {}
    for idx, w in raw_targets.items():
        t = idx_to_ticker.get(idx, "")
        ticker_target[t] = ticker_target.get(t, 0.0) + w
        ticker_idxs.setdefault(t, []).append(idx)

    # 2) ticker レベルで water-fill cap
    tt = dict(ticker_target)
    degraded = False
    for _ in range(10):  # water-fill 反復上限 (ticker 数有限なので必ず収束)
        excess = 0.0
        capped = set()
        for t, w in tt.items():
            cap = MEDIUM_MAX_WEIGHTS.get(t)
            if cap is not None and w > cap + 1e-9:
                excess += (w - cap)
                tt[t] = cap
                capped.add(t)
        if excess <= 1e-9:
            break
        room = {}
        for t, w in tt.items():
            if t in capped:
                continue
            cap = MEDIUM_MAX_WEIGHTS.get(t)
            headroom = (cap - w) if cap is not None else float("inf")
            if headroom > 1e-9:
                room[t] = w if w > 0 else 1e-6
        if not room:
            degraded = True
            break
        room_sum = sum(room.values())
        for t, w in room.items():
            tt[t] = tt[t] + excess * (w / room_sum)

    # 3) cap 後の ticker target を、元の ticker 内 value 比で各 idx へ配分し直す
    out: dict[int, float] = {}
    for t, idxs in ticker_idxs.items():
        before = ticker_target.get(t, 0.0)
        after = tt.get(t, before)
        if before > 1e-12:
            for idx in idxs:
                out[idx] = raw_targets[idx] * (after / before)
        else:
            # 元 target が 0 の ticker は均等
            for idx in idxs:
                out[idx] = after / len(idxs)
    return out, degraded


def calculate_medium_drift(snapshot: dict) -> dict:
    """
    Medium 層各ポジションのウェイトドリフトを計算する。

    Args:
        snapshot: portfolio_manager.build_portfolio_snapshot() の出力

    Returns:
        {
          'status':     'ok' | 'warning',
          'total_jpy':  Medium 層合計（円）,
          'positions':  [{ticker, name, actual_pct, target_pct, drift, level}, ...],
          'actions':    [ドリフト是正アクション],
        }
    """
    medium_positions = [
        p for p in snapshot.get("positions", [])
        if p.get("investment_type") == "medium"
    ]
    if not medium_positions:
        return {"status": "ok", "total_jpy": 0, "positions": [], "actions": []}

    total_medium = sum(p.get("value_jpy", 0) for p in medium_positions) or 1

    # 目標ウェイト正規化 (basis: Medium 層内比率、総資産比ではない):
    #   1. 明示 target は ticker 単位で 1 回だけ計上する。同一 ticker を複数口座で
    #      保有していても二重カウントしない (META_特定 + META_一般 → META 20% を 1 回)。
    #   2. 明示 ticker の weight は、その ticker の各ポジションへ value 比で按分する。
    #   3. 明示されていない ticker には残余 (1 - 明示合計) を ticker 単位で均等配分する。
    #   4. 最後に全ポジションの raw target を合計で割って正規化し、合計を必ず 1.0 にする
    #      (全 ticker が明示で合計<1 のケースでも 100% に揃える)。
    def _explicit_weight(ticker: str, key: str):
        w = MEDIUM_TARGET_WEIGHTS.get(key)
        if w is None:
            w = MEDIUM_TARGET_WEIGHTS.get(ticker)
        return w

    # ticker 単位の集計
    ticker_value: dict[str, float] = {}
    ticker_explicit: dict[str, float] = {}
    for p in medium_positions:
        t = p.get("ticker", "")
        k = p.get("key", t)
        v = p.get("value_jpy", 0) or 0
        ticker_value[t] = ticker_value.get(t, 0.0) + v
        w = _explicit_weight(t, k)
        if w is not None:
            # 同一 ticker で複数の明示値が来た場合は最初の値を採用 (通常は同一)
            ticker_explicit.setdefault(t, w)

    # Codex re-review #1 (basis 統一): Medium 層内比率に統一し、target 合計を
    # 常に 100% に正規化する。
    #   理由: actual_pct (= value / total_medium) は invested ポジションのみで
    #   合計 100% になる。target を絶対値のまま (合計<100%) にすると分母不整合で
    #   drift が歪み、trim→sleeve 縮小→再 trim の反復縮小を起こす (round3 #1)。
    #   target も同じ invested 母数で 100% に正規化すれば drift 合計 = 0 となり、
    #   生成される trim/buy は cash-neutral な 1 回のリバランスになる (反復しない)。
    #   明示 weight は「相対的な配分選好」として扱い、保有銘柄間で正規化される。
    #   合計>100% の設定ミスも同じ正規化で吸収する。
    explicit_weight_sum = sum(ticker_explicit.values())
    unlisted_tickers = [t for t in ticker_value if t not in ticker_explicit]
    remainder = max(0.0, 1.0 - min(explicit_weight_sum, 1.0))
    fallback_per_ticker = (remainder / len(unlisted_tickers)) if unlisted_tickers else 0.0

    # 各ポジションの raw target (ticker weight を value 比で按分)
    raw_targets: dict[int, float] = {}
    for idx, p in enumerate(medium_positions):
        t = p.get("ticker", "")
        v = p.get("value_jpy", 0) or 0
        t_val = ticker_value.get(t, 0.0) or 1.0
        share = (v / t_val) if t_val > 0 else (1.0 / max(1, sum(1 for q in medium_positions if q.get("ticker") == t)))
        if t in ticker_explicit:
            raw_targets[idx] = ticker_explicit[t] * share
        else:
            raw_targets[idx] = fallback_per_ticker * share

    # 合計を必ず 1.0 に正規化 (drift 合計 = 0 を保証 → cash-neutral リバランス)。
    raw_sum = sum(raw_targets.values())
    if raw_sum > 0:
        for idx in raw_targets:
            raw_targets[idx] /= raw_sum

    # Codex round4 設計判断: hard cap を water-fill 適用 (round5 #1: ticker 合算)。
    idx_to_ticker = {idx: p.get("ticker", "") for idx, p in enumerate(medium_positions)}
    raw_targets, degraded_target_model = _apply_max_weight_caps(raw_targets, idx_to_ticker)

    # Codex round5 #3: cap 後 target 合計が 100% 未満になる場合 (degraded) は
    # その差を unallocated_target_pct として明示する。
    allocated_after_cap = sum(raw_targets.values())
    unallocated_target_pct = round(max(0.0, 1.0 - allocated_after_cap) * 100, 1)

    position_reports = []
    actions = []
    worst_level = "ok"

    for idx, p in enumerate(medium_positions):
        ticker = p.get("ticker", "")
        key    = p.get("key", ticker)
        name   = p.get("name", ticker)
        account = p.get("account")
        value  = p.get("value_jpy", 0)

        actual_pct = value / total_medium
        target_pct = raw_targets.get(idx, 0.0)
        drift      = actual_pct - target_pct

        # Codex round4 #1: 閾値判定後も plan を cash-neutral にするため、閾値未満でも
        # signed_amount を蓄積し、最後に net 残差を residual として明示する。
        signed_amount = round(-drift * total_medium)  # +買い / -売り

        if abs(drift) >= MEDIUM_DRIFT_TRIGGER:
            level = "warning"
            worst_level = "warning"
            direction = "過剰（削減を検討）" if drift > 0 else "不足（追加を検討）"
            diff_jpy = abs(drift) * total_medium
            atype = "reduce" if drift > 0 else "buy"

            # Codex round6 #3 (C): NISA 口座の reduce は hard suppress する。
            #   NISA は売却すると非課税枠が戻らないため、rebalance レイヤーで
            #   実行不可にする (特定/一般の税効率順序・損益通算は tax_lot/execution に委ねる)。
            nisa_protected = (atype == "reduce" and _is_nisa_account(account))

            _suppressed = bool(degraded_target_model) or nisa_protected
            if degraded_target_model:
                _reason = "degraded_target_model: cap 再配分先不足のため観測のみ"
            elif nisa_protected:
                _reason = f"NISA売却保護: {account} は非課税枠が戻らないため reduce 実行不可 (観測のみ)"
            else:
                _reason = None

            _act = {
                "priority": 2,
                "level":    "warning",
                "type":     atype,
                "ticker":   ticker,
                # Codex round4 #2: 同一 ticker 複数口座を区別できるよう key/account を保持。
                "key":      key,
                "account":  account,
                "message":  (
                    f"Medium/{ticker}"
                    + (f"[{account}]" if account else "")
                    + f": 実際{actual_pct*100:.1f}%→目標{target_pct*100:.1f}% "
                    f"（乖離{drift*100:+.1f}% / {direction} / ¥{diff_jpy/10000:.0f}万"
                    + ("・NISA売却保護" if nisa_protected else "")
                    + "）"
                ),
                "amount_jpy": round(diff_jpy),
                "signed_amount_jpy": signed_amount,
                # Codex round5 #2 / round6 #3: degraded または NISA reduce は
                # 実行不可・観測のみ。status だけでは prompt 注入時に deterministic な
                # 実行抑制にならないため、フラグで明示する。
                "executable":   not _suppressed,
                "observe_only": _suppressed,
                "nisa_protected": nisa_protected,
            }
            if _reason:
                _act["suppressed_reason"] = _reason
            actions.append(_act)
        else:
            level = "ok"

        position_reports.append({
            "ticker":     ticker,
            "name":       name,
            "key":        key,
            "account":    account,
            "actual_pct": round(actual_pct * 100, 1),
            "target_pct": round(target_pct * 100, 1),
            "drift_pct":  round(drift * 100, 1),
            "value_jpy":  round(value),
            "level":      level,
        })

    # Codex round7 #1: NISA-locked funding gap。
    #   reduce 側が NISA 保護等で実行不可だと、その売却代金は実際には出ない。
    #   それなのに sibling の buy だけ executable=True のままだと「NISA を売れない
    #   のに買いだけ実行可能」になり、外部資金を黙って要求する危険な plan になる。
    #   executable な buy 所要額が executable な reduce 調達額を上回る場合 (= 抑制された
    #   reduce に依存している場合)、その buy を external_cash_required=True /
    #   executable=False / observe_only=True に落とし、新規資金判断 (別承認) に回す。
    _exec_reduce_cash = sum(
        a.get("amount_jpy", 0) for a in actions
        if a.get("type") == "reduce" and a.get("executable")
    )
    _exec_buy_cash = sum(
        a.get("amount_jpy", 0) for a in actions
        if a.get("type") == "buy" and a.get("executable")
    )
    _has_suppressed_reduce = any(
        a.get("type") == "reduce" and not a.get("executable") for a in actions
    )
    _nisa_locked_reduce = any(
        a.get("type") == "reduce" and a.get("nisa_protected") for a in actions
    )
    underfunded_plan = False
    nisa_locked_drift = False
    if _exec_buy_cash > _exec_reduce_cash + 1 and _has_suppressed_reduce:
        underfunded_plan = True
        nisa_locked_drift = bool(_nisa_locked_reduce)
        _gap_reason = (
            "funding source (reduce) が NISA 保護等で実行不可: rebalance では自己資金"
            "調達できないため外部資金が必要 (別承認)"
        )
        for a in actions:
            if a.get("type") == "buy" and a.get("executable"):
                a["executable"] = False
                a["observe_only"] = True
                a["external_cash_required"] = True
                a.setdefault("suppressed_reason", _gap_reason)

    # Codex round4 #1 / round7 #1: residual は **executable な** action の net cash flow。
    # 抑制された NISA reduce を「資金源」として数えないようにする (負=現金不足/正=余剰)。
    residual_cash_jpy = -sum(
        a.get("signed_amount_jpy", 0) for a in actions if a.get("executable")
    )
    # 参考: target ドリフト全体 (executable 問わず) の残差も保持。
    plan_residual_cash_jpy = -sum(a.get("signed_amount_jpy", 0) for a in actions)

    # degraded / underfunded 時は強制リバランスせず警告に留める。
    if (degraded_target_model or underfunded_plan) and worst_level == "warning":
        worst_level = "degraded"

    return {
        "status":    worst_level,
        "total_jpy": round(total_medium),
        "positions": position_reports,
        "actions":   sorted(actions, key=lambda x: abs(x.get("amount_jpy", 0)), reverse=True),
        # target は Medium 層内比率。hard cap (ticker 合算) 適用後。
        # round5 #3: degraded で cap 後合計<100% のとき、その差を unallocated として明示。
        "unallocated_target_pct": unallocated_target_pct,
        "target_basis": "medium_tier_internal_normalized_100pct_capped",
        # round7 #1: residual は executable な action のみの net cash flow (負=不足/正=余剰)。
        "residual_cash_jpy": int(residual_cash_jpy),
        # 参考: executable 問わず target ドリフト全体の残差。
        "plan_residual_cash_jpy": int(plan_residual_cash_jpy),
        # Codex round4 設計: cap 再配分先不足の縮退局面。True なら actions は
        # executable=False / observe_only=True で実行抑制 (status=degraded)。
        "degraded_target_model": bool(degraded_target_model),
        # Codex round7 #1: 抑制 reduce に依存する buy が自己資金調達できない局面。
        # underfunded_plan=True なら buy は external_cash_required で別承認待ち。
        "underfunded_plan": bool(underfunded_plan),
        "nisa_locked_drift": bool(nisa_locked_drift),
        "note":      f"Medium層合計 ¥{total_medium/10000:.0f}万（{len(medium_positions)}ポジション / {len(ticker_value)}銘柄 / 閾値±{MEDIUM_DRIFT_TRIGGER*100:.0f}% / hard cap適用"
                     + (f"・target合計{round(allocated_after_cap*100,1)}%+未割当{unallocated_target_pct}%・縮退(degraded:観測のみ)"
                        if degraded_target_model else "・target合計100%")
                     + ("・NISA売却ロックで自己資金調達不可(外部資金要・別承認)" if nisa_locked_drift
                        else ("・自己資金調達不足(外部資金要)" if underfunded_plan else ""))
                     + f"・executable残差¥{int(residual_cash_jpy)/10000:.1f}万）",
    }


# ============================================================
# 損出し候補検出（tax_optimizer.pyとの連携用）
# ============================================================

def analyze_geographic_concentration(snapshot: dict) -> dict:
    """
    欧州集中リスク（EWG + IEV + EPOL）を検知する。

    NATO/地政学リスクによる相関崩壊が起きた場合、欧州ETFが同時に下落するため、
    合計15%超はポートフォリオリスクとして警告する。

    Returns:
        {
          'european_ratio': float,
          'european_value_jpy': int,
          'status': 'ok' / 'warning',
          'positions': [{ticker, name, ratio, value_jpy}, ...],
          'action': str or None,
        }
    """
    total = snapshot.get('total_jpy', 0)
    if not total:
        return {'european_ratio': 0, 'status': 'ok', 'positions': [], 'action': None}

    eu_positions = []
    eu_total_jpy = 0
    for p in snapshot.get('positions', []):
        ticker = p.get('ticker', '')
        key    = p.get('key', '')
        if ticker in EUROPEAN_TICKERS or key in EUROPEAN_TICKERS:
            val = p.get('value_jpy', 0) or 0
            eu_total_jpy += val
            eu_positions.append({
                'ticker':    ticker,
                'name':      p.get('name', ticker),
                'ratio':     round(val / total, 4) if total else 0,
                'value_jpy': round(val),
            })

    eu_ratio = eu_total_jpy / total if total else 0

    if eu_ratio > EUROPEAN_THRESHOLD:
        status = 'warning'
        action = (
            f'欧州ETF（EWG+IEV+EPOL）が{eu_ratio*100:.1f}%（閾値{EUROPEAN_THRESHOLD*100:.0f}%）。'
            'NATO拡大/ロシア・ウクライナ情勢の相関崩壊リスクあり。'
            f'新規欧州購入を停止し、¥{(eu_ratio - EUROPEAN_THRESHOLD) * total / 10000:.0f}万分を他地域へ分散推奨。'
        )
    else:
        status = 'ok'
        action = None

    return {
        'european_ratio':     round(eu_ratio, 4),
        'european_value_jpy': round(eu_total_jpy),
        'threshold':          EUROPEAN_THRESHOLD,
        'status':             status,
        'positions':          eu_positions,
        'action':             action,
    }


def check_nisa_sell_protection(snapshot: dict, action_plan: list) -> list:
    """
    アクションプランの売り推奨からNISA口座ポジションを保護する。

    NISA口座で保有するポジションを売るとNISA枠が消耗するため、
    リバランス目的の売りアクションにNISA保護の注記を追加する。

    Returns:
        action_plan に 'nisa_protected' フラグと注記を付与したリスト
    """
    # NISA口座のticker集合を構築
    nisa_tickers: set[str] = set()
    for p in snapshot.get('positions', []):
        acct = p.get('account', '')
        if any(kw in acct for kw in NISA_ACCOUNT_KEYWORDS):
            nisa_tickers.add(p.get('ticker', ''))
            nisa_tickers.add(p.get('key', ''))

    protected_plan = []
    for action in action_plan:
        a = dict(action)
        # 売り/削減アクションかつNISAポジションが含まれる可能性がある場合に注記
        if a.get('type') in ('reduce', 'sell') and nisa_tickers:
            a['nisa_warning'] = (
                f'⚠️ NISA口座ポジション（{", ".join(sorted(nisa_tickers))}）は'
                '売却するとNISA枠が消耗します。売り対象は特定・一般口座に限定してください。'
            )
        protected_plan.append(a)
    return protected_plan


def find_loss_harvest_candidates(snapshot: dict, min_loss_jpy: float = 50_000) -> list:
    """
    損出し候補銘柄を検出する。

    Args:
        snapshot:      portfolio_manager スナップショット
        min_loss_jpy:  最低含み損額（円）

    Returns:
        [{ticker, name, unrealized_jpy, unrealized_pct, investment_type, account}, ...]
    """
    candidates = []
    for p in snapshot.get('positions', []):
        if p['unrealized_jpy'] < -min_loss_jpy:
            candidates.append({
                'key':             p['key'],
                'ticker':          p['ticker'],
                'name':            p['name'],
                'unrealized_jpy':  p['unrealized_jpy'],
                'unrealized_pct':  p['unrealized_pct'],
                'investment_type': p['investment_type'],
                'account':         p['account'],
                'sector':          p['sector'],
            })
    candidates.sort(key=lambda x: x['unrealized_jpy'])   # 損失が大きい順
    return candidates


# ============================================================
# スナップショット保存
# ============================================================

def save_rebalance_report(report: dict):
    """リバランスレポートをJSONにアトミックに保存する。"""
    atomic_write_json(BASE_DIR / 'rebalance_report.json', report)


# ============================================================
# CLI
# ============================================================

def _print_report(report: dict):
    s = report['summary']
    status_icon = {'ok': '✅', 'warning': '⚠️', 'action_needed': '🔴'}.get(s['overall_status'], '')

    print(f'\n=== リバランス分析 {report["as_of"]} ===')
    print(f'総合ステータス: {status_icon} {s["overall_status"]}')
    print(f'総資産:         ¥{s["total_jpy"]/10000:.0f}万')
    print(f'テック比率:     {s["tech_ratio"]*100:.1f}%{"  ⚠️ 集中" if s["tech_concentration"] else ""}')
    print(f'四半期チェック: {"⏰ 実施推奨" if s["quarterly_check_due"] else "次回まで待機"}')

    # 通貨配分
    print('\n【通貨配分】')
    for ccy, info in report['currency_result']['currencies'].items():
        icon = '✅' if info['level'] == 'ok' else '⚠️'
        print(f'  {icon} {ccy}: {info["ratio"]*100:.1f}% '
              f'（目標 {info["target_min"]*100:.0f}〜{info["target_max"]*100:.0f}%）')

    # セクター配分（上位5）
    print('\n【セクター配分（上位5）】')
    sectors = sorted(report['sector_result']['sectors'].items(),
                     key=lambda x: -x[1]['ratio'])[:5]
    for sector, info in sectors:
        icon = {'ok': '✅', 'warning': '⚠️', 'action_needed': '🔴'}.get(info['level'], '')
        print(f'  {icon} {sector}: {info["ratio"]*100:.1f}% （目標 {info["target"]*100:.0f}%）')

    # アクションプラン
    if report['action_plan']:
        print('\n【アクションプラン】')
        for i, action in enumerate(report['action_plan'], 1):
            icon = {'critical': '🔴', 'warning': '⚠️', 'info': 'ℹ️'}.get(action.get('level', 'info'), 'ℹ️')
            print(f'  {i}. {icon} {action["message"]}')
    else:
        print('\n  アクション不要（すべて目標範囲内）')

    # 新規資金プラン
    print('\n【新規資金の振り分け提案】')
    for p in report['new_cash_plan']:
        print(f'  → {p["action"]}: {p["detail"]}')


if __name__ == '__main__':
    import sys
    try:
        import portfolio_manager
    except ImportError:
        print('portfolio_manager.py が必要です')
        sys.exit(1)

    args = sys.argv[1:]
    cash = float(args[0]) if args else 0

    print('ポートフォリオスナップショットを取得中...')
    snapshot = portfolio_manager.build_portfolio_snapshot()
    report   = calculate_rebalance_actions(snapshot, available_cash=cash)
    _print_report(report)
    save_rebalance_report(report)
    print(f'\nレポート保存: rebalance_report.json')

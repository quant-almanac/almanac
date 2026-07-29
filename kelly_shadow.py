"""kelly_shadow.py — Stage 6B: half-Kelly の反実仮想 (counterfactual) 影実行

現状の実行フロー: raw priority_actions → apply_policy_gate → accepted
(analyst/__init__.py で実際に使われる経路)。

本モジュールは同じ入力から**別の**経路をもう一本だけ計算する:
  counterfactual: deep-copy(raw) → Kelly cap → apply_policy_gate (同じルール)
                  → post-filter → 最終チェック

位置は policy_engine の前 — Kelly はサイズを提案するだけで、VaR/DD/leverage
等の既存ルールを迂回しない。影経路は観測用であり、以下を一切行わない:
  - action_state_tracker への登録
  - ai_recommendation_log.json への追記
  - execution_plan_state.json の更新
  - 実際の synthesis["priority_actions"] の変更
  - 通知 (Telegram 等)

Kelly は目標ポジションの絶対上限（追加可能額 = max(0, Kelly目標額 − 現在保有額)）。
policy_engine の policy_size_adj（リスク状態の縮小倍率）とは別の量であり、混ぜない —
Kelly cap は「このティッカーにこれ以上足すな」という上限、policy_size_adj は
「今のリスク状態なら提案サイズ全体をこの倍率に縮めろ」という別の縮小要因。

影経路には実経路と同一の凍結 context (呼び出し側が構築済みの PolicyContext・
portfolio_total・保有額) を渡す — 実経路の後に再ロードしない。

最終処理: 丸め後の追加可能額が1単位未満なら actionable=False として記録するだけで、
assert は使わない（最小単位が上限を超えるのは想定内の業務状態であり、システムの
不変条件違反ではない — action を除外して理由を記録する）。
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Callable, Optional

from kelly_sizing import suggest_size_pct
from position_identity import position_identity_for_action

BUY_SIDE_TYPES = frozenset({'buy', 'add', 'dca'})


@dataclass(frozen=True)
class KellyCapResult:
    ticker: str
    actionable: bool
    kelly_target_jpy: float
    current_holding_jpy: float
    addable_jpy: float
    original_notional_jpy: Optional[float]
    capped_notional_jpy: Optional[float]
    was_capped: bool
    reason: Optional[str]


def compute_kelly_cap(
    *,
    ticker: str,
    investment_type: str,
    portfolio_total_jpy: float,
    current_holding_jpy: float,
    kelly_stats: Optional[dict] = None,
) -> KellyCapResult:
    """このティッカーへ追加してよい絶対上限 (JPY) を計算する。

    追加可能額 = max(0, Kelly目標額 − 現在保有額)。Kelly目標額は
    kelly_sizing.suggest_size_pct() が返す size_pct × portfolio_total。
    entry_allowed=False (履歴不足・EV負) の場合は addable_jpy=0
    (assert ではなく明示的な 0 上限として表現)。
    """
    sizing = suggest_size_pct(ticker, investment_type, stats=kelly_stats)
    if not sizing.get('entry_allowed'):
        return KellyCapResult(
            ticker=ticker, actionable=False,
            kelly_target_jpy=0.0, current_holding_jpy=current_holding_jpy,
            addable_jpy=0.0, original_notional_jpy=None, capped_notional_jpy=None,
            was_capped=False,
            reason=f"Kelly entry_allowed=False ({sizing.get('reason', '')})",
        )
    kelly_target_jpy = sizing['size_pct'] * portfolio_total_jpy
    addable_jpy = max(0.0, kelly_target_jpy - current_holding_jpy)
    return KellyCapResult(
        ticker=ticker, actionable=addable_jpy > 0,
        kelly_target_jpy=round(kelly_target_jpy, 0),
        current_holding_jpy=round(current_holding_jpy, 0),
        addable_jpy=round(addable_jpy, 0),
        original_notional_jpy=None, capped_notional_jpy=None, was_capped=False,
        reason=None if addable_jpy > 0 else "現在保有額が既に Kelly 目標額に到達済み",
    )


def _action_notional_jpy(action: dict) -> Optional[float]:
    """action から想定元本 (JPY) を推定する。amount_hint 等の自由形式フィールドしか
    無い action も多いため、数値化できない場合は None (推定不能) を返す。"""
    for key in ('notional_jpy', 'estimated_notional_jpy', 'amount_jpy'):
        v = action.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _replace_action_notional_jpy(action: dict, amount: float) -> None:
    """Make the counterfactual action itself reflect the capped notional.

    Keeping the old action fields and writing only ``kelly_shadow`` metadata
    produced a false counterfactual: the downstream policy still evaluated the
    original amount.  Preserve the original field for audit, then update the
    structured field that supplied the notional and always expose the canonical
    ``estimated_notional_jpy`` value.
    """
    for key in ('notional_jpy', 'estimated_notional_jpy', 'amount_jpy'):
        value = action.get(key)
        if isinstance(value, (int, float)) and value > 0:
            action[f'kelly_original_{key}'] = value
            action[key] = amount
            break
    action['estimated_notional_jpy'] = amount


def apply_kelly_cap_to_action(
    action: dict,
    *,
    portfolio_total_jpy: float,
    current_holdings_by_ticker: dict,
    current_holdings_by_position: Optional[dict] = None,
    require_position_identity: bool = False,
    kelly_stats: Optional[dict] = None,
    lot_unit_jpy: float = 1.0,
) -> dict:
    """1つの action に Kelly cap を適用した**新しい** dict を返す (元の action は
    変更しない)。buy/add/dca 以外の action はそのまま (cap 対象外) で返す。

    最終処理: cap 後の元本を lot_unit_jpy で切り下げ、1単位未満になったら
    actionable=False を action に付与するだけで例外は投げない。
    """
    new_action = copy.deepcopy(action)
    atype = str(action.get('type') or '').lower()
    ticker = action.get('ticker') or ''
    if atype not in BUY_SIDE_TYPES or not ticker:
        return new_action

    itype = str(action.get('tier') or action.get('investment_type') or 'medium').lower()
    holding_basis = "ticker_legacy"
    if current_holdings_by_position is not None:
        identity = position_identity_for_action(action)
        if identity is None or identity.key not in current_holdings_by_position:
            if require_position_identity:
                new_action['kelly_shadow'] = {
                    'capped': False,
                    'cap_skipped_reason': (
                        'position_identity_unresolved'
                        if identity is None
                        else 'position_holding_unknown'
                    ),
                    'current_holding_basis': 'unknown',
                }
                return new_action
        if identity is not None and identity.key in current_holdings_by_position:
            current_holding = float(current_holdings_by_position[identity.key] or 0.0)
            holding_basis = "position_identity"
        else:
            current_holding = float(current_holdings_by_ticker.get(ticker, 0.0) or 0.0)
    else:
        current_holding = float(current_holdings_by_ticker.get(ticker, 0.0) or 0.0)
    cap = compute_kelly_cap(
        ticker=ticker, investment_type=itype,
        portfolio_total_jpy=portfolio_total_jpy, current_holding_jpy=current_holding,
        kelly_stats=kelly_stats,
    )
    new_action['kelly_shadow'] = {
        'kelly_target_jpy': cap.kelly_target_jpy,
        'current_holding_jpy': cap.current_holding_jpy,
        'current_holding_basis': holding_basis,
        'addable_jpy': cap.addable_jpy,
        'reason': cap.reason,
    }

    original_notional = _action_notional_jpy(action)
    if original_notional is None:
        # 元本を推定できない action は cap のしようがない。Kelly 情報だけ付与し
        # actionable 判定はそのまま (影経路として観測不能であることを明示)。
        new_action['kelly_shadow']['capped'] = False
        new_action['kelly_shadow']['cap_skipped_reason'] = 'original_notional_unresolvable'
        return new_action

    if original_notional <= cap.addable_jpy:
        new_action['kelly_shadow']['capped'] = False
        return new_action

    # cap 適用 → lot 単位へ切り下げ
    capped_notional = math.floor(cap.addable_jpy / lot_unit_jpy + 1e-9) * lot_unit_jpy if lot_unit_jpy > 0 else cap.addable_jpy
    new_action['kelly_shadow']['capped'] = True
    new_action['kelly_shadow']['original_notional_jpy'] = original_notional
    new_action['kelly_shadow']['capped_notional_jpy'] = capped_notional
    _replace_action_notional_jpy(new_action, capped_notional)

    if capped_notional < lot_unit_jpy:
        # 想定内の業務状態 (assert しない) — action を除外して理由を記録する
        new_action['actionable'] = False
        new_action['kelly_shadow']['non_actionable_reason'] = (
            f"Kelly cap 後の元本 ¥{capped_notional:,.0f} が最小取引単位 ¥{lot_unit_jpy:,.0f} 未満"
        )

    return new_action


@dataclass(frozen=True)
class KellyShadowDecision:
    evaluated: tuple
    accepted: tuple
    rejected: tuple
    modified: tuple
    capped_count: int
    non_actionable_count: int
    post_filter_applied: bool
    post_filter_filtered_count: int
    post_filter_review_count: int


def run_kelly_shadow(
    actions: list,
    policy_ctx,
    *,
    portfolio_total_jpy: float,
    current_holdings_by_ticker: dict,
    current_holdings_by_position: Optional[dict] = None,
    require_position_identity: bool = False,
    kelly_stats: Optional[dict] = None,
    lot_unit_jpy: float = 1.0,
    post_filter: Optional[Callable[[list[dict]], dict]] = None,
) -> KellyShadowDecision:
    """反実仮想の影経路本体。

    副作用禁止 (Stage 6B の合格条件そのもの): action_state 登録・
    recommendation log 追加・execution plan 更新・実 action 変更・通知の
    いずれも行わない。呼び出し側は戻り値を synthesis の別フィールド
    (例: synthesis["kelly_shadow_decision"]) へ observation 専用として
    格納すること — priority_actions を置き換えてはならない。

    policy_ctx は実経路で既に構築済みの同一 PolicyContext を再利用する
    (呼び出し側が再ロードしないこと — 本関数自体は新しいデータ取得を行わない)。
    """
    from policy_engine import apply_policy_gate

    capped_actions = []
    evaluated_actions = []
    capped_count = 0
    non_actionable_count = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        new_action = apply_kelly_cap_to_action(
            action,
            portfolio_total_jpy=portfolio_total_jpy,
            current_holdings_by_ticker=current_holdings_by_ticker,
            current_holdings_by_position=current_holdings_by_position,
            require_position_identity=require_position_identity,
            kelly_stats=kelly_stats,
            lot_unit_jpy=lot_unit_jpy,
        )
        if new_action.get('kelly_shadow', {}).get('capped'):
            capped_count += 1
        evaluated_actions.append(new_action)
        if new_action.get('actionable') is False:
            non_actionable_count += 1
            continue  # policy_gate に渡さない (既に non_actionable)
        capped_actions.append(new_action)

    # 実経路と同じルールを、Kelly cap 済みのコピーに対して再適用する。
    # apply_policy_gate は pure function (副作用なし) なので、ここで
    # 何度呼んでも実経路には一切影響しない。
    decision = apply_policy_gate(capped_actions, policy_ctx)
    accepted = list(decision.accepted)
    rejected = list(decision.rejected)
    post_filter_applied = post_filter is not None
    post_filter_filtered_count = 0
    post_filter_review_count = 0

    if post_filter is not None:
        filtered_synthesis = post_filter(
            [copy.deepcopy(action) for action in accepted if isinstance(action, dict)]
        )
        if not isinstance(filtered_synthesis, dict):
            raise TypeError("Kelly shadow post_filter must return a synthesis dict")
        accepted = [
            action
            for action in (filtered_synthesis.get("priority_actions") or [])
            if isinstance(action, dict)
        ]
        post_filtered = [
            action
            for action in (
                list(filtered_synthesis.get("_filtered_actions") or [])
                + list(filtered_synthesis.get("order_intent_deferred_actions") or [])
            )
            if isinstance(action, dict)
        ]
        post_filter_filtered_count = len(post_filtered)
        post_filter_review_count = sum(
            1
            for action in accepted
            if str(action.get("execution_readiness") or "") != "ready"
        )
        rejected.extend(post_filtered)

    # Final invariant: a downstream minimum-notional/rounding rule must never
    # enlarge a counterfactual buy beyond Kelly's absolute addable amount.
    # Treat this as a normal non-actionable business state, not an assertion.
    final_accepted: list[dict] = []
    for action in accepted:
        shadow = action.get("kelly_shadow")
        if not isinstance(shadow, dict):
            final_accepted.append(action)
            continue
        addable = shadow.get("addable_jpy")
        notional = _action_notional_jpy(action)
        if (
            isinstance(addable, (int, float))
            and notional is not None
            and notional > float(addable) + 1e-9
        ):
            rejected_action = copy.deepcopy(action)
            rejected_action["actionable"] = False
            rejected_action["filtered_reason"] = "kelly_cap_post_filter_violation"
            rejected_action["kelly_shadow"]["non_actionable_reason"] = (
                f"post-filter後の元本 ¥{notional:,.0f} が "
                f"Kelly追加可能額 ¥{float(addable):,.0f} を超過"
            )
            rejected.append(rejected_action)
            non_actionable_count += 1
            continue
        final_accepted.append(action)

    return KellyShadowDecision(
        evaluated=tuple(evaluated_actions),
        accepted=tuple(final_accepted),
        rejected=tuple(rejected),
        modified=tuple(decision.modified),
        capped_count=capped_count,
        non_actionable_count=non_actionable_count,
        post_filter_applied=post_filter_applied,
        post_filter_filtered_count=post_filter_filtered_count,
        post_filter_review_count=post_filter_review_count,
    )

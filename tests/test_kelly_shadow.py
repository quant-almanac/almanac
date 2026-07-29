"""Stage 6B: Kelly 反実仮想の影実行 (kelly_shadow.py)。

背景: Kelly cap をどこに挿入するかが曖昧だった。本モジュールは
policy_engine の前に Kelly cap を適用した反実仮想 (counterfactual) 経路を
実経路とは完全に分離して計算する。副作用 (action_state 登録・
recommendation log 追加・execution plan 更新・実 action 変更・通知) を
一切行わないことが Stage 6B の合格条件そのものであり、本テストの中心。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import kelly_shadow as ks  # noqa: E402
from policy_engine import PolicyContext  # noqa: E402


# ---------------------------------------------------------------------------
# compute_kelly_cap
# ---------------------------------------------------------------------------


def test_kelly_cap_addable_is_target_minus_current_holding():
    cap = ks.compute_kelly_cap(
        ticker='AVGO', investment_type='long',
        portfolio_total_jpy=10_000_000, current_holding_jpy=100_000,
        kelly_stats={'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
    )
    # sizing = half-Kelly capped at long=5% → kelly_target = 0.05 * 10,000,000 = 500,000
    assert cap.kelly_target_jpy == pytest.approx(500_000, rel=0.01)
    assert cap.addable_jpy == pytest.approx(cap.kelly_target_jpy - 100_000, rel=0.01)
    assert cap.actionable is True


def test_kelly_cap_zero_when_already_at_or_above_target():
    cap = ks.compute_kelly_cap(
        ticker='AVGO', investment_type='long',
        portfolio_total_jpy=10_000_000, current_holding_jpy=999_000_000,  # 既に大きく超過
        kelly_stats={'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
    )
    assert cap.addable_jpy == 0.0
    assert cap.actionable is False


def test_kelly_cap_zero_when_entry_not_allowed():
    """履歴不足・EV負など entry_allowed=False の場合は assert せず
    addable_jpy=0 として明示的に表現する。"""
    cap = ks.compute_kelly_cap(
        ticker='NEW', investment_type='swing',
        portfolio_total_jpy=10_000_000, current_holding_jpy=0,
        kelly_stats={},  # 統計無し → fallback → entry_allowed=False
    )
    assert cap.addable_jpy == 0.0
    assert cap.actionable is False
    assert cap.reason is not None


# ---------------------------------------------------------------------------
# apply_kelly_cap_to_action
# ---------------------------------------------------------------------------


def test_non_buy_action_passes_through_unmodified():
    action = {'ticker': 'AVGO', 'type': 'trim', 'notional_jpy': 500_000}
    result = ks.apply_kelly_cap_to_action(
        action, portfolio_total_jpy=10_000_000, current_holdings_by_ticker={},
    )
    assert result == action
    assert 'kelly_shadow' not in result


def test_original_action_is_never_mutated():
    """本題: apply_kelly_cap_to_action は新しい dict を返し、元の action を書き換えない。"""
    action = {'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 5_000_000}
    original_copy = dict(action)
    ks.apply_kelly_cap_to_action(
        action, portfolio_total_jpy=10_000_000, current_holdings_by_ticker={},
        kelly_stats={'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
    )
    assert action == original_copy  # 呼び出し前後で不変


def test_buy_under_kelly_cap_is_not_capped():
    action = {'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 50_000}
    result = ks.apply_kelly_cap_to_action(
        action, portfolio_total_jpy=10_000_000, current_holdings_by_ticker={'AVGO': 0},
        kelly_stats={'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
    )
    assert result['kelly_shadow']['capped'] is False
    assert result.get('actionable') is not False


def test_buy_over_kelly_cap_is_capped_and_lot_rounded():
    action = {'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 5_000_000}  # 目標(50万)を大幅超過
    result = ks.apply_kelly_cap_to_action(
        action, portfolio_total_jpy=10_000_000, current_holdings_by_ticker={'AVGO': 0},
        kelly_stats={'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
        lot_unit_jpy=1000.0,
    )
    assert result['kelly_shadow']['capped'] is True
    assert result['kelly_shadow']['capped_notional_jpy'] < action['notional_jpy']
    assert result['kelly_shadow']['capped_notional_jpy'] % 1000.0 == 0
    assert result['notional_jpy'] == result['kelly_shadow']['capped_notional_jpy']
    assert result['estimated_notional_jpy'] == result['notional_jpy']
    assert result['kelly_original_notional_jpy'] == action['notional_jpy']


def test_position_identity_holding_does_not_mix_same_ticker_across_accounts():
    stats = {
        'AVGO': {
            'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03,
            'n': 10, 'sufficient': True,
        },
    }
    husband = {
        'ticker': 'AVGO', 'type': 'add', 'tier': 'long',
        'notional_jpy': 400_000,
        'execution_owner': 'husband', 'execution_broker': 'rakuten',
        'execution_account': 'general',
    }
    result = ks.apply_kelly_cap_to_action(
        husband,
        portfolio_total_jpy=10_000_000,
        current_holdings_by_ticker={'AVGO': 9_000_000},
        current_holdings_by_position={
            'husband|rakuten|general|AVGO': 100_000,
            'wife|rakuten|general|AVGO': 8_900_000,
        },
        require_position_identity=True,
        kelly_stats=stats,
    )
    assert result['kelly_shadow']['current_holding_jpy'] == 100_000
    assert result['kelly_shadow']['current_holding_basis'] == 'position_identity'
    assert result['kelly_shadow']['capped'] is False


def test_required_position_identity_never_assumes_missing_holding_is_zero():
    action = {'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 400_000}
    result = ks.apply_kelly_cap_to_action(
        action,
        portfolio_total_jpy=10_000_000,
        current_holdings_by_ticker={'AVGO': 0},
        current_holdings_by_position={},
        require_position_identity=True,
    )
    assert result['kelly_shadow']['capped'] is False
    assert result['kelly_shadow']['cap_skipped_reason'] == 'position_identity_unresolved'
    assert result['kelly_shadow']['current_holding_basis'] == 'unknown'


def test_capped_below_one_lot_marks_non_actionable_without_raising():
    """本題: 最小単位を上限が下回るのは想定内の業務状態。assert しない。"""
    action = {'ticker': 'TINY', 'type': 'buy', 'notional_jpy': 10_000_000}
    result = ks.apply_kelly_cap_to_action(
        action, portfolio_total_jpy=1000.0,  # 極端に小さいポートフォリオ → addable も極小
        current_holdings_by_ticker={'TINY': 0},
        kelly_stats={'TINY': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
        lot_unit_jpy=100_000.0,
    )
    assert result.get('actionable') is False
    assert 'non_actionable_reason' in result['kelly_shadow']


def test_unresolvable_notional_is_skipped_gracefully():
    action = {'ticker': 'AVGO', 'type': 'add'}  # notional 系フィールドが無い
    result = ks.apply_kelly_cap_to_action(
        action, portfolio_total_jpy=10_000_000, current_holdings_by_ticker={},
        kelly_stats={'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
    )
    assert result['kelly_shadow']['capped'] is False
    assert result['kelly_shadow']['cap_skipped_reason'] == 'original_notional_unresolvable'


# ---------------------------------------------------------------------------
# run_kelly_shadow — 副作用ゼロの検証 (Stage 6B の合格条件そのもの)
# ---------------------------------------------------------------------------


def test_run_kelly_shadow_never_touches_action_state(tmp_path, monkeypatch):
    """本題: 影経路は action_state_tracker を一切 import・呼び出ししない。
    実際に action_state.json が作られないことで検証する。"""
    import action_state_tracker as ast

    state_path = tmp_path / "action_state.json"
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    actions = [
        {'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 5_000_000, 'tier': 'long'},
    ]
    ctx = PolicyContext()
    ks.run_kelly_shadow(
        actions, ctx, portfolio_total_jpy=10_000_000,
        current_holdings_by_ticker={'AVGO': 0},
        kelly_stats={'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
    )
    assert not state_path.exists()


def test_run_kelly_shadow_does_not_mutate_input_actions_list():
    actions = [{'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 5_000_000, 'tier': 'long'}]
    original = [dict(a) for a in actions]
    ctx = PolicyContext()
    ks.run_kelly_shadow(
        actions, ctx, portfolio_total_jpy=10_000_000,
        current_holdings_by_ticker={'AVGO': 0},
        kelly_stats={'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
    )
    assert actions == original


def test_run_kelly_shadow_applies_same_policy_rules_as_real_path():
    """VaR 超過等、実経路と同じ policy ルールが影経路にも効くこと。"""
    actions = [{'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 100_000, 'tier': 'long', 'confidence_pct': 80}]
    ctx = PolicyContext(var_1d_95=0.05, var_max_threshold=0.023)  # VaR 超過 → hard reject のはず
    decision = ks.run_kelly_shadow(
        actions, ctx, portfolio_total_jpy=10_000_000,
        current_holdings_by_ticker={'AVGO': 0},
        kelly_stats={'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True}},
    )
    assert len(decision.rejected) >= 1 or len(decision.accepted) == 0


def test_run_kelly_shadow_returns_capped_and_non_actionable_counts():
    actions = [
        {'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 5_000_000, 'tier': 'long'},  # capped
        {'ticker': 'NVDA', 'type': 'add', 'notional_jpy': 10_000, 'tier': 'long'},     # not capped
    ]
    ctx = PolicyContext()
    stats = {
        'AVGO': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True},
        'NVDA': {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10, 'sufficient': True},
    }
    decision = ks.run_kelly_shadow(
        actions, ctx, portfolio_total_jpy=10_000_000,
        current_holdings_by_ticker={'AVGO': 0, 'NVDA': 0}, kelly_stats=stats,
    )
    assert decision.capped_count == 1
    avgo = next(a for a in decision.evaluated if a.get('ticker') == 'AVGO')
    assert avgo['notional_jpy'] < 5_000_000


def test_run_kelly_shadow_applies_side_effect_free_post_filter_after_policy():
    actions = [
        {'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 100_000, 'tier': 'long'},
        {'ticker': 'NVDA', 'type': 'add', 'notional_jpy': 100_000, 'tier': 'long'},
    ]
    seen = []

    def _post_filter(rows):
        seen.extend(rows)
        return {
            'priority_actions': [row for row in rows if row['ticker'] == 'AVGO'],
            '_filtered_actions': [
                {**row, 'filtered_reason': 'cooldown'}
                for row in rows if row['ticker'] == 'NVDA'
            ],
        }

    decision = ks.run_kelly_shadow(
        actions,
        PolicyContext(),
        portfolio_total_jpy=10_000_000,
        current_holdings_by_ticker={'AVGO': 0, 'NVDA': 0},
        kelly_stats={
            ticker: {
                'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03,
                'n': 20, 'sufficient': True,
            }
            for ticker in ('AVGO', 'NVDA')
        },
        post_filter=_post_filter,
    )

    assert {row['ticker'] for row in seen} == {'AVGO', 'NVDA'}
    assert [row['ticker'] for row in decision.accepted] == ['AVGO']
    assert any(row.get('ticker') == 'NVDA' for row in decision.rejected)
    assert decision.post_filter_applied is True
    assert decision.post_filter_filtered_count == 1


def test_post_filter_cannot_raise_notional_above_kelly_cap():
    action = {'ticker': 'AVGO', 'type': 'add', 'notional_jpy': 5_000_000, 'tier': 'long'}

    def _bad_post_filter(rows):
        rows[0]['notional_jpy'] = 900_000
        rows[0]['estimated_notional_jpy'] = 900_000
        return {'priority_actions': rows}

    decision = ks.run_kelly_shadow(
        [action],
        PolicyContext(),
        portfolio_total_jpy=10_000_000,
        current_holdings_by_ticker={'AVGO': 0},
        kelly_stats={
            'AVGO': {
                'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03,
                'n': 20, 'sufficient': True,
            },
        },
        post_filter=_bad_post_filter,
    )

    assert decision.accepted == ()
    assert decision.non_actionable_count == 1
    assert decision.rejected[-1]['filtered_reason'] == 'kelly_cap_post_filter_violation'

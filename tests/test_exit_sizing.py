"""Stage 5E: 決定論的 exit sizing (exit_sizing.py)。

背景: current_weight → target/band → max_step → 税影響 → lot rounding の
どの段階で重複防止・再現性を保証するかが曖昧だった。本テストは計算順序
(8ステップ)・intent_key/evaluation_key の分離・「税務入力unknownならreview、
数量ゼロにしない」契約を検証する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import exit_sizing as es  # noqa: E402
from position_identity import PositionIdentity  # noqa: E402
from tax_lot import CostBasisEstimate  # noqa: E402

POSITION = PositionIdentity("husband", "rakuten", "general", "AVGO")


def _base_kwargs(**overrides):
    kwargs = dict(
        position=POSITION,
        normalized_direction="sell",
        plan_item_id="plan1",
        analysis_id="a1",
        snapshot_hash="hash1",
        current_qty=100.0,
        current_weight=0.10,
        target_weight=0.05,
        band=0.01,
        max_step_qty=50.0,
        lot_size=1.0,
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# キー構造
# ---------------------------------------------------------------------------


def test_intent_key_is_stable_across_snapshots():
    """同じ position/direction/plan_item なら snapshot が変わっても intent_key は同じ。"""
    k1 = es.intent_key(POSITION, "sell", "plan1")
    k2 = es.intent_key(POSITION, "sell", "plan1")
    assert k1 == k2


def test_evaluation_key_changes_with_snapshot():
    intent = es.intent_key(POSITION, "sell", "plan1")
    e1 = es.evaluation_key(intent, analysis_id="a1", snapshot_hash="h1")
    e2 = es.evaluation_key(intent, analysis_id="a1", snapshot_hash="h2")
    assert e1 != e2
    assert e1.startswith(intent)


def test_result_intent_key_matches_new_snapshot_revision():
    """本題: 新しい snapshot で数量が変わっても intent_key は同じ (revision扱い)。"""
    r1 = es.compute_exit_size(**_base_kwargs(snapshot_hash="h1"))
    r2 = es.compute_exit_size(**_base_kwargs(snapshot_hash="h2", current_weight=0.12))
    assert r1.intent_key == r2.intent_key
    assert r1.evaluation_key != r2.evaluation_key


# ---------------------------------------------------------------------------
# band 内は non_actionable
# ---------------------------------------------------------------------------


def test_within_band_is_non_actionable():
    r = es.compute_exit_size(**_base_kwargs(target_weight=0.095, band=0.01))  # gap=0.005 < band
    assert r.status == "non_actionable"
    assert r.final_qty == 0.0


# ---------------------------------------------------------------------------
# Step 2: pending 控除 → revision 判定
# ---------------------------------------------------------------------------


def test_pending_same_intent_is_netted_out_before_max_step():
    """本題: 既存 pending を先に控除しないと、既注文分にも再計算してしまう。"""
    no_pending = es.compute_exit_size(**_base_kwargs(pending_qty_same_intent=0.0))
    with_pending = es.compute_exit_size(**_base_kwargs(pending_qty_same_intent=-20.0))
    # raw_delta は同じだが、pending 控除後の net delta が異なるため最終数量が変わる
    assert with_pending.raw_delta_qty == no_pending.raw_delta_qty
    assert with_pending.final_qty != no_pending.final_qty
    assert with_pending.is_revision_of_pending is True
    assert no_pending.is_revision_of_pending is False


# ---------------------------------------------------------------------------
# Step 3: max_step クランプ
# ---------------------------------------------------------------------------


def test_max_step_clamps_large_delta():
    r = es.compute_exit_size(**_base_kwargs(
        current_weight=0.50, target_weight=0.01, band=0.01, max_step_qty=10.0,
    ))
    assert abs(r.final_qty) <= 10.0


# ---------------------------------------------------------------------------
# Step 4: lot rounding
# ---------------------------------------------------------------------------


def test_lot_rounding_floors_toward_zero():
    r = es.compute_exit_size(**_base_kwargs(lot_size=100.0, max_step_qty=250.0))
    assert r.final_qty % 100.0 == 0


def test_sub_lot_delta_is_non_actionable():
    r = es.compute_exit_size(**_base_kwargs(
        current_weight=0.101, target_weight=0.10, band=0.0, lot_size=1000.0, max_step_qty=5.0,
    ))
    assert r.status == "non_actionable"
    assert r.final_qty == 0.0


# ---------------------------------------------------------------------------
# Step 5: 税務入力 unknown → review、数量ゼロにしない
# ---------------------------------------------------------------------------


def test_unknown_cost_basis_is_review_not_zeroed():
    """本題 (プラン明示契約): 取得原価が分からなければ review。数量はゼロにせず
    人間確認の材料として丸め済みの意図数量を保持する。"""
    r = es.compute_exit_size(**_base_kwargs(
        cost_basis_resolver=lambda position, qty: None,
    ))
    assert r.status == "review"
    assert r.final_qty != 0.0  # ゼロにしない
    assert "review" in r.reason.lower() or "取得原価" in r.reason


def test_known_cost_basis_proceeds_to_actionable():
    known = CostBasisEstimate(
        amount_jpy=10000.0, source="event_ledger", method="total_average_like",
        as_of="2026-07-27", reconciled=False,
    )
    r = es.compute_exit_size(**_base_kwargs(
        cost_basis_resolver=lambda position, qty: known,
    ))
    assert r.status == "actionable"
    assert r.cost_basis is known


def test_no_resolver_supplied_skips_tax_check_entirely():
    """cost_basis_resolver を渡さない場合は税チェック自体をスキップし、
    通常通り actionable になる (5E は税連携を必須にしない — 呼び出し側が
    まだ税データを持たない段階でも使えるようにする)。"""
    r = es.compute_exit_size(**_base_kwargs())
    assert r.status == "actionable"
    assert r.cost_basis is None


# ---------------------------------------------------------------------------
# Step 6: tax_policy_fn による縮小 → 再切り下げ
# ---------------------------------------------------------------------------


def test_tax_policy_fn_shrinks_and_reround():
    """1回だけ10%縮小し、結果が縮小前より小さく・lot単位に丸まっていること。
    (max_iterations=1 で止め、繰り返し縮小によるゼロ収束と区別する — それは
    別テスト test_tax_policy_fn_iteration_count_is_bounded で検証済み)。"""
    known = CostBasisEstimate(
        amount_jpy=10000.0, source="event_ledger", method="total_average_like",
        as_of="2026-07-27", reconciled=False,
    )
    r = es.compute_exit_size(**_base_kwargs(
        current_qty=1000.0, max_step_qty=500.0, lot_size=10.0,
        cost_basis_resolver=lambda position, qty: known,
        tax_policy_fn=lambda qty, cb: qty * 0.9,  # 10%縮小
        max_iterations=1,
    ))
    assert r.status == "actionable"
    assert r.final_qty != 0.0
    assert r.final_qty % 10.0 == 0  # 縮小後も lot 単位
    assert abs(r.final_qty) < abs(r.raw_delta_qty)  # 実際に縮小されている


def test_tax_policy_fn_shrinking_to_zero_is_non_actionable():
    known = CostBasisEstimate(
        amount_jpy=10000.0, source="event_ledger", method="total_average_like",
        as_of="2026-07-27", reconciled=False,
    )
    r = es.compute_exit_size(**_base_kwargs(
        cost_basis_resolver=lambda position, qty: known,
        tax_policy_fn=lambda qty, cb: 0.0,
    ))
    assert r.status == "non_actionable"
    assert r.final_qty == 0.0


def test_tax_policy_fn_iteration_count_is_bounded():
    """最大反復回数を固定 (無限ループにならない)。毎回微妙に違う値を返す
    非収束関数でも max_iterations で打ち切られる。"""
    known = CostBasisEstimate(
        amount_jpy=10000.0, source="event_ledger", method="total_average_like",
        as_of="2026-07-27", reconciled=False,
    )
    calls = {"n": 0}

    def _never_converges(qty, cb):
        calls["n"] += 1
        return qty - 1  # 毎回微妙に減らし続け、lot_size=1 なら丸めても変化し続ける

    r = es.compute_exit_size(**_base_kwargs(
        lot_size=1.0,
        cost_basis_resolver=lambda position, qty: known,
        tax_policy_fn=_never_converges,
        max_iterations=3,
    ))
    assert calls["n"] == 3
    assert r.status in ("actionable", "non_actionable")  # クラッシュしない


# ---------------------------------------------------------------------------
# steps の audit trail
# ---------------------------------------------------------------------------


def test_steps_are_recorded_in_order():
    r = es.compute_exit_size(**_base_kwargs())
    step_numbers = [s["step"] for s in r.steps]
    assert step_numbers == sorted(step_numbers)
    assert step_numbers[0] == 1

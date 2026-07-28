"""exit_sizing.py — Stage 5E: 決定論的 exit sizing

現状の課題: current_weight → target/band → max_step → lot rounding の
どの段階で重複防止・再現性を保証するかが曖昧だった。本モジュールは
計算順序とキー構造を固定する。

計算順（税と丸めが往復するため順序が重要）:
  1. current position と target/band から raw delta
  2. 同じ intent_key の pending / open order を控除
     (先に引かないと既注文分にも税額を再計算してしまう)
  3. max step を適用
  4. 売買単位へ切り下げ
  5. 丸め後の数量で取得原価・税影響を評価
  6. 税 policy (呼び出し側が任意で供給) が数量を縮小したら再度切り下げ
  7. 0単位なら review / non_actionable
  8. evaluation_key を確定

キーを2つに分ける:
  intent_key      = PositionIdentity + normalized_direction + plan_item_id
                    → open order・pending の重複防止に使う
  evaluation_key  = intent_key + analysis_id + snapshot_hash
                    → 再現性の追跡に使う (同じ intent でも snapshot が
                      変われば新しい evaluation)

新しい snapshot で数量が変わっても新規注文にはしない — 既存 intent の
revision として扱う (呼び出し側が cancel/replace するか人間確認するかを
判断する材料として is_revision_of_pending を返す)。

税務入力が unknown なら数量を 0 にせず review — fail-closed だが、
「見えなくする」のではなく「人に見せて止める」。

Medium-tier の構造化 target/band と同じ PositionIdentity を解決できる exit
だけが analyst の影響経路へ配線される。owner/broker を含む取得原価を確定
できない間は status="review" のままで、AI が生成した数量を executable にしない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from position_identity import PositionIdentity

try:
    from tax_lot import CostBasisEstimate
except Exception:  # pragma: no cover - tax_lot は常に import 可能な想定
    CostBasisEstimate = None  # type: ignore

MAX_ITERATIONS = 5  # 税policyによる再切り下げの最大反復回数 (固定)


def intent_key(position: PositionIdentity, normalized_direction: str, plan_item_id: str) -> str:
    """open order・pending の重複防止に使う鍵。"""
    return f"{position.key}|{normalized_direction}|{plan_item_id}"


def evaluation_key(intent: str, *, analysis_id: str, snapshot_hash: str) -> str:
    """再現性の追跡に使う鍵。同じ intent でも snapshot が変われば別の evaluation。"""
    return f"{intent}|{analysis_id}|{snapshot_hash}"


@dataclass(frozen=True)
class ExitSizingResult:
    intent_key: str
    evaluation_key: str
    status: str  # "actionable" | "review" | "non_actionable"
    final_qty: float
    raw_delta_qty: float
    steps: tuple[dict, ...]  # 各ステップの中間値 (audit用)
    reason: Optional[str]
    is_revision_of_pending: bool
    cost_basis: Optional["CostBasisEstimate"]


def _round_down_to_lot(qty: float, lot_size: float) -> float:
    if lot_size <= 0:
        return qty
    import math
    return math.floor(qty / lot_size + 1e-9) * lot_size


def compute_exit_size(
    *,
    position: PositionIdentity,
    normalized_direction: str,
    plan_item_id: str,
    analysis_id: str,
    snapshot_hash: str,
    current_qty: float,
    current_weight: float,
    target_weight: float,
    band: float,
    max_step_qty: float,
    lot_size: float,
    pending_qty_same_intent: float = 0.0,
    cost_basis_resolver: Optional[Callable[[PositionIdentity, float], Optional["CostBasisEstimate"]]] = None,
    tax_policy_fn: Optional[Callable[[float, Optional["CostBasisEstimate"]], float]] = None,
    max_iterations: int = MAX_ITERATIONS,
) -> ExitSizingResult:
    """決定論的 exit sizing。8ステップの計算順を固定する。

    Args:
        pending_qty_same_intent: 既存注文の signed quantity。buy は正、
            sell は負で raw_delta_qty と同じ符号にそろえる。正の注文株数を
            sell にそのまま渡すと二重発注分を控除する代わりに増額してしまう。
        cost_basis_resolver: (position, qty) -> CostBasisEstimate | None。
            None を返す = 取得原価不明。この場合 status="review" になり、
            final_qty は 0 にせず「丸め済みの意図数量」をそのまま保持する
            (呼び出し側が人間確認の材料として見られるように)。
        tax_policy_fn: (qty, cost_basis) -> shrunk_qty。任意。税影響に基づく
            数量縮小ロジックは業務判断なので本モジュールでは発明しない —
            呼び出し側が供給する。供給しない場合は縮小しない (qty のまま)。
        max_iterations: tax_policy_fn による再切り下げの最大反復回数。
            無限ループを避けるため固定 (デフォルト MAX_ITERATIONS)。
    """
    steps: list[dict] = []
    intent = intent_key(position, normalized_direction, plan_item_id)

    # Step 1: raw delta
    weight_gap = target_weight - current_weight
    if abs(weight_gap) <= band:
        return ExitSizingResult(
            intent_key=intent,
            evaluation_key=evaluation_key(intent, analysis_id=analysis_id, snapshot_hash=snapshot_hash),
            status="non_actionable",
            final_qty=0.0,
            raw_delta_qty=0.0,
            steps=tuple(steps),
            reason=f"weight_gap={weight_gap:.4f} は band={band:.4f} 以内",
            is_revision_of_pending=False,
            cost_basis=None,
        )
    # weight_gap を qty に変換するための現在ポジションの平均単価想定が無いため、
    # 呼び出し側は current_weight/target_weight を「同一スケールの qty 相当」として
    # current_qty との比を使う。ここでは current_qty を基準に比例配分する
    # (current_weight が 0 の場合は current_qty のみで delta を決められないため
    # raw_delta は max_step 相当のシグナルとして扱う)。
    if current_weight > 1e-9:
        raw_delta_qty = current_qty * (weight_gap / current_weight)
    else:
        raw_delta_qty = max_step_qty if weight_gap > 0 else -max_step_qty
    steps.append({"step": 1, "name": "raw_delta", "value": raw_delta_qty})

    # Step 2: 同じ intent_key の pending/open order を控除
    is_revision = abs(pending_qty_same_intent) > 1e-9
    net_delta_qty = raw_delta_qty - pending_qty_same_intent
    steps.append({"step": 2, "name": "net_of_pending", "value": net_delta_qty, "pending": pending_qty_same_intent})

    # Step 3: max step
    clamped_qty = max(-max_step_qty, min(max_step_qty, net_delta_qty))
    steps.append({"step": 3, "name": "max_step_clamp", "value": clamped_qty})

    # Step 4: lot rounding
    direction_sign = 1.0 if clamped_qty >= 0 else -1.0
    rounded_qty = direction_sign * _round_down_to_lot(abs(clamped_qty), lot_size)
    steps.append({"step": 4, "name": "lot_round", "value": rounded_qty})

    if abs(rounded_qty) <= 1e-9:
        return ExitSizingResult(
            intent_key=intent,
            evaluation_key=evaluation_key(intent, analysis_id=analysis_id, snapshot_hash=snapshot_hash),
            status="non_actionable",
            final_qty=0.0,
            raw_delta_qty=raw_delta_qty,
            steps=tuple(steps),
            reason="lot 単位未満のため実行不可",
            is_revision_of_pending=is_revision,
            cost_basis=None,
        )

    # Step 5: 丸め後の数量で取得原価・税影響を評価
    cost_basis = None
    if cost_basis_resolver is not None:
        try:
            cost_basis = cost_basis_resolver(position, rounded_qty)
        except Exception:
            cost_basis = None
    steps.append({"step": 5, "name": "cost_basis", "known": cost_basis is not None})

    # 税務入力が unknown なら数量を 0 にせず review (プランの明示的契約)
    if cost_basis_resolver is not None and cost_basis is None:
        return ExitSizingResult(
            intent_key=intent,
            evaluation_key=evaluation_key(intent, analysis_id=analysis_id, snapshot_hash=snapshot_hash),
            status="review",
            final_qty=rounded_qty,
            raw_delta_qty=raw_delta_qty,
            steps=tuple(steps),
            reason="取得原価が不明なため review — 数量はゼロにせず人間確認に委ねる",
            is_revision_of_pending=is_revision,
            cost_basis=None,
        )

    # Step 6: 税 policy による縮小 → 再度切り下げ (最大反復回数を固定)
    final_qty = rounded_qty
    for _ in range(max_iterations):
        if tax_policy_fn is None:
            break
        shrunk = tax_policy_fn(final_qty, cost_basis)
        shrunk_rounded = direction_sign * _round_down_to_lot(abs(shrunk), lot_size)
        if abs(shrunk_rounded - final_qty) <= 1e-9:
            break  # 収束
        final_qty = shrunk_rounded
    steps.append({"step": 6, "name": "tax_policy_shrink", "value": final_qty})

    # Step 7: 0単位なら review/non_actionable
    if abs(final_qty) <= 1e-9:
        return ExitSizingResult(
            intent_key=intent,
            evaluation_key=evaluation_key(intent, analysis_id=analysis_id, snapshot_hash=snapshot_hash),
            status="non_actionable",
            final_qty=0.0,
            raw_delta_qty=raw_delta_qty,
            steps=tuple(steps),
            reason="税policyによる縮小後に0単位になったため実行不可",
            is_revision_of_pending=is_revision,
            cost_basis=cost_basis,
        )

    # Step 8: evaluation_key 確定
    return ExitSizingResult(
        intent_key=intent,
        evaluation_key=evaluation_key(intent, analysis_id=analysis_id, snapshot_hash=snapshot_hash),
        status="actionable",
        final_qty=final_qty,
        raw_delta_qty=raw_delta_qty,
        steps=tuple(steps),
        reason=None,
        is_revision_of_pending=is_revision,
        cost_basis=cost_basis,
    )

"""
policy_engine.py — Deterministic Policy Engine
==============================================

P1-17 + P1-21: AI 提案 (priority_actions) に対する deterministic な制約フィルタ。

設計思想:
  Codex の構造批判で最も重要だった指摘 = 「安全装置がプロンプト注意書きで終わっている」
  への対策。本モジュールは AI の出力を**コード側で**hard / soft に制約する。

  - hard 制約: ex-ante VaR / current DD stage / leverage health
    → reject = 該当アクションは priority_actions から除外、reason を記録
  - AI bounded 制約: earnings blackout
    → 専用の明示理由と十分な信頼度がない場合は reject、ある場合は後段 cap 前提で通過
  - soft 制約: data freshness 低下 / DCA 過熱
    → modify = urgency 降格 / amount_hint に縮小指示 (policy_size_adj)

  ルールは個別関数として実装し、新しいルールは RULES list に追加すれば即時有効。
  全ルールは pure function（副作用なし）、テスト容易性を最優先。

入出力:
  apply_policy_gate(actions, ctx) -> PolicyDecision(accepted, rejected, modified)

呼出側:
  analyst/__init__.py の synthesis 完了後、priority_actions を本関数でフィルタ。
  rejected / modified は ai_portfolio_analysis.json に追加保存して監査可能化。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple, List, Set

from action_amounts import rewrite_action_quantity
from risk_policy import POLICY, classify_drawdown, var_threshold_decimal


# ============================================================
# Context — Policy 評価に必要な集約済み市場・ポートフォリオ状態
# ============================================================

@dataclass
class PolicyContext:
    """
    Policy 判定に必要なすべての ex-ante 入力。
    呼出側 (analyst) が snapshot / risk / macro / freshness / leverage_health を集約して構築する。
    """
    # Risk metrics (ex-ante)
    var_1d_95: Optional[float] = None   # 例: 0.012 = 1.2%
    cvar_1d_95: Optional[float] = None
    current_dd: Optional[float] = None  # 例: -0.05 = -5% (負値で表現)

    # Macro
    vix: Optional[float] = None
    market_regime_mode: Optional[str] = None
    market_regime_levels: dict = field(default_factory=dict)
    market_regime_buy_multipliers: dict = field(default_factory=dict)
    market_regime_shock_active: bool = False

    # Leverage
    leverage_status: Optional[str] = None   # 'safe' | 'warning' | 'deleverage' | 'emergency'

    # Data quality
    data_freshness: Optional[float] = None  # 0..1 (1=完全に新鮮)
    cvar_unstable: bool = False
    # cvar_unstable の理由を区別 (P1-2):
    #   "insufficient_clean_history" = クリーン履歴不足 → margin_buy は soft (half-size 降格)
    #   "tail_small_sample" 等 (実データ有・テール薄い) → 従来どおり margin_buy hard reject
    cvar_reason: Optional[str] = None
    # Actual DD/P&L guard state, separate from synthetic ex-ante parquet DD.
    actual_dd_stage: Optional[str] = None
    # v7 names: P&L shock control and canonical DD are separate metrics.
    loss_guard_stage: Optional[str] = None
    canonical_drawdown_stage: Optional[str] = None
    actual_trading_allowed: Optional[bool] = None
    allow_dca_tranche: bool = False
    dca_active_tranche: Optional[str] = None

    # Ledger / accounting integrity
    ledger_integrity_ok: Optional[bool] = None
    ledger_blocking_issue_count: int = 0
    ledger_unapplied_executed_count: int = 0

    # Tickers under earnings blackout (5 営業日以内に決算)
    earnings_blackout: Set[str] = field(default_factory=set)

    # 閾値 (環境変数で上書き可、通常時デフォルトは objective.md 想定値)
    var_threshold: float = POLICY.var_normal_decimal
    var_max_threshold: float = POLICY.var_absolute_max_decimal
    dd_block_threshold: float = POLICY.dd_block_decimal
    dd_caution_threshold: float = POLICY.dd_caution_decimal
    vix_block_threshold: float = 40.0    # VIX > 40 で全 buy 抑制
    vix_caution_threshold: float = 30.0
    freshness_threshold: float = 0.7


# ============================================================
# Decision — 適用結果
# ============================================================

@dataclass
class PolicyDecision:
    accepted: List[dict] = field(default_factory=list)
    rejected: List[dict] = field(default_factory=list)
    modified: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "accepted_count": len(self.accepted),
            "rejected_count": len(self.rejected),
            "modified_count": len(self.modified),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "modified": self.modified,
        }


# ============================================================
# Rule signature:
#   Rule(action, ctx) -> None | ('reject', reason) | ('modify', new_action, reason)
# ============================================================

# Action type categories
_BUY_TYPES = {"buy", "add", "dca", "margin_buy"}
_SPECULATIVE_TYPES = {"margin_buy", "short"}
_EXECUTABLE_TYPES = _BUY_TYPES | {"sell", "trim", "reduce", "rebalance", "stop_loss", "take_profit", "short", "cover"}
# Every action type the synthesis layer is allowed to emit (see analyst priority_actions
# schema). "hold" is non-executable/advisory. Anything outside this set cannot be gated
# safely, so apply_policy_gate rejects it (fail-closed) rather than passing it through.
_NON_EXECUTABLE_TYPES = {"hold"}
_KNOWN_ACTION_TYPES = _EXECUTABLE_TYPES | _NON_EXECUTABLE_TYPES


def _confidence_pct(action: dict) -> float:
    try:
        return float(action.get("confidence_pct") or 0)
    except (TypeError, ValueError):
        return 0.0


def _nonempty_text(action: dict, *keys: str) -> bool:
    return any(str(action.get(key) or "").strip() for key in keys)


def _is_dca_ladder_action(action: dict) -> bool:
    return (
        str(action.get("type") or "").lower() == "dca"
        and str(action.get("source") or "").strip() == "dca_ladder"
    )


def _rule_ledger_integrity(action: dict, ctx: PolicyContext):
    """
    holdings/account/event_ledger が不整合なら、提案は参考情報に落とす。

    理由:
      sizing・口座選択・売却株数は台帳を前提にする。ok=False のまま
      実行可能アクションとして通すと、保有数/現金残高の誤認から誤発注に直結する。
    """
    if ctx.ledger_integrity_ok is not False:
        return None
    atype = action.get("type", "").lower()
    if atype not in _EXECUTABLE_TYPES:
        return None
    return (
        "reject",
        "Portfolio Ledger Integrity ok=False "
        f"(blocking={ctx.ledger_blocking_issue_count}, "
        f"unapplied={ctx.ledger_unapplied_executed_count})。"
        "保有・現金台帳の照合完了まで実行候補から除外し、参考候補として表示する。",
    )


def _rule_var_budget(action: dict, ctx: PolicyContext):
    """
    ex-ante VaR が threshold を超えた状態で新規 buy/add/dca/margin_buy をすると
    リスクバジェットを更に圧迫する → 全て reject。
    """
    if action.get("type", "").lower() not in _BUY_TYPES:
        return None
    if ctx.var_1d_95 is None or ctx.var_1d_95 < ctx.var_threshold:
        return None
    return ("reject",
            f"ex-ante VaR_1d_95% = {ctx.var_1d_95 * 100:.2f}% が threshold "
            f"{ctx.var_threshold * 100:.2f}% を超過。新規 buy はバジェット解放後に再評価。")


def _rule_dd_stage(action: dict, ctx: PolicyContext):
    """
    current DD で stage 判定:
      - DD ≤ -8%  → 新規 buy 全停止 (reject)
      - DD ≤ -5%  → 警戒 (urgency 降格 + policy_size_adj=0.5)
      - それ以外  → pass
    """
    if action.get("type", "").lower() not in _BUY_TYPES:
        return None
    if ctx.loss_guard_stage and ctx.loss_guard_stage != "ok":
        return ("reject", f"loss_guard_stage={ctx.loss_guard_stage}（日次/30日P&Lショック制御）により新規リスク停止。")
    dd_stage = ctx.canonical_drawdown_stage or classify_drawdown(ctx.current_dd).get("dd_stage")
    if dd_stage in {"objective_breach", "freeze", "derisk_review", "block"}:
        return ("reject", f"canonical_drawdown_stage={dd_stage} により新規リスクは人間レビュー待ち。")
    # Before Slice 3 promotion a missing canonical DD is a visible data-quality
    # caution at execution preflight, not a morning-analysis hard block.
    if dd_stage == "data_confidence_caution":
        return None
    if dd_stage == "caution":
        modified = dict(action)
        if modified.get("urgency") == "high":
            modified["urgency"] = "medium"
        modified["policy_size_adj"] = min(_current_size_adj(modified), 0.5)
        return ("modify", modified,
                "canonical_drawdown_stage=caution → サイズ半減 + urgency 降格")

    return None


def _rule_market_regime_size(action: dict, ctx: PolicyContext):
    """Apply the approved per-market Regime v2 entry-size contract."""
    atype = str(action.get("type") or "").lower()
    if atype not in _BUY_TYPES or ctx.market_regime_mode != "advisory":
        return None

    ticker = str(action.get("ticker") or "")
    market = "JP" if ticker.endswith(".T") else "US"
    level_raw = ctx.market_regime_levels.get(market)
    multiplier_raw = ctx.market_regime_buy_multipliers.get(market)
    try:
        level = int(level_raw)
        multiplier = max(0.0, min(1.0, float(multiplier_raw)))
    except (TypeError, ValueError):
        return None

    dca_exception = (
        _is_dca_ladder_action(action)
        and ctx.allow_dca_tranche
        and ctx.actual_trading_allowed is True
    )
    if atype == "margin_buy" and level < 2:
        return (
            "reject",
            f"Market Regime v2 {market} level={level} では新規レバレッジ禁止。"
            " margin_buy は strong_bull かつ他の全ゲート通過時だけ許容。",
        )
    if ctx.market_regime_shock_active or level <= -2:
        if not dca_exception:
            return (
                "reject",
                f"Market Regime v2 {market} level={level} "
                f"shock={ctx.market_regime_shock_active} のため裁量的な新規buy停止。"
                " activeなdeterministic DCA trancheのみ再評価可能。",
            )
        multiplier = 0.25

    if multiplier >= 1.0:
        return None
    modified = dict(action)
    if modified.get("urgency") == "high" and level <= 0:
        modified["urgency"] = "medium"
    modified["policy_size_adj"] = min(_current_size_adj(modified), multiplier)
    modified["policy_market_regime"] = {
        "market": market,
        "level": level,
        "size_multiplier": multiplier,
        "shock": ctx.market_regime_shock_active,
    }
    return (
        "modify",
        modified,
        f"Market Regime v2 {market} level={level} → "
        f"entry size上限 {multiplier:.2f}x",
    )


def _rule_leverage_block(action: dict, ctx: PolicyContext):
    """
    leverage_status が warning/deleverage/emergency のときに新規信用建てを全 reject。
    behavioral_guard.evaluate_leverage_health の出力を直接利用する想定。
    """
    atype = action.get("type", "").lower()
    if atype not in {"margin_buy", "short"}:
        return None
    if ctx.leverage_status not in ("warning", "deleverage", "emergency"):
        return None
    return ("reject",
            f"leverage_status = '{ctx.leverage_status}' で type={atype} の新規信用建ては禁止。"
            " trim/sell/cover でレバレッジを下げてから再評価。")


def _rule_earnings_blackout(action: dict, ctx: PolicyContext):
    """
    決算 5 営業日以内の銘柄への buy/add/dca は通常 reject。

    AI 自律判断 v2: 決算そのものを catalyst として明示的に取りに行く場合だけ、
    後段 post-filter の 0.5% cap を前提に通す。VaR/DD/ledger/leverage はこの rule
    より先に評価されるため、破綻防止ゲートは上書きできない。
    """
    if action.get("type", "").lower() not in _BUY_TYPES:
        return None
    ticker = action.get("ticker", "")
    if not ticker or ticker not in ctx.earnings_blackout:
        return None
    has_explicit_event_reason = bool(action.get("earnings_event_trade")) and _nonempty_text(
        action,
        "earnings_event_reason",
        "ai_override_reason",
        "bounded_decision_reason",
    )
    if has_explicit_event_reason and _confidence_pct(action) >= 75:
        modified = dict(action)
        modified["ai_bounded_gate"] = "earnings_blackout"
        modified["policy_earnings_blackout_override"] = True
        modified.setdefault("provisional_decision", True)
        return (
            "modify",
            modified,
            f"{ticker} は earnings_blackout 中だが、AI が決算イベント取引として明示。後段 cap 必須。",
        )
    return ("reject",
            f"{ticker} は決算 5 営業日以内 (earnings_blackout)。"
            " 決算イベント取引として明示し confidence>=75 かつ後段 cap を満たす場合のみ再評価。")


def _rule_freshness_downgrade(action: dict, ctx: PolicyContext):
    """
    data_freshness < threshold のとき urgency=high を medium に降格する soft 制約。
    """
    if action.get("urgency") != "high":
        return None
    if ctx.data_freshness is None or ctx.data_freshness >= ctx.freshness_threshold:
        return None
    modified = dict(action)
    modified["urgency"] = "medium"
    modified["policy_freshness_downgraded"] = True
    return ("modify", modified,
            f"data_freshness = {ctx.data_freshness:.2f} < "
            f"{ctx.freshness_threshold:.2f} → urgency=high を medium に降格")


def _rule_cvar_unstable(action: dict, ctx: PolicyContext):
    """
    CVaR tail sample が不足している時はリスク推定を過信しない。

    P1-2: cvar_reason で margin_buy の扱いを分岐:
      - "insufficient_clean_history" (クリーン NAV 履歴不足) → margin_buy も hard reject せず
        soft (サイズ半減 + urgency 降格)。恒久ブロックを避ける。実 DD/VIX/leverage は別 rule が gating。
      - それ以外 (tail_small_sample 等、実データはあるがテールが薄い) → 従来どおり margin_buy は reject。
    通常 buy/add/dca はどちらの理由でもサイズ半減 + urgency 降格。
    """
    if not ctx.cvar_unstable:
        return None
    atype = action.get("type", "").lower()
    _soft_margin = (ctx.cvar_reason == "insufficient_clean_history")
    if atype == "margin_buy" and not _soft_margin:
        return ("reject", "cvar_unstable=true (tail sample 不足) のため、テールリスク推定が安定するまで margin_buy 禁止。")
    # margin_buy ∈ _BUY_TYPES。soft (insufficient_clean_history) の margin_buy はここで降格扱いになる。
    if atype in _BUY_TYPES:
        modified = dict(action)
        if modified.get("urgency") == "high":
            modified["urgency"] = "medium"
        modified["policy_size_adj"] = min(_current_size_adj(modified), 0.5)
        modified["policy_cvar_unstable_downgraded"] = True
        _why = ("cvar_unstable (insufficient_clean_history) → サイズ半減 + urgency 降格"
                if _soft_margin else "cvar_unstable=true → サイズ半減 + urgency 降格")
        return ("modify", modified, _why)
    return None


def _rule_vix_extreme(action: dict, ctx: PolicyContext):
    """
    VIX > 40 (capitulation) のとき投機系 (margin_buy / short) を reject、buy は urgency 降格。
    """
    if ctx.vix is None or ctx.vix < ctx.vix_block_threshold:
        return None
    atype = action.get("type", "").lower()
    if atype in _SPECULATIVE_TYPES:
        return ("reject",
                f"VIX = {ctx.vix:.1f} ≥ {ctx.vix_block_threshold:.0f} (capitulation) で "
                f"投機系 type={atype} は停止。")
    if atype in _BUY_TYPES and action.get("urgency") == "high":
        modified = dict(action)
        modified["urgency"] = "medium"
        modified["policy_vix_downgraded"] = True
        return ("modify", modified,
                f"VIX = {ctx.vix:.1f} ≥ {ctx.vix_block_threshold:.0f} → "
                f"buy urgency=high を medium に降格")
    return None


# ============================================================
# Rule registry — 順序が評価順 (上から下へ)
# ============================================================

Rule = Callable[[dict, PolicyContext], Optional[Tuple]]

RULES: List[Rule] = [
    _rule_ledger_integrity,
    _rule_var_budget,
    _rule_dd_stage,
    _rule_market_regime_size,
    _rule_leverage_block,
    _rule_earnings_blackout,
    _rule_cvar_unstable,
    _rule_vix_extreme,
    _rule_freshness_downgrade,
]


# ============================================================
# Size enforcement helpers (P1-#6: policy_size_adj must hit real quantities)
# ============================================================

def _current_size_adj(action: dict) -> float:
    """現在の policy_size_adj を float で返す (未設定/不正は 1.0)。"""
    raw = action.get("policy_size_adj")
    if raw is None:
        return 1.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if val <= 0:
        return 1.0
    return val


def _scale_size_field(raw, factor: float, *, unit: int = 1) -> Tuple[object, bool]:
    """
    数量/金額フィールドを factor 倍する。
    Returns (new_value, collapsed)。collapsed=True は「数量が 1 単元 (unit) 未満に潰れた」=発注不能。

    - unit: 売買単元。通常の日本株 (.T) は 100、かぶミニ指定の日本株現物買いは 1。
      数量は unit の倍数に floor する
      (Codex re-review #6: 後段の 100 株丸めで policy 半減が増額され打ち消されるのを防ぐ)。
    - 数値: floor(scaled/unit)*unit。元が >=unit で結果 <unit なら collapsed。
    - 文字列: 先頭付近の数値を抽出して倍率適用。分類はカンマでは判定しない:
      株/口/share suffix あり、または ¥/円 の無い bare number (例 "2", "1,100株", "100口") = 数量
      (unit floor・<unit で collapsed)、数量 suffix が無く ¥/円 がある (例 "¥150,000") = 金額
      (四捨五入・collapse無し)。suffix は保持。
    - 解釈不能/真偽値: そのまま返す (collapsed=False)。
    """
    if isinstance(raw, bool) or raw is None:
        return raw, False
    if isinstance(raw, (int, float)):
        scaled = float(raw) * factor
        if float(raw) >= 1:
            new = int(math.floor(scaled / unit) * unit)
            return new, (float(raw) > 0 and new < unit)
        return scaled, False
    if not isinstance(raw, str):
        return raw, False
    m = re.search(r"[\d,]+(?:\.\d+)?", raw)
    if not m:
        return raw, False
    token = m.group(0)
    try:
        num = float(token.replace(",", ""))
    except ValueError:
        return raw, False
    prefix, suffix = raw[: m.start()], raw[m.end():]
    # Codex re-re-review #6: カンマは金額の指標にしない ("1,100株" は数量)。
    # 株/口/share の数量 suffix があれば数量扱い (単元 floor)、無く通貨記号 (¥/円) があれば金額。
    _has_qty_unit = ("株" in raw) or ("口" in raw) or ("share" in suffix.lower())
    is_amount = (("¥" in raw) or ("円" in raw)) and not _has_qty_unit
    scaled = num * factor
    if is_amount:
        new_num = int(round(scaled))
        return f"{prefix}{new_num}{suffix}", False
    new_num = int(math.floor(scaled / unit) * unit)
    if _has_qty_unit:
        qty_label = "口" if "口" in raw else ("株" if "株" in raw else " shares")
        # Embedded money/prose is discarded. The structured notional is scaled
        # separately and rendered only at API/UI boundaries.
        return f"{new_num}{qty_label}", (num > 0 and new_num < unit)
    return f"{prefix}{new_num}{suffix}", (num > 0 and new_num < unit)


_SIZE_FIELDS = ("amount_hint", "shares", "quantity", "amount")
_NOTIONAL_FIELDS = ("estimated_notional_jpy", "notional_jpy", "amount_jpy")


def _numeric_ratio(before, after) -> Optional[float]:
    """Return the actual size ratio after lot rounding, if both are numeric."""
    def _extract(value):
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str):
            return None
        match = re.search(r"[\d,]+(?:\.\d+)?", value)
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", ""))
        except ValueError:
            return None

    old_value = _extract(before)
    new_value = _extract(after)
    if old_value is None or new_value is None or old_value <= 0:
        return None
    return max(0.0, min(1.0, new_value / old_value))


def _is_kabu_mini_cash_buy(action: dict) -> bool:
    """楽天かぶミニ台帳で確認済みの日本株現物 buy/add は 1 株単位で扱う。"""
    ticker = str(action.get("ticker") or "")
    atype = str(action.get("type") or "").lower()
    if not ticker.endswith(".T") or atype not in {"buy", "add"}:
        return False
    try:
        from kabu_mini_eligibility import action_requests_kabu_mini, is_kabu_mini_eligible
        channel = str(action.get("execution_channel") or action.get("broker_channel") or "")
        return action_requests_kabu_mini(action) and is_kabu_mini_eligible(ticker, channel=channel)
    except Exception:
        return False


def _lot_unit(action: dict) -> int:
    """銘柄の売買単元。JPX ETFの公式単位を普通株より優先する。"""
    from instrument_metadata import canonical_ticker, trading_unit_for_ticker

    if canonical_ticker(action.get("ticker")).endswith(".T"):
        return 1 if _is_kabu_mini_cash_buy(action) else trading_unit_for_ticker(action.get("ticker"))
    return 1


def _apply_size_adj(action: dict) -> Tuple[dict, Optional[str]]:
    """
    policy_size_adj (<1) を実際の数量/金額フィールドへ強制適用する。
    Returns (action, collapse_reason)。collapse_reason!=None なら呼出側で reject。

    Codex re-review #6: 市場単元 (.T=通常100株、かぶミニ現物は1株) まで policy 内で確定させ、1 単元未満に潰れる
    縮小は発注不能として reject。適用したら policy_size_final を立て、後段の 100 株丸め
    (analyst/__init__.py) が policy 出力を増額しないようにする。
    """
    factor = _current_size_adj(action)
    if factor >= 1.0:
        return action, None
    unit = _lot_unit(action)
    out = dict(action)
    applied = {}
    collapsed_fields: List[str] = []
    actual_ratios: List[float] = []
    quantity_rewrite: Optional[Tuple[str, str]] = None
    for fld in _SIZE_FIELDS:
        if fld not in out:
            continue
        old_val = out[fld]
        new_val, collapsed = _scale_size_field(old_val, factor, unit=unit)
        if new_val != old_val:
            applied[fld] = {"from": old_val, "to": new_val}
            out[fld] = new_val
            ratio = _numeric_ratio(old_val, new_val)
            if ratio is not None:
                actual_ratios.append(ratio)
            if fld == "amount_hint":
                old_match = re.search(
                    r"[\d,]+(?:\.\d+)?\s*(?:株|口|shares?)",
                    str(old_val),
                    re.IGNORECASE,
                )
                new_match = re.search(
                    r"[\d,]+(?:\.\d+)?\s*(?:株|口|shares?)",
                    str(new_val),
                    re.IGNORECASE,
                )
                if old_match and new_match:
                    quantity_rewrite = (old_match.group(0), new_match.group(0))
        if collapsed:
            collapsed_fields.append(fld)
    # Rewrite only when the order token is unambiguous. A plain str.replace()
    # can corrupt the holding quantity in "保有30口のうち30口を売却".
    if quantity_rewrite:
        old_quantity, new_quantity = quantity_rewrite
        old_action = str(out.get("action") or "")
        new_action, rewrite_status = rewrite_action_quantity(
            old_action,
            old_hint=old_quantity,
            new_hint=new_quantity,
        )
        out["action_quantity_sync_status"] = rewrite_status
        if rewrite_status == "rewritten":
            applied["action"] = {"from": old_action, "to": new_action}
            out["action"] = new_action
        else:
            out["action_quantity_sync_failed"] = True
    # Keep audit/funding notionals consistent with the final rounded quantity.
    # The smallest observed ratio is conservative when multiple size fields
    # have different lot-rounding effects.
    effective_ratio = min(actual_ratios) if actual_ratios else factor
    for fld in _NOTIONAL_FIELDS:
        if fld not in out:
            continue
        try:
            old_notional = float(out[fld])
        except (TypeError, ValueError):
            continue
        new_notional = int(round(old_notional * effective_ratio))
        if new_notional != out[fld]:
            applied[fld] = {"from": out[fld], "to": new_notional}
            out[fld] = new_notional
    if applied:
        out["policy_size_applied"] = applied
        out["policy_size_final"] = True  # 後段の単元丸めで増額しない印
    if collapsed_fields:
        return out, (
            f"policy_size_adj={factor} 適用で {','.join(collapsed_fields)} が "
            f"1 単元 (unit={unit}) 未満に潰れたため発注不能 → reject。"
        )
    return out, None


# ============================================================
# Engine
# ============================================================

def apply_policy_gate(actions: List[dict], ctx: PolicyContext) -> PolicyDecision:
    """
    AI 提案 actions を policy rules でフィルタする。
    Returns PolicyDecision(accepted, rejected, modified)。

    挙動 (fail-closed):
      - 未知の action type (gating 不能) は reject。
      - 各 action に対して RULES を順に評価。
      - ルールが例外を投げたら、その制約を評価できていない = 安全側で当該 action を reject。
      - 最初の reject verdict で打ち切り、reason を記録して捨てる。
      - 未知 verdict も評価不能として reject。
      - modify は累積 (次の rule は modified action を入力にする)。
      - 全 pass / modify のみ通過後、policy_size_adj を実数量へ適用。
        サイズが 1 株/口未満に潰れる場合は reject。
    """
    if not isinstance(actions, list):
        return PolicyDecision()

    decision = PolicyDecision()

    for original in actions:
        if not isinstance(original, dict):
            continue
        current = dict(original)
        modifications: List[str] = []
        rejected_reason: Optional[str] = None
        rejected_rule: Optional[str] = None

        atype = str(current.get("type", "")).lower()
        if atype not in _KNOWN_ACTION_TYPES:
            decision.rejected.append({
                "action": original,
                "rule": "unknown_action_type",
                "reason": (
                    f"未知の action type='{current.get('type')}' は policy gating 不能のため "
                    "安全側で reject。"
                ),
            })
            continue

        for rule in RULES:
            try:
                res = rule(current, ctx)
            except Exception as e:
                # 安全ルールの評価に失敗 = その制約を保証できない → fail-closed で reject。
                rejected_reason = f"安全ルール {rule.__name__} の評価に失敗 ({e}) → fail-closed で reject。"
                rejected_rule = f"rule_error:{rule.__name__}"
                break
            if res is None:
                continue
            verdict = res[0]
            if verdict == "reject":
                rejected_reason = res[1]
                rejected_rule = rule.__name__
                break
            elif verdict == "modify":
                current = res[1]
                modifications.append(f"[{rule.__name__}] {res[2]}")
            else:
                # 未知 verdict — 評価結果を解釈できない → fail-closed で reject。
                rejected_reason = f"安全ルール {rule.__name__} が未知 verdict='{verdict}' を返した → fail-closed で reject。"
                rejected_rule = f"unknown_verdict:{rule.__name__}"
                break

        if rejected_reason is None:
            # サイズ縮小指示 (policy_size_adj<1) を実数量へ強制適用。
            current, collapse_reason = _apply_size_adj(current)
            if collapse_reason is not None:
                rejected_reason = collapse_reason
                rejected_rule = "policy_size_collapsed"
            elif "policy_size_applied" in current:
                modifications.append(
                    f"[policy_size_adj] サイズ {current.get('policy_size_adj')}x を数量へ適用"
                )

        if rejected_reason:
            decision.rejected.append({
                "action": original,
                "rule": rejected_rule,
                "reason": rejected_reason,
            })
        else:
            if modifications:
                decision.modified.append({
                    "original": original,
                    "modified": current,
                    "modifications": modifications,
                })
            decision.accepted.append(current)

    return decision


# ============================================================
# Context builder helpers — analyst から渡すデータを統一する
# ============================================================

def build_context_from_synthesis_inputs(
    *,
    risk: Optional[dict] = None,
    macro: Optional[dict] = None,
    leverage_health: Optional[dict] = None,
    freshness_score: Optional[float] = None,
    earnings_blackout_tickers: Optional[List[str]] = None,
    portfolio_integrity: Optional[dict] = None,
) -> PolicyContext:
    """
    analyst/synthesis から渡される dict / float を PolicyContext に詰める helper。
    各入力は欠落可（None / 空）— 該当 rule は自動的に no-op。
    """
    risk = risk or {}
    macro = macro or {}
    leverage_health = leverage_health or {}
    portfolio_integrity = portfolio_integrity or {}
    regime_v2 = (
        macro.get("market_regime_v2")
        if isinstance(macro.get("market_regime_v2"), dict)
        else {}
    )
    regime_policy = (
        regime_v2.get("policy")
        if isinstance(regime_v2.get("policy"), dict)
        else {}
    )

    # v7 contract: policy consumes only explicitly named decimal fields.
    # Never infer whether an arbitrary magnitude is a percent or a decimal.
    def _decimal_field(name: str) -> float | None:
        try:
            value = risk.get(name)
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    var_decimal = _decimal_field("var_95_decimal")
    cvar_decimal = _decimal_field("cvar_95_decimal")
    dd_decimal = _decimal_field("enforced_flow_adjusted_dd_decimal")
    try:
        ledger_blocking = int(portfolio_integrity.get("blocking_issue_count") or 0)
    except (TypeError, ValueError):
        ledger_blocking = 0
    summary = portfolio_integrity.get("summary") if isinstance(portfolio_integrity.get("summary"), dict) else {}
    try:
        unapplied = int(summary.get("unapplied_executed_count") or 0)
    except (TypeError, ValueError):
        unapplied = 0

    def _default_var_threshold() -> float:
        try:
            vix = float(macro.get("vix")) if macro.get("vix") is not None else None
        except (TypeError, ValueError):
            vix = None
        scenario_key = str(macro.get("scenario_key") or macro.get("scenario") or "").upper()
        regime_label = str(macro.get("regime") or macro.get("hmm_regime") or "")
        regime_upper = regime_label.upper()
        loss_stage = str(risk.get("loss_guard_stage") or "").lower()
        bull = (
            scenario_key == "BULL"
            or "強気" in regime_label
            or bool(macro.get("regime_bull_confirmed"))
        )
        stress = (
            scenario_key in {"BEAR", "DEFENSIVE", "STRESS"}
            or "BEAR" in regime_upper
            or "DEFENSIVE" in regime_upper
            or "弱気" in regime_label
            or loss_stage in {"daily_block", "stage_1", "stage_2", "stage_3"}
            or (vix is not None and vix >= 30)
        )
        if stress:
            return POLICY.var_bear_decimal
        if bull and vix is not None and vix < 25:
            return POLICY.var_bull_decimal
        return POLICY.var_normal_decimal

    _var_threshold = _default_var_threshold()

    return PolicyContext(
        var_1d_95         = var_decimal,
        cvar_1d_95        = cvar_decimal,
        current_dd        = dd_decimal,
        vix               = (float(macro["vix"]) if macro.get("vix") is not None else None),
        market_regime_mode = (
            str(regime_v2.get("mode"))
            if regime_v2.get("mode") is not None
            and bool((regime_v2.get("portfolio") or {}).get("eligible"))
            else None
        ),
        market_regime_levels = dict(regime_policy.get("market_levels") or {}),
        market_regime_buy_multipliers = dict(
            regime_policy.get("market_buy_size_multipliers") or {}
        ),
        market_regime_shock_active = bool(
            (regime_v2.get("shock") or {}).get("active")
        ),
        leverage_status   = (leverage_health.get("status") if isinstance(leverage_health, dict) else None),
        data_freshness    = freshness_score,
        cvar_unstable     = bool(risk.get("cvar_unstable", False)),
        cvar_reason       = risk.get("cvar_reason"),
        actual_dd_stage   = None,  # legacy name intentionally no longer sourced
        loss_guard_stage  = risk.get("loss_guard_stage"),
        canonical_drawdown_stage = risk.get("enforced_drawdown_stage") or classify_drawdown(dd_decimal).get("dd_stage"),
        actual_trading_allowed = risk.get("trading_allowed"),
        allow_dca_tranche = bool(risk.get("allow_dca_tranche", False)),
        dca_active_tranche = risk.get("dca_active_tranche"),
        ledger_integrity_ok = (
            bool(portfolio_integrity.get("ok"))
            if "ok" in portfolio_integrity else None
        ),
        ledger_blocking_issue_count = ledger_blocking,
        ledger_unapplied_executed_count = unapplied,
        earnings_blackout = set(earnings_blackout_tickers or []),
        var_threshold       = _var_threshold,
        var_max_threshold   = POLICY.var_absolute_max_decimal,
        dd_block_threshold  = POLICY.dd_block_decimal,
        dd_caution_threshold= POLICY.dd_caution_decimal,
    )

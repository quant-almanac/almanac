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
    var_threshold: float = 0.016        # 通常時: ex-ante VaR_1d_95% ≤ 1.6%
    var_max_threshold: float = 0.023    # 絶対上限: ex-ante VaR_1d_95% ≤ 2.3%
    dd_block_threshold: float = -0.08    # DD ≤ -8% で新規 buy 全停止
    dd_caution_threshold: float = -0.05  # DD ≤ -5% で警戒 (サイズ半減)
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
    if ctx.actual_dd_stage in {"block", "daily_block", "monthly_block", "stage_1", "stage_2", "stage_3"}:
        if _is_dca_ladder_action(action) and ctx.allow_dca_tranche:
            # trading_allowed は True と確認できた場合のみ例外を許す (None=欠落は fail-closed)。
            if ctx.actual_trading_allowed is not True or ctx.actual_dd_stage in {"stage_3", "daily_block"}:
                return ("reject",
                        f"actual_dd_stage={ctx.actual_dd_stage} かつ trading_allowed="
                        f"{ctx.actual_trading_allowed} のため DCA 例外も停止。")
            modified = dict(action)
            if modified.get("urgency") == "high":
                modified["urgency"] = "medium"
            modified["policy_size_adj"] = min(_current_size_adj(modified), 0.5)
            modified["policy_dca_dd_exception"] = True
            if ctx.dca_active_tranche:
                modified["policy_dca_active_tranche"] = ctx.dca_active_tranche
            return ("modify", modified,
                    f"actual_dd_stage={ctx.actual_dd_stage} だが DCA ラダー "
                    "deterministic 例外によりサイズ半減で通過。")
        return ("reject",
                f"actual_dd_stage={ctx.actual_dd_stage}（実損益ガード）により新規 buy 停止。")
    if ctx.actual_dd_stage == "caution":
        # 実損益ガードが警戒 → deterministic にサイズ半減。数値 current_dd での再判定はしない。
        modified = dict(action)
        if modified.get("urgency") == "high":
            modified["urgency"] = "medium"
        modified["policy_size_adj"] = min(_current_size_adj(modified), 0.5)
        return ("modify", modified,
                "actual_dd_stage=caution（実損益ガード警戒）→ サイズ半減 + urgency 降格")
    if ctx.actual_dd_stage == "ok":
        # 実損益ガードが健全と評価済みなら stage が権威。数値 current_dd による再判定は行わない
        # (単位誤読や合成系列 DD が stage=ok を上書きし、高値圏の凪の日に buy を全停止した事故の再発防止)。
        return None
    if ctx.current_dd is None:
        return None

    if ctx.current_dd <= ctx.dd_block_threshold:
        if _is_dca_ladder_action(action) and ctx.allow_dca_tranche and ctx.actual_trading_allowed is True:
            modified = dict(action)
            if modified.get("urgency") == "high":
                modified["urgency"] = "medium"
            modified["policy_size_adj"] = min(_current_size_adj(modified), 0.5)
            modified["policy_dca_dd_exception"] = True
            if ctx.dca_active_tranche:
                modified["policy_dca_active_tranche"] = ctx.dca_active_tranche
            return ("modify", modified,
                    f"current_dd = {ctx.current_dd * 100:.1f}% だが DCA ラダー "
                    "deterministic 例外によりサイズ半減で通過。")
        return ("reject",
                f"current_dd = {ctx.current_dd * 100:.1f}% ≤ "
                f"{ctx.dd_block_threshold * 100:.0f}%（危険ステージ）。新規 buy 停止。")

    if ctx.current_dd <= ctx.dd_caution_threshold:
        modified = dict(action)
        if modified.get("urgency") == "high":
            modified["urgency"] = "medium"
        modified["policy_size_adj"] = min(_current_size_adj(modified), 0.5)
        return ("modify", modified,
                f"current_dd = {ctx.current_dd * 100:.1f}% ≤ "
                f"{ctx.dd_caution_threshold * 100:.0f}%（警戒）→ サイズ半減 + urgency 降格")

    return None


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
    return f"{prefix}{new_num}{suffix}", (num > 0 and new_num < unit)


_SIZE_FIELDS = ("amount_hint", "shares", "quantity", "amount")


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
    for fld in _SIZE_FIELDS:
        if fld not in out:
            continue
        new_val, collapsed = _scale_size_field(out[fld], factor, unit=unit)
        if new_val != out[fld]:
            applied[fld] = {"from": out[fld], "to": new_val}
            out[fld] = new_val
        if collapsed:
            collapsed_fields.append(fld)
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
    import os

    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, str(default)))
        except (TypeError, ValueError):
            return default

    risk = risk or {}
    macro = macro or {}
    leverage_health = leverage_health or {}
    portfolio_integrity = portfolio_integrity or {}

    # VaR / DD の単位検出:
    #   コードベースの慣習として risk[*] は % 表示で保存される (例 0.8 = 0.8%)。
    #   ただし呼び出し元によっては既に小数 (0.008) で渡るケースもある。
    #   閾値判定: 絶対値 > 0.1 (= 10%) なら "% 表示" と判定して /100、それ以下は小数。
    #   日次 VaR_95 が 10% を超える状況はそもそも危険水準で、小数 0.10 = 10% でも
    #   threshold 比較は同じく機能する (どちらに転んでも誤判定にならない安全圏)。
    def _to_decimal(raw):
        if raw is None:
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v / 100.0 if abs(v) > 0.1 else v

    var_decimal = _to_decimal(risk.get("var_95"))
    if var_decimal is not None:
        var_decimal = abs(var_decimal)
    cvar_decimal = _to_decimal(risk.get("cvar_95") or risk.get("cvar_pct"))
    if cvar_decimal is not None:
        cvar_decimal = abs(cvar_decimal)
    # actual_current_dd は data_gatherer が常に percent 表記 (round(x*100, 2)) で書く契約。
    # _to_decimal のヒューリスティック (|v| > 0.1 なら %) は |v| ≤ 0.1 の percent 値
    # (例: -0.1 = -0.1%) を小数表記 (-10%) と誤読し、ほぼ高値圏の凪の日に dd_block を
    # 誤発動させた (2026-07-07) — 実ガード値は推測せず無条件に /100 する。
    dd_actual = risk.get("actual_current_dd")
    if dd_actual is not None:
        try:
            dd_decimal = float(dd_actual) / 100.0
        except (TypeError, ValueError):
            dd_decimal = _to_decimal(risk.get("current_dd"))
    else:
        dd_decimal = _to_decimal(risk.get("current_dd"))
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
        """Use normal/stress/bull tiers while DD rules still size entries down."""
        try:
            vix = float(macro.get("vix")) if macro.get("vix") is not None else None
        except (TypeError, ValueError):
            vix = None
        scenario_key = str(macro.get("scenario_key") or macro.get("scenario") or "").upper()
        regime_label = str(macro.get("regime") or macro.get("hmm_regime") or "")
        regime_upper = regime_label.upper()
        actual_stage = str(risk.get("actual_dd_stage") or "").lower()
        bull = (
            scenario_key == "BULL"
            or "強気" in regime_label
            or bool(macro.get("regime_bull_confirmed"))
        )
        if bull and vix is not None and vix < 25:
            return 0.020
        stress = (
            scenario_key in {"BEAR", "DEFENSIVE", "STRESS"}
            or "BEAR" in regime_upper
            or "DEFENSIVE" in regime_upper
            or "弱気" in regime_label
            or actual_stage in {"block", "daily_block", "monthly_block", "stage_1", "stage_2", "stage_3"}
            or (vix is not None and vix >= 30)
        )
        return 0.012 if stress else 0.016

    _var_max_threshold = _env_float("POLICY_VAR_MAX_THRESHOLD", 0.023)
    _var_threshold = min(
        _env_float("POLICY_VAR_THRESHOLD", _default_var_threshold()),
        _var_max_threshold,
    )

    return PolicyContext(
        var_1d_95         = var_decimal,
        cvar_1d_95        = cvar_decimal,
        current_dd        = dd_decimal,
        vix               = (float(macro["vix"]) if macro.get("vix") is not None else None),
        leverage_status   = (leverage_health.get("status") if isinstance(leverage_health, dict) else None),
        data_freshness    = freshness_score,
        cvar_unstable     = bool(risk.get("cvar_unstable", False)),
        cvar_reason       = risk.get("cvar_reason"),
        actual_dd_stage   = risk.get("actual_dd_stage"),
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
        var_max_threshold   = _var_max_threshold,
        dd_block_threshold  = _env_float("POLICY_DD_BLOCK_THRESHOLD", -0.08),
        dd_caution_threshold= _env_float("POLICY_DD_CAUTION_THRESHOLD",-0.05),
        vix_block_threshold = _env_float("POLICY_VIX_BLOCK_THRESHOLD", 40.0),
        vix_caution_threshold=_env_float("POLICY_VIX_CAUTION_THRESHOLD",30.0),
        freshness_threshold = _env_float("POLICY_FRESHNESS_THRESHOLD", 0.7),
    )

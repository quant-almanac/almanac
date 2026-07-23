"""
action_stage_log.py — Append-only pipeline action stage log.

各分析実行でアクションがパイプラインを流れる全ステージを JSONL で記録する。
既存の ai_recommendation_log.json は事後価格検証専用として残す。
本ファイルは coverage / audit 専用。

ステージ:
  tier_generated   → 各ティア (Long/Medium/Swing/MarginLong/ShortSell) が出力したアクション
  opus_raw         → Opus 合成後・policy gate 前 (raw_priority_actions)
  policy_accepted  → Policy Engine が通したアクション
  policy_rejected  → Policy Engine が拒否したアクション
  post_filter_final → 全後処理後の最終 priority_actions
  post_filter_rejected → post-filter で除外されたアクション
  post_filter_deferred → open-order-aware sizing で実行保留されたアクション
  executed         → 実際に執行した記録（actions.py から呼ぶ）

必須フィールド (analysis_id 単位で集計できる):
  analysis_id, as_of, stage, ticker, tier, canonical_action_type, direction,
  scenario_key, regime, actual_dd_stage, leverage_status,
  filter_rule, filtered_reason, eligible, eligibility_reason,
  estimated_notional_jpy, logged_at
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
LOG_PATH = BASE_DIR / "action_stage_log.jsonl"

# ── 方向分類 ─────────────────────────────────────────────────
_SELL_DIRECTION = {"sell", "trim", "reduce", "stop_loss", "take_profit", "short"}
_BUY_DIRECTION  = {"buy", "add", "dca", "margin_buy", "cover"}
_NEUTRAL        = {"rebalance", "hold"}


def _canonical_type(raw: str) -> str:
    return str(raw or "").lower().strip() or "unknown"


def _direction(action_type: str) -> str:
    t = _canonical_type(action_type)
    if t in _SELL_DIRECTION:
        return "sell"
    if t in _BUY_DIRECTION:
        return "buy"
    return "neutral"


# ── エントリ構築 ─────────────────────────────────────────────

def _make_entry(
    *,
    analysis_id: str,
    as_of: str,
    stage: str,
    action: dict,
    scenario_key: Optional[str] = None,
    regime: Optional[str] = None,
    actual_dd_stage: Optional[str] = None,
    leverage_status: Optional[str] = None,
    filter_rule: Optional[str] = None,
    filtered_reason: Optional[str] = None,
    eligible: Optional[bool] = None,
    eligibility_reason: Optional[str] = None,
    estimated_notional_jpy: Optional[float] = None,
) -> dict:
    atype = _canonical_type(action.get("type", ""))
    if estimated_notional_jpy is None:
        estimated_notional_jpy = action.get("estimated_notional_jpy")
    if filtered_reason is None:
        filtered_reason = action.get("filtered_reason")
    if filter_rule is None and filtered_reason:
        filter_rule = str(filtered_reason).split(":", 1)[0]
    if eligible is None and filtered_reason:
        eligible = False
    entry = {
        "analysis_id":            analysis_id,
        "as_of":                  as_of,
        "stage":                  stage,
        "ticker":                 action.get("ticker", ""),
        "account":                action.get("execution_account") or action.get("account", ""),
        "tier":                   action.get("tier", ""),
        "canonical_action_type":  atype,
        "direction":              _direction(atype),
        "source":                 action.get("source", ""),
        "urgency":                action.get("urgency", ""),
        "confidence_pct":         action.get("confidence_pct"),
        "eligible":               eligible,
        "eligibility_reason":     eligibility_reason,
        "estimated_notional_jpy": estimated_notional_jpy,
        "filter_rule":            filter_rule,
        "filtered_reason":        (filtered_reason or "")[:300] if filtered_reason else None,
        "scenario_key":           scenario_key,
        "regime":                 regime,
        "actual_dd_stage":        actual_dd_stage,
        "leverage_status":        leverage_status,
        "logged_at":              datetime.now(timezone.utc).isoformat(),
    }
    for key in (
        "execution_account",
        "execution_owner",
        "execution_broker",
        "execution_investment_type",
        "execution_position_keys",
        "execution_readiness",
        "order_intent_decision",
        "non_executable",
        "execution_state",
        "existing_order_id",
        "existing_order_status",
        "existing_order_quantity",
        "existing_order_notional_jpy",
        "recommended_quantity",
        "recommended_notional_jpy",
        "target_quantity",
        "target_notional_jpy",
        "executable_delta_quantity",
        "incremental_notional_jpy",
        "material_change",
        "material_change_reasons",
        "non_executable_reason",
    ):
        if key in action:
            entry[key] = action.get(key)
    if isinstance(action.get("execution_block_reasons"), list):
        entry["execution_block_reason_codes"] = [
            str(reason.get("code"))
            for reason in action.get("execution_block_reasons") or []
            if isinstance(reason, dict) and reason.get("code")
        ]
    return entry


# ── 書き込み ─────────────────────────────────────────────────

def append_entries(entries: list[dict], path: "Path | None" = None) -> None:
    """エントリを append-only JSONL に追記。空リストは no-op。
    path=None のときは呼び出し時点の LOG_PATH を参照する（monkeypatch テスト対応）。
    """
    if not entries:
        return
    _path = path if path is not None else LOG_PATH
    try:
        with open(_path, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        # ログ失敗は分析を止めない
        print(f"[action_stage_log] write error: {e}")


# ── 公開 API ─────────────────────────────────────────────────

def new_analysis_id() -> str:
    """UUID の先頭 8 文字をラン識別子として返す。"""
    return str(uuid.uuid4())[:8]


def log_tier_generated(
    *,
    analysis_id: str,
    as_of: str,
    tier_name: str,
    actions: list[dict],
    scenario_key: Optional[str] = None,
    regime: Optional[str] = None,
    actual_dd_stage: Optional[str] = None,
    leverage_status: Optional[str] = None,
) -> None:
    """各ティア分析が出力したアクションを記録する。"""
    entries = []
    for action in (actions or []):
        if not isinstance(action, dict):
            continue
        a = dict(action)
        if not a.get("tier"):
            a["tier"] = tier_name
        entries.append(_make_entry(
            analysis_id=analysis_id, as_of=as_of, stage="tier_generated", action=a,
            scenario_key=scenario_key, regime=regime,
            actual_dd_stage=actual_dd_stage, leverage_status=leverage_status,
        ))
    append_entries(entries)


def log_opus_raw(
    *,
    analysis_id: str,
    as_of: str,
    actions: list[dict],
    scenario_key: Optional[str] = None,
    regime: Optional[str] = None,
    actual_dd_stage: Optional[str] = None,
    leverage_status: Optional[str] = None,
) -> None:
    """Opus 合成直後・policy gate 前のアクション（raw_priority_actions）を記録する。"""
    entries = []
    for action in (actions or []):
        if not isinstance(action, dict):
            continue
        entries.append(_make_entry(
            analysis_id=analysis_id, as_of=as_of, stage="opus_raw", action=action,
            scenario_key=scenario_key, regime=regime,
            actual_dd_stage=actual_dd_stage, leverage_status=leverage_status,
        ))
    append_entries(entries)


def log_policy_decision(
    *,
    analysis_id: str,
    as_of: str,
    accepted: list[dict],
    rejected: list[dict],
    scenario_key: Optional[str] = None,
    regime: Optional[str] = None,
    actual_dd_stage: Optional[str] = None,
    leverage_status: Optional[str] = None,
) -> None:
    """Policy Engine の accepted / rejected 両方を記録する。"""
    entries = []
    for action in (accepted or []):
        if not isinstance(action, dict):
            continue
        entries.append(_make_entry(
            analysis_id=analysis_id, as_of=as_of, stage="policy_accepted", action=action,
            scenario_key=scenario_key, regime=regime,
            actual_dd_stage=actual_dd_stage, leverage_status=leverage_status,
        ))
    for item in (rejected or []):
        if not isinstance(item, dict):
            continue
        # rejected エントリは {"action": ..., "rule": ..., "reason": ...} の構造
        action = item.get("action") if isinstance(item.get("action"), dict) else item
        entries.append(_make_entry(
            analysis_id=analysis_id, as_of=as_of, stage="policy_rejected", action=action,
            filter_rule=item.get("rule"),
            filtered_reason=item.get("reason"),
            scenario_key=scenario_key, regime=regime,
            actual_dd_stage=actual_dd_stage, leverage_status=leverage_status,
        ))
    append_entries(entries)


def log_post_filter_final(
    *,
    analysis_id: str,
    as_of: str,
    actions: list[dict],
    scenario_key: Optional[str] = None,
    regime: Optional[str] = None,
    actual_dd_stage: Optional[str] = None,
    leverage_status: Optional[str] = None,
) -> None:
    """全後処理通過後の最終 priority_actions を記録する。"""
    entries = []
    for action in (actions or []):
        if not isinstance(action, dict):
            continue
        entries.append(_make_entry(
            analysis_id=analysis_id, as_of=as_of, stage="post_filter_final", action=action,
            scenario_key=scenario_key, regime=regime,
            actual_dd_stage=actual_dd_stage, leverage_status=leverage_status,
        ))
    append_entries(entries)


def log_post_filter_rejected(
    *,
    analysis_id: str,
    as_of: str,
    actions: list[dict],
    scenario_key: Optional[str] = None,
    regime: Optional[str] = None,
    actual_dd_stage: Optional[str] = None,
    leverage_status: Optional[str] = None,
) -> None:
    """post-filter で除外された actions を監査用に記録する。"""
    entries = []
    for action in (actions or []):
        if not isinstance(action, dict):
            continue
        entries.append(_make_entry(
            analysis_id=analysis_id, as_of=as_of, stage="post_filter_rejected", action=action,
            scenario_key=scenario_key, regime=regime,
            actual_dd_stage=actual_dd_stage, leverage_status=leverage_status,
        ))
    append_entries(entries)


def log_post_filter_deferred(
    *,
    analysis_id: str,
    as_of: str,
    actions: list[dict],
    scenario_key: Optional[str] = None,
    regime: Optional[str] = None,
    actual_dd_stage: Optional[str] = None,
    leverage_status: Optional[str] = None,
) -> None:
    """open-order-aware sizing で実行保留された actions を監査用に記録する。"""
    entries = []
    for action in (actions or []):
        if not isinstance(action, dict):
            continue
        entries.append(_make_entry(
            analysis_id=analysis_id, as_of=as_of, stage="post_filter_deferred", action=action,
            scenario_key=scenario_key, regime=regime,
            actual_dd_stage=actual_dd_stage, leverage_status=leverage_status,
        ))
    append_entries(entries)


def log_executed(
    *,
    analysis_id: str,
    as_of: str,
    action: dict,
    estimated_notional_jpy: Optional[float] = None,
    scenario_key: Optional[str] = None,
    regime: Optional[str] = None,
    actual_dd_stage: Optional[str] = None,
    leverage_status: Optional[str] = None,
) -> None:
    """1 件の執行済みアクションを記録する（api/routes/actions.py から呼ぶ）。"""
    if not isinstance(action, dict):
        return
    append_entries([_make_entry(
        analysis_id=analysis_id, as_of=as_of, stage="executed", action=action,
        estimated_notional_jpy=estimated_notional_jpy,
        scenario_key=scenario_key, regime=regime,
        actual_dd_stage=actual_dd_stage, leverage_status=leverage_status,
    )])


# ── 読み込み ─────────────────────────────────────────────────

def read_entries(
    path: "Path | None" = None,
    since_iso: Optional[str] = None,
    stages: Optional[list[str]] = None,
) -> list[dict]:
    """
    JSONL を読み込む。
    since_iso: as_of >= since_iso のエントリのみ（ISO 8601 文字列比較）
    stages: 指定があれば該当ステージのみ
    """
    _path = path if path is not None else LOG_PATH
    if not _path.exists():
        return []
    entries = []
    with open(_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_iso and entry.get("as_of", "") < since_iso:
                continue
            if stages and entry.get("stage") not in stages:
                continue
            entries.append(entry)
    return entries

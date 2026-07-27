"""execution_invalidation.py — 実行無効化の単一権威 (Stage -1)

背景: 未検証の GINN モデル (n_test=0) がボラティリティを最大3倍に膨らませ、
2026-07-27 06:24 の分析 (analysis_id=a64491d6-...) の tier プロンプトへ混入した。
同時に AVGO/XLF の対象ポジションは証券会社との照合が7/14で止まっており、
別銘柄 (LLY) の更新で holdings.json 全体が新しく見える鮮度判定の穴もあった。

この2つの分析結果 (AVGO trim / XLF trim, ともに execution_readiness="ready")
を実行不可へ落とすための overlay。

設計方針 (overlay-only):
  - action_state.json の原レコードは一切書き換えない。監査上の原文として保持する。
  - 無効化は execution_invalidation_state.json という別ファイルにのみ記録する。
  - 消費側 (Today / 注文API / dedup) は resolve_execution_invalidation() を通して
    「有効か」を判定する。raw の action_state.json を直接信用しない。
  - fill 報告 (過去に実際に約定した事実) は無効化後も拒否しない。ただし
    invalidated_at より後に executed したものは post_invalidation_fill として
    区別する (AI 推薦由来として扱わない)。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils import atomic_write_json, process_lock

BASE_DIR = Path(__file__).parent
STATE_PATH = BASE_DIR / "execution_invalidation_state.json"
SCHEMA_VERSION = 1

# Today / 注文API のブロック理由コードとして使う。
REASON_UNVALIDATED_GINN_INPUT = "unvalidated_ginn_input"
REASON_POSITION_FRESHNESS_UNVERIFIED = "position_freshness_unverified"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_hash(record: dict) -> str:
    """action_state.json の原レコードのハッシュ (改変検知用の参考値)。"""
    payload = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    return data


def invalidate_analysis(
    *,
    analysis_id: str,
    action_state_ids: list[str],
    reason_codes: list[str],
    approved_by: str,
    source_incident: str,
    original_records: Optional[dict[str, dict]] = None,
) -> dict:
    """analysis_id (と紐づく action_state_ids) を実行不可として記録する。

    action_state.json 自体は変更しない (overlay-only)。同じ analysis_id を
    複数回 invalidate してもエントリは重複せず、最新の呼び出しで上書きされる
    (承認者・理由の追記のため)。

    Args:
        analysis_id:      無効化する分析の synthesis.analysis_id
        action_state_ids: 紐づく action_state.json 上の id 群
        reason_codes:     REASON_* 定数などの理由コード
        approved_by:      承認者 (人間の識別子。空文字は不可)
        source_incident:  対応するインシデントの説明
        original_records: {action_state_id: 原レコード} — hash 記録用 (任意)
    """
    if not analysis_id:
        raise ValueError("analysis_id is required")
    if not approved_by:
        raise ValueError("approved_by is required — Stage -1 は人間の承認が前提")
    if not reason_codes:
        raise ValueError("reason_codes must not be empty")

    entry = {
        "schema_version": SCHEMA_VERSION,
        "analysis_id": analysis_id,
        "action_state_ids": list(action_state_ids),
        "invalidated_at": _now_iso(),
        "reason_codes": list(reason_codes),
        "approved_by": approved_by,
        "source_incident": source_incident,
        "original_record_hashes": {
            aid: _record_hash(rec)
            for aid, rec in (original_records or {}).items()
        },
    }

    with process_lock("execution_invalidation", timeout=10):
        state = _load_state()
        # 同一 analysis_id の既存エントリは置き換える (追記ではなく更新)。
        state["entries"] = [
            e for e in state["entries"] if e.get("analysis_id") != analysis_id
        ]
        state["entries"].append(entry)
        state["schema_version"] = SCHEMA_VERSION
        state["last_updated"] = _now_iso()
        atomic_write_json(STATE_PATH, state)

    return entry


def resolve_execution_invalidation(
    *,
    analysis_id: Optional[str] = None,
    action_state_id: Optional[str] = None,
) -> Optional[dict]:
    """analysis_id または action_state_id が無効化されていれば、そのエントリを返す。

    これが Today / 注文API / dedup の共通チョークポイント。
    どちらも指定されなければ None (無効化なし扱い)。
    """
    if not analysis_id and not action_state_id:
        return None
    state = _load_state()
    for entry in state.get("entries", []):
        if analysis_id and entry.get("analysis_id") == analysis_id:
            return entry
        if action_state_id and action_state_id in (entry.get("action_state_ids") or []):
            return entry
    return None


def is_invalidated(
    *,
    analysis_id: Optional[str] = None,
    action_state_id: Optional[str] = None,
) -> bool:
    return resolve_execution_invalidation(
        analysis_id=analysis_id, action_state_id=action_state_id
    ) is not None


def classify_fill_timing(entry: dict, executed_at: Optional[str]) -> str:
    """fill 報告が invalidated_at の前か後かを判定する。

    executed_at が無い/不正な場合は安全側 (before) に倒す — fill 事実の
    拒否は絶対にしないという契約のため。
    """
    invalidated_at = entry.get("invalidated_at")
    if not executed_at or not invalidated_at:
        return "post_invalidation_unknown_timing"
    try:
        exec_dt = datetime.fromisoformat(str(executed_at).replace("Z", "+00:00"))
        inv_dt = datetime.fromisoformat(str(invalidated_at).replace("Z", "+00:00"))
        if exec_dt.tzinfo is None:
            exec_dt = exec_dt.replace(tzinfo=timezone.utc)
        if inv_dt.tzinfo is None:
            inv_dt = inv_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "post_invalidation_unknown_timing"
    return "pre_invalidation_fact" if exec_dt <= inv_dt else "post_invalidation_fill"


def invalidation_block_reason(entry: dict) -> dict:
    """Today / 注文API 向けの execution_block_reasons 互換の理由 dict。"""
    codes = entry.get("reason_codes") or [REASON_UNVALIDATED_GINN_INPUT]
    return {
        "code": codes[0],
        "message": (
            "この推奨は無効化されています。証券会社データとの再照合後、"
            "新しい分析での再提案が必要です。"
            f" (incident: {entry.get('source_incident') or 'unspecified'})"
        ),
        "reason_codes": codes,
        "analysis_id": entry.get("analysis_id"),
        "invalidated_at": entry.get("invalidated_at"),
    }

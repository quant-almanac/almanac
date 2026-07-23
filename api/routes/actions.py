"""
POST /api/actions/execute   — 実行記録を保存 + holdings / trade_history に反映
GET  /api/actions/executions — 実行履歴を取得
PATCH /api/actions/status/{action_id} — アクションのステータスを更新
DELETE /api/actions/executions/{exec_id} — 実行記録を削除

P1-4: Pydantic validation + 通貨自動判定（.T/.JP → JPY, 既存 holdings → 既存通貨継承）。
"""
import copy
import csv
import hashlib
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

router = APIRouter()
BASE_DIR      = Path(__file__).parent.parent.parent
EXEC_FILE     = BASE_DIR / "action_executions.json"
HOLDINGS_FILE = BASE_DIR / "holdings.json"
ACCOUNT_FILE  = BASE_DIR / "account.json"
HISTORY_FILE  = BASE_DIR / "trade_history.csv"
ANALYSIS_FILE = BASE_DIR / "ai_portfolio_analysis.json"

sys.path.insert(0, str(BASE_DIR))
from utils import (
    LockBusy,
    atomic_write_json as _save_json,
    load_json_strict as _load_json_strict,
    process_lock,
)
from portfolio_manager import get_fx_rate as _get_fx_rate


# ============================================================
# P0-8: Portfolio snapshot cache invalidation
# 単一 ledger event 適用後に必ず呼ぶ。
# ============================================================
def _invalidate_portfolio_cache() -> None:
    try:
        from api.routes import portfolio as _p
        _p._invalidate_cache()
    except Exception:
        pass


# ============================================================
# P1-4: Pydantic モデル（入力バリデーション）
# ============================================================

class Direction(str, Enum):
    buy        = "buy"
    sell       = "sell"
    hold       = "hold"
    margin_buy = "margin_buy"
    short      = "short"
    cover      = "cover"


class Status(str, Enum):
    executed  = "executed"
    partial   = "partial"
    ordered   = "ordered"
    skip      = "skip"
    cancelled = "cancelled"


class Account(str, Enum):
    tokutei   = "特定"
    ippan     = "一般"
    nisa_g    = "NISA成長投資枠"
    nisa_t    = "NISAつみたて投資枠"
    margin    = "信用"
    employee  = "持株会"


class InvestmentType(str, Enum):
    long   = "long"
    medium = "medium"
    swing  = "swing"


class Currency(str, Enum):
    JPY = "JPY"
    USD = "USD"
    EUR = "EUR"


# 日本株を示す代表的な suffix（JPY 自動判定対象）
_JPY_SUFFIXES = (".T", ".JP", ".JPX", ".OS")


def _auto_detect_currency(ticker: str, holdings: dict) -> Optional[str]:
    """
    ticker + holdings から通貨を自動判定する。
    優先度: 既存 holdings → .T/.JP 等の suffix → None（未判定）
    """
    if not ticker:
        return None
    # 既存 holdings にあれば既存通貨を継承
    key = _find_holding_key(holdings, ticker)
    if key:
        existing = holdings[key].get("currency")
        if existing:
            return existing
    # suffix ベースの推定（日本株のみ自動判定、欧米系は明示要求）
    if ticker.endswith(_JPY_SUFFIXES):
        return "JPY"
    return None


class ExecutionRequest(BaseModel):
    """実行記録の入力スキーマ。API POST /api/actions/execute で受理。"""
    ticker:          str
    direction:       Direction              = Direction.hold
    quantity:        Optional[float]        = None
    price:           Optional[float]        = None
    currency:        Optional[Currency]     = None
    account:         Optional[Account]      = None
    investment_type: InvestmentType         = InvestmentType.medium
    status:          Status                 = Status.executed
    sell_all:        bool                   = False
    name:            Optional[str]          = None
    note:            str                    = ""
    action:          str                    = ""
    # A-8: 執行品質トラッキング用（任意）
    order_type:      Optional[str]          = None    # 'market' | 'limit' | 'stop' | 'stop_limit'
    bid_at_order:    Optional[float]        = None
    ask_at_order:    Optional[float]        = None
    executed_at_time: Optional[str]         = None    # ISO タイムスタンプ（約定時刻）
    # v5.1: AI 指値判断 + Implementation Shortfall
    limit_price:               Optional[float] = None  # 実際に出した指値（ユーザーが上書き可能）
    decision_price:            Optional[float] = None  # AI 提示時 mid（後で shortfall 計算用）
    decision_ts:               Optional[str]   = None  # ISO（AI 提示時刻）
    ai_recommended_order_type: Optional[str]   = None  # AI 推奨 order_type（人間が変えても保持）
    ai_recommended_limit:      Optional[float] = None  # AI 推奨 limit_price
    # AI 推奨から実行した場合、元の分析runとjoinするために保持する（手入力はNone）。
    analysis_id:               Optional[str]   = None
    action_state_id:           Optional[str]   = None
    policy_override_reason:    Optional[str]   = None
    execution_owner:           Optional[str]   = None  # husband | wife
    execution_broker:          Optional[str]   = None  # rakuten | sbi
    execution_position_keys:   Optional[list[str]] = None
    # Explicit funding provenance for a new discretionary purchase.  Historical
    # fills remain recordable without it; only future plan accounting consumes
    # an approved contribution.
    contribution_id:            Optional[str]   = None
    idempotency_key:           str

    @field_validator("ticker")
    @classmethod
    def ticker_non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("ticker は必須です")
        from instrument_metadata import canonical_execution_ticker
        return canonical_execution_ticker(v)

    @field_validator("quantity")
    @classmethod
    def quantity_nonneg(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("quantity は 0 以上の値を指定してください")
        return v

    @field_validator("price")
    @classmethod
    def price_nonneg(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("price は 0 以上の値を指定してください")
        return v

    @field_validator("order_type", "ai_recommended_order_type")
    @classmethod
    def validate_order_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.lower().strip()
        if v not in ("market", "limit", "stop", "stop_limit"):
            raise ValueError("order_type は 'market'/'limit'/'stop'/'stop_limit' のいずれか")
        return v

    @field_validator("bid_at_order", "ask_at_order", "limit_price",
                     "decision_price", "ai_recommended_limit")
    @classmethod
    def validate_quote(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("bid/ask/limit/decision は 0 以上")
        return v

    @field_validator(
        "analysis_id", "action_state_id", "policy_override_reason",
        "execution_owner", "execution_broker", "contribution_id",
    )
    @classmethod
    def normalize_optional_text(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("execution_owner")
    @classmethod
    def validate_execution_owner(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        from execution_safety import canonical_owner

        normalized = canonical_owner(v)
        if not normalized:
            raise ValueError("execution_owner は husband または wife")
        return normalized

    @field_validator("execution_broker")
    @classmethod
    def validate_execution_broker(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        from execution_safety import canonical_broker

        normalized = canonical_broker(v)
        if not normalized:
            raise ValueError("execution_broker は rakuten または sbi")
        return normalized

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, v: str) -> str:
        value = str(v or "").strip()
        if not (8 <= len(value) <= 128) or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
            raise ValueError("idempotency_key は8〜128文字の英数字・._:-で指定してください")
        return value


class StatusPatchRequest(BaseModel):
    status: str
    note:   str = ""

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("placed", "filled", "cancelled"):
            raise ValueError(f"無効なstatus: {v}")
        return v


class PortfolioResolutionRequest(BaseModel):
    resolution: Literal["apply", "externally_reconciled"]
    execution_owner: Optional[str] = None
    execution_broker: Optional[str] = None
    account: Optional[Account] = None
    investment_type: Optional[InvestmentType] = None
    execution_position_key: Optional[str] = None
    external_reconcile_source: Optional[str] = None

    @field_validator("execution_owner")
    @classmethod
    def validate_owner(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        from execution_safety import canonical_owner
        normalized = canonical_owner(v)
        if not normalized:
            raise ValueError("execution_owner は husband または wife")
        return normalized

    @field_validator("execution_broker")
    @classmethod
    def validate_broker(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        from execution_safety import canonical_broker
        normalized = canonical_broker(v)
        if not normalized:
            raise ValueError("execution_broker は rakuten または sbi")
        return normalized

    @field_validator("execution_position_key", "external_reconcile_source")
    @classmethod
    def normalize_resolution_text(cls, v: Optional[str]) -> Optional[str]:
        value = str(v or "").strip()
        return value or None


class PortfolioApplicationPending(Exception):
    """A valid fill that cannot yet be applied to holdings/cash safely."""

    def __init__(self, code: str, message: str, *, candidate_position_keys: Optional[list[str]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidate_position_keys = candidate_position_keys or []


# ============================================================
# 内部ロジック（Pydantic 既定 serialize 後の dict/obj を受ける）
# ============================================================

def _find_holding_key(
    holdings: dict,
    ticker: str,
    account: Optional[str] = None,
    *,
    execution_owner: Optional[str] = None,
    execution_broker: Optional[str] = None,
) -> Optional[str]:
    """Backward-compatible unique match helper. Ambiguous matches return None."""
    matches = _find_holding_matches(
        holdings,
        ticker,
        account=account,
        execution_owner=execution_owner,
        execution_broker=execution_broker,
    )
    return matches[0] if len(matches) == 1 else None


def _find_holding_matches(
    holdings: dict,
    ticker: str,
    account: Optional[str] = None,
    *,
    execution_owner: Optional[str] = None,
    execution_broker: Optional[str] = None,
) -> list[str]:
    """Return every position matching the supplied structured scope."""
    matches: list[str] = []
    for k, v in holdings.items():
        if isinstance(v, dict) and (v.get("ticker", k) == ticker or k == ticker):
            matches.append(k)
    if account:
        matches = [k for k in matches if holdings[k].get("account") == account]
    if execution_owner or execution_broker:
        from execution_safety import canonical_broker, canonical_owner

        routed = []
        for key in matches:
            row = holdings[key]
            row_owner = canonical_owner(row.get("owner"))
            row_broker = canonical_broker(row.get("broker"))
            if execution_owner and row_owner and row_owner != execution_owner:
                continue
            if execution_broker and row_broker != execution_broker:
                continue
            routed.append(key)
        matches = routed
    return matches


def _resolve_holding_scope(
    holdings: dict,
    ticker: str,
    account: Optional[str],
    *,
    execution_owner: Optional[str],
    execution_broker: Optional[str],
    execution_position_keys: Optional[list[str]] = None,
) -> tuple[str, Optional[str], list[str]]:
    """Resolve a holding as none/unique/ambiguous without guessing."""
    requested = [str(k) for k in (execution_position_keys or []) if str(k)]
    if requested:
        valid = []
        for key in requested:
            row = holdings.get(key)
            if not isinstance(row, dict):
                continue
            if str(row.get("ticker") or key) != ticker and key != ticker:
                continue
            if account and row.get("account") != account:
                continue
            if execution_owner or execution_broker:
                from execution_safety import canonical_broker, canonical_owner
                row_owner = canonical_owner(row.get("owner"))
                row_broker = canonical_broker(row.get("broker"))
                if execution_owner and row_owner and row_owner != execution_owner:
                    continue
                if execution_broker and row_broker and row_broker != execution_broker:
                    continue
            valid.append(key)
        if len(valid) == 1:
            return "unique", valid[0], valid
        if len(valid) > 1:
            return "ambiguous", None, valid
        return "none", None, []

    matches = _find_holding_matches(
        holdings,
        ticker,
        account=account,
        execution_owner=execution_owner,
        execution_broker=execution_broker,
    )
    if len(matches) == 1:
        return "unique", matches[0], matches
    if len(matches) > 1:
        return "ambiguous", None, matches
    return "none", None, []


def _holding_key_for_new_position(
    holdings: dict,
    ticker: str,
    account: Optional[str],
    *,
    execution_owner: Optional[str] = None,
    execution_broker: Optional[str] = None,
) -> str:
    if ticker not in holdings:
        return ticker
    suffix_map = {
        "特定": "toku",
        "一般": "ippan",
        "NISA成長投資枠": "NISA",
        "NISAつみたて投資枠": "NISA_T",
        "信用": "margin",
    }
    suffix = suffix_map.get(account or "", str(account or "acct").replace(" ", "_"))
    if execution_owner:
        suffix = f"{suffix}_{execution_owner.upper()}"
    elif execution_broker:
        suffix = f"{suffix}_{execution_broker.upper()}"
    base = f"{ticker}_{suffix}"
    if base not in holdings:
        return base
    i = 2
    while True:
        key = f"{base}_{i}"
        if key not in holdings:
            return key
        i += 1


def _load_required_dict(path: Path, label: str) -> dict:
    """台帳系 JSON は壊れたまま空 dict で続行しない。"""
    try:
        data = _load_json_strict(path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"{label} が存在しません: {path}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{label} の読み込みに失敗: {e}") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail=f"{label} が dict ではありません")
    return data


def _load_execution_log() -> dict:
    """実行ログは初回だけ空で作れるが、既存ファイル破損は止める。"""
    if not EXEC_FILE.exists():
        return {"executions": []}
    data = _load_required_dict(EXEC_FILE, "action_executions.json")
    records = data.get("executions")
    if records is None:
        data["executions"] = []
    elif not isinstance(records, list):
        raise HTTPException(status_code=500, detail="action_executions.json の executions が list ではありません")
    return data


def _is_portfolio_applied(rec: dict) -> bool:
    """旧 portfolio_updated=true も反映済み扱いにする。"""
    return bool(rec.get("portfolio_applied") or rec.get("portfolio_updated"))


def _normalize_execution_record(rec: dict) -> dict:
    """旧レコードに portfolio_applied を補完し、PATCH 再反映を防ぐ。"""
    if "portfolio_applied" not in rec:
        rec["portfolio_applied"] = bool(rec.get("portfolio_updated"))
    if rec.get("portfolio_applied") and "portfolio_updated" not in rec:
        rec["portfolio_updated"] = True
    return rec


def _make_execution_event_id(exec_id: Optional[str]) -> Optional[str]:
    if not exec_id:
        return None
    return f"exec_{exec_id}"


def _execution_request_hash(req: ExecutionRequest) -> str:
    payload = req.model_dump(mode="json", exclude={"idempotency_key"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _execution_id_from_key(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"execution_{digest}"


def _execution_record_by_id(exec_id: str) -> Optional[dict]:
    data = _load_execution_log()
    for raw in data.get("executions", []):
        if isinstance(raw, dict) and raw.get("id") == exec_id:
            return _normalize_execution_record(raw)
    return None


def _response_for_execution_record(record: dict, *, idempotent_replay: bool) -> dict:
    portfolio = {
        "updated": bool(record.get("portfolio_applied")),
        "message": record.get("portfolio_message") or "約定事実を保存済み",
        "realized_pnl_jpy": record.get("realized_pnl_jpy"),
        "cash_delta": record.get("cash_delta"),
        "cash_currency": record.get("cash_currency"),
        "cash_route": record.get("cash_route"),
        "event_id": record.get("event_id"),
        "margin_position_id": record.get("margin_position_id"),
        "margin_closed_position_ids": record.get("margin_closed_position_ids"),
        "margin_side": record.get("margin_side"),
        "position_key": record.get("position_key"),
    }
    return {
        "ok": True,
        "id": record.get("id"),
        "portfolio": portfolio,
        "action_state_id": record.get("action_state_id"),
        "portfolio_application_status": record.get("portfolio_application_status") or (
            "applied" if record.get("portfolio_applied") else "not_applicable"
        ),
        "portfolio_application_reasons": record.get("portfolio_application_reasons") or [],
        "candidate_position_keys": record.get("candidate_position_keys") or [],
        "idempotent_replay": idempotent_replay,
    }


def _sync_action_state_for_execution(
    *,
    ticker: str,
    direction: str,
    execution_status: str,
    note: str = "",
    action_state_id: Optional[str] = None,
    execution_owner: Optional[str] = None,
    execution_broker: Optional[str] = None,
    execution_account: Optional[str] = None,
    execution_investment_type: Optional[str] = None,
    execution_position_keys: Optional[list[str]] = None,
) -> Optional[str]:
    """execution log と action_state の未完了アクションを同期する。"""
    try:
        from action_state_tracker import sync_execution_status

        return sync_execution_status(
            ticker=ticker,
            direction=direction,
            execution_status=execution_status,
            note=note,
            action_state_id=action_state_id,
            execution_owner=execution_owner,
            execution_broker=execution_broker,
            execution_account=execution_account,
            execution_investment_type=execution_investment_type,
            execution_position_keys=execution_position_keys,
        )
    except Exception as e:
        print(f"[action_state] sync skip: {e}")
        return None


def _validate_action_state_link(req: ExecutionRequest) -> None:
    """Fail loudly when an explicit AI recommendation link is inconsistent."""
    if not req.action_state_id:
        return
    state_path = BASE_DIR / "action_state.json"
    try:
        state = _load_json_strict(state_path)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"action_state link を検証できません: {exc}") from exc
    actions = state.get("actions") if isinstance(state, dict) else None
    entry = actions.get(req.action_state_id) if isinstance(actions, dict) else None
    if not isinstance(entry, dict):
        raise HTTPException(status_code=422, detail=f"action_state_id '{req.action_state_id}' が見つかりません")
    if str(entry.get("ticker") or "") != req.ticker:
        raise HTTPException(status_code=422, detail="action_state_id と ticker が一致しません")
    try:
        from action_state_tracker import normalize_action_type
        expected = normalize_action_type(entry.get("action_type") or entry.get("type"))
        actual = normalize_action_type(req.direction.value)
    except Exception:
        expected = actual = ""
    if expected and actual and expected != actual:
        raise HTTPException(status_code=422, detail="action_state_id と direction が一致しません")


def _linked_ai_readiness_values(
    *,
    analysis_id: str | None,
    action_state_id: str | None,
    ticker: str,
    direction: str,
) -> tuple[str | None, list[dict]]:
    """Return the persisted readiness for an explicitly AI-linked execution.

    The latest analysis is preferred, while action_state is the durable fallback
    for an older recommendation.  Missing readiness on an explicit link is
    treated as unknown (fail closed for a new order, never for a reported fill).
    """
    if not analysis_id and not action_state_id:
        return None, []

    state_entry: dict = {}
    if action_state_id:
        try:
            state = _load_json_strict(BASE_DIR / "action_state.json")
            candidate = (state.get("actions") or {}).get(action_state_id)
            if isinstance(candidate, dict):
                state_entry = candidate
        except Exception:
            pass

    matches: list[dict] = []
    try:
        analysis = _load_json_strict(ANALYSIS_FILE)
        synthesis = analysis.get("synthesis") if isinstance(analysis, dict) else None
        current_id = str((synthesis or {}).get("analysis_id") or "")
        if isinstance(synthesis, dict) and (not analysis_id or current_id == analysis_id):
            from action_state_tracker import normalize_action_type
            requested_direction = normalize_action_type(direction)
            for action in synthesis.get("priority_actions") or []:
                if not isinstance(action, dict) or str(action.get("ticker") or "") != ticker:
                    continue
                if normalize_action_type(action.get("type") or action.get("action_type")) != requested_direction:
                    continue
                matches.append(action)
    except Exception:
        pass

    source = matches[0] if matches else state_entry
    readiness = str(source.get("execution_readiness") or "").strip().lower() or "unknown"
    raw_reasons = source.get("execution_block_reasons") or []
    reasons = [r for r in raw_reasons if isinstance(r, dict)]
    # A reported fill remains a fact even when this guard is stale, but an
    # ``ordered`` request must never resurrect a holiday/reprice candidate or
    # an elapsed limit simply because action-state cleanup has not run yet.
    state_status = str(state_entry.get("status") or "").lower()
    if state_status == "reprice_required" or state_entry.get("market_reprice_required"):
        readiness = "review"
        reasons.append({
            "code": "market_closed_reprice_required",
            "message": "休場日に生成された候補です。次の取引セッションで再分析・再価格設定が必要です",
        })
    elif state_status == "pending" and not state_entry.get("expiry_deferred_until_reprice"):
        from execution_safety import execution_expiry_at

        expires_at = execution_expiry_at(state_entry)
        if expires_at is not None and expires_at <= datetime.now(expires_at.tzinfo):
            readiness = "review"
            reasons.append({
                "code": "order_expired",
                "message": "推奨時の有効期限を過ぎています。新しい価格で再分析が必要です",
            })
    if readiness == "unknown" and not reasons:
        reasons = [{
            "code": "execution_readiness_unknown",
            "message": "AI推奨の実行可否を検証できません",
        }]
    return readiness, reasons


def _linked_ai_readiness(req: ExecutionRequest) -> tuple[str | None, list[dict]]:
    return _linked_ai_readiness_values(
        analysis_id=req.analysis_id,
        action_state_id=req.action_state_id,
        ticker=req.ticker,
        direction=req.direction.value,
    )


def _linked_ai_action(req: ExecutionRequest) -> dict:
    """Return the persisted action used to carry deterministic routing."""
    if req.action_state_id:
        try:
            state = _load_json_strict(BASE_DIR / "action_state.json")
            entry = (state.get("actions") or {}).get(req.action_state_id)
            if isinstance(entry, dict):
                return entry
        except Exception:
            pass
    if req.analysis_id:
        try:
            analysis = _load_json_strict(ANALYSIS_FILE)
            synthesis = analysis.get("synthesis") if isinstance(analysis, dict) else None
            if isinstance(synthesis, dict) and str(synthesis.get("analysis_id") or "") == req.analysis_id:
                from action_state_tracker import normalize_action_type

                requested = normalize_action_type(req.direction.value)
                for action in synthesis.get("priority_actions") or []:
                    if not isinstance(action, dict) or str(action.get("ticker") or "") != req.ticker:
                        continue
                    if normalize_action_type(action.get("type") or action.get("action_type")) == requested:
                        return action
        except Exception:
            pass
    return {}


def _snapshot_execution_plan_metadata(req: ExecutionRequest) -> dict:
    """Persist immutable plan metadata from the linked recommendation.

    ``compute_monthly_consumption`` can currently follow an action_state link,
    but execution records are retained independently.  Snapshotting the exact
    identifiers prevents future state cleanup from erasing the audit trail and
    never attempts ticker/account/sector inference.
    """
    linked = _linked_ai_action(req)
    if not isinstance(linked, dict):
        return {}
    fields = (
        "plan_item_id",
        "monthly_objective_id",
        "execution_plan_decision",
        "execution_plan_override",
        "execution_plan_match_kind",
    )
    return {
        field: linked.get(field)
        for field in fields
        if linked.get(field) not in (None, "")
    }


def _validate_contribution_link(req: ExecutionRequest) -> None:
    """Validate an explicit contribution id without inferring one.

    A reported fill is still saved when no id is supplied.  An id that no
    longer exists or contradicts the owner/broker route is a user-input error,
    not something the API should silently remap.
    """
    if not req.contribution_id:
        return
    try:
        from contribution_ledger import load_ledger

        ledger = load_ledger(BASE_DIR / "contribution_ledger.json")
        matches = [
            row for row in ledger.get("contributions", [])
            if isinstance(row, dict)
            and str(row.get("id") or "") == req.contribution_id
            and str(row.get("status") or "approved") == "approved"
        ]
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"contribution_id を検証できません: {exc}") from exc
    if len(matches) != 1:
        raise HTTPException(status_code=422, detail="contribution_id が見つからないか利用停止です")
    # A contribution is account-scoped capital, not a portfolio-wide wallet.
    # Do not guess a route from a same-ticker holding: that could debit a
    # husband account while consuming a wife contribution (or the reverse).
    if not req.execution_owner or not req.execution_broker:
        raise HTTPException(
            status_code=422,
            detail="contribution_id を使う約定には execution_owner と execution_broker が必要です",
        )
    source = matches[0]
    for field, supplied in (("owner", req.execution_owner), ("broker", req.execution_broker)):
        expected = str(source.get(field) or "")
        if expected and str(supplied) != expected:
            raise HTTPException(status_code=422, detail=f"contribution_id と execution_{field} が一致しません")


def _enforce_ai_order_readiness(req: ExecutionRequest) -> tuple[str | None, list[dict]]:
    """Block only creation of a linked open order; fills remain historical facts."""
    linked = _linked_ai_action(req)
    route_checks = (
        ("execution_owner", req.execution_owner),
        ("execution_broker", req.execution_broker),
    )
    for field, supplied in route_checks:
        expected = linked.get(field)
        if supplied and expected and str(supplied) != str(expected):
            raise HTTPException(status_code=422, detail=f"action_state_id と {field} が一致しません")
    if req.execution_position_keys and linked.get("execution_position_keys"):
        if sorted(req.execution_position_keys) != sorted(str(k) for k in linked.get("execution_position_keys") or []):
            raise HTTPException(status_code=422, detail="action_state_id と execution_position_keys が一致しません")
    if req.account is not None and linked.get("execution_account"):
        if req.account.value != str(linked.get("execution_account")):
            raise HTTPException(status_code=422, detail="action_state_id と execution_account が一致しません")
    if not req.execution_owner and linked.get("execution_owner"):
        req.execution_owner = str(linked.get("execution_owner"))
    if not req.execution_broker and linked.get("execution_broker"):
        req.execution_broker = str(linked.get("execution_broker"))
    if not req.execution_position_keys and linked.get("execution_position_keys"):
        req.execution_position_keys = [str(key) for key in linked.get("execution_position_keys") or []]
    if req.account is None and linked.get("execution_account"):
        try:
            req.account = Account(str(linked.get("execution_account")))
        except ValueError:
            pass
    if req.status == Status.ordered and linked:
        from execution_safety import evaluate_exit_route_consistency

        route_result = evaluate_exit_route_consistency(linked, base_dir=BASE_DIR)
        if route_result.get("readiness") != "ready":
            route_reasons = [
                row for row in (route_result.get("reasons") or [])
                if isinstance(row, dict)
            ]
            raise HTTPException(
                status_code=409,
                detail={
                    "code": (
                        route_reasons[0].get("code")
                        if route_reasons
                        else "execution_route_text_unresolved"
                    ),
                    "message": "AI推奨の説明文と構造化ルートを照合できないため発注できません",
                    "reasons": route_reasons,
                },
            )
    readiness, reasons = _linked_ai_readiness(req)
    if req.status == Status.ordered and readiness is not None and readiness != "ready":
        codes = ", ".join(str(r.get("code") or "unknown") for r in reasons)
        raise HTTPException(
            status_code=409,
            detail=f"AI推奨は発注可能状態ではありません (readiness={readiness}; {codes})",
        )
    account_value = req.account.value if req.account else ""
    if req.status == Status.ordered and "NISA" in account_value:
        if not req.execution_owner or not req.execution_broker:
            raise HTTPException(
                status_code=409,
                detail="NISAのAI連動注文にはexecution_ownerとexecution_brokerが必要です",
            )
    return readiness, reasons


def _enforce_discretionary_order_funding(req: ExecutionRequest) -> None:
    """Block new buy orders without approved funding; never reject fill facts."""
    if req.status != Status.ordered:
        return
    from discretionary_funding import evaluate_discretionary_funding, load_execution_plan_state

    decision = evaluate_discretionary_funding(
        req.direction.value,
        plan_state=load_execution_plan_state(BASE_DIR),
    )
    if decision.get("required") and not decision.get("allowed"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": decision.get("reason_code") or "discretionary_funding_unresolved",
                "message": decision.get("message") or "裁量投資枠を確認できません",
            },
        )


def _enforce_ordered_exit_inventory(req: ExecutionRequest) -> None:
    """Validate a new sell order against one exact holdings account.

    Reported fills remain facts and are handled by the portfolio-application
    pending flow.  This guard applies only before a new open sell order is
    recorded, including old recommendations whose persisted readiness predates
    the account-scoped quantity check.
    """
    if req.status != Status.ordered or req.direction != Direction.sell:
        return
    holdings = _load_required_dict(HOLDINGS_FILE, "holdings.json")
    account = req.account.value if req.account is not None else None
    scope_status, key, candidates = _resolve_holding_scope(
        holdings,
        req.ticker,
        account,
        execution_owner=req.execution_owner,
        execution_broker=req.execution_broker,
        execution_position_keys=req.execution_position_keys,
    )
    if scope_status != "unique" or key is None:
        code = "holding_scope_ambiguous" if scope_status == "ambiguous" else "holding_scope_unresolved"
        raise HTTPException(
            status_code=409,
            detail={
                "code": code,
                "message": "売却元の保有口座を一意に確認できません",
                "candidate_position_keys": candidates,
            },
        )
    try:
        available = float((holdings.get(key) or {}).get("shares"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "holding_quantity_unresolved",
                "message": "指定口座の保有数量を確認できません",
                "execution_position_key": key,
            },
        )
    if available <= 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "holding_quantity_unresolved",
                "message": "指定口座に売却可能な保有数量がありません",
                "available_quantity": available,
                "execution_position_key": key,
            },
        )
    requested = available if req.sell_all and req.quantity is None else float(req.quantity or 0)
    if requested <= 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "holding_quantity_unresolved",
                "message": "売却数量を確認できません",
                "available_quantity": available,
            },
        )
    if requested > available + 1e-9:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "holding_quantity_exceeds_account",
                "message": f"指定口座の保有{available:g}株に対し{requested:g}株の売却はできません",
                "requested_quantity": requested,
                "available_quantity": available,
                "shortfall_quantity": round(requested - available, 8),
                "execution_account": account,
                "execution_position_key": key,
            },
        )


def _ledger_event_exists(event_id: Optional[str]) -> bool:
    """execution event_id が既に ledger にあるなら再適用しない。"""
    if not event_id:
        return False
    try:
        import event_ledger
        event_ledger.init_schema()
        with sqlite3.connect(str(event_ledger.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT 1 FROM ledger_events WHERE event_id = ? LIMIT 1",
                (event_id,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _record_trade_event(
    *,
    event_id: Optional[str],
    direction: str,
    ticker: str,
    price: Optional[float],
    quantity: Optional[float],
    currency: Optional[str],
    account: Optional[str],
    pnl_jpy: Optional[float] = None,
    execution_owner: Optional[str] = None,
    execution_broker: Optional[str] = None,
    cash_route: Optional[str] = None,
    execution_position_key: Optional[str] = None,
) -> None:
    """
    P1-18-B: 約定 event を event_ledger に append する。
    台帳系なので fail-loud。event_id は execution id 由来で固定し、再処理を idempotent にする。
    """
    if price is None or quantity is None or float(quantity) <= 0:
        raise ValueError(
            f"{ticker} {direction} の trade event には price と正の quantity が必須です "
            f"(price={price!r}, quantity={quantity!r})"
        )
    from event_ledger import append_event

    fx = None
    if currency and currency.upper() == "USD":
        from utils import get_fx_rate_cached
        fx, _src = get_fx_rate_cached()
        if fx is None or float(fx) <= 0:
            raise RuntimeError(f"{ticker} の USD trade event に必要な FX レートが取得できません")

    append_event(
        event_type="trade",
        ticker=ticker,
        direction=direction,
        quantity=float(quantity),
        price=float(price),
        currency=(currency or "JPY").upper(),
        fx_rate_usdjpy=fx,
        account=account,
        source="api",
        note=(f"realized_pnl_jpy={pnl_jpy:.0f}" if pnl_jpy is not None else None),
        raw_payload={
            "execution_owner": execution_owner,
            "execution_broker": execution_broker,
            "cash_route": cash_route,
            "execution_position_key": execution_position_key,
        },
        event_id=event_id,
    )


def _append_trade(direction: str, ticker: str, price: Optional[float],
                  quantity: Optional[float], pnl_pct: Optional[float],
                  pnl_jpy: Optional[float]) -> None:
    action_str = {
        "buy": "BUY",
        "sell": "SELL",
        "margin_buy": "MARGIN_BUY",
        "short": "SHORT",
        "cover": "COVER",
    }.get(direction, direction.upper())
    pnl_pct_str = f"{pnl_pct:.1f}%" if pnl_pct is not None else ""
    pnl_jpy_str = f"¥{pnl_jpy:,.0f}" if pnl_jpy is not None else ""
    with open(HISTORY_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            action_str,
            ticker,
            price if price is not None else "",
            quantity if quantity is not None else "",
            pnl_pct_str,
            pnl_jpy_str,
        ])


# ============================================================
# P0-8: Ledger event の単一エントリーポイント
#
# 本関数は約定確定（status=executed）に伴う以下の更新を 1 つの呼び出しで実行する:
#   1. holdings.json — BUY/SELL の数量・取得単価
#   2. account.json  — 通貨別 cash ±（信用 / 持株会 は対象外）
#   3. trade_history.csv — 監査ログ append
#   4. portfolio snapshot cache 無効化
#
# 旧実装の課題:
#   * SELL 超過が黙って全売却扱いになっていた（P0-3）
#   * BUY/SELL で現金残高が更新されず holdings と account が乖離していた（P0-4）
#   * ordered → executed PATCH 遷移で何も反映されない死蔵があった（P0-8 旧称 T1）
#
# 呼出し側は完了後に record['portfolio_applied']=True を立てて idempotency を保証する。
# ============================================================

# 内部口座（証券口座外）— cash 変動の対象外
_CASH_EXEMPT_ACCOUNTS = {"信用", "持株会"}
_MARGIN_ACCOUNTS = {"信用", "信用口座", None}
_FILL_STATUSES = {"executed", "partial", "filled", "done"}
_MARGIN_OPEN_DIRECTIONS = {"margin_buy", "short"}
_MARGIN_CLOSE_DIRECTIONS = {"cover"}


def _normalize_margin_account(account: Optional[str]) -> str:
    if account in _MARGIN_ACCOUNTS:
        return "信用"
    raise HTTPException(
        status_code=422,
        detail=f"信用取引は account='信用' で記録してください (account={account!r})",
    )


def _default_margin_positions_state() -> dict:
    return {
        "cash_collateral": 0,
        "securities_collateral": 0,
        "sec_haircut": 0.80,
        "positions": [],
        "updated": "",
    }


def _load_margin_positions_state() -> dict:
    try:
        import margin_manager

        data = margin_manager._load_positions()
    except Exception:
        data = _default_margin_positions_state()
    if not isinstance(data, dict):
        data = _default_margin_positions_state()
    if not isinstance(data.get("positions"), list):
        data["positions"] = []
    data.setdefault("cash_collateral", 0)
    data.setdefault("securities_collateral", 0)
    data.setdefault("sec_haircut", 0.80)
    data.setdefault("updated", "")
    return data


def _save_margin_positions_state(data: dict) -> None:
    import margin_manager

    margin_manager._save_positions(data)


def _next_margin_position_id(data: dict) -> int:
    ids = []
    for pos in data.get("positions", []):
        try:
            ids.append(int(pos.get("id")))
        except Exception:
            continue
    return (max(ids) + 1) if ids else 1


def _open_margin_position(
    *,
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    currency: str,
    account: Optional[str],
    investment_type: str,
    event_id: Optional[str],
    name: Optional[str],
) -> tuple[dict, dict]:
    if side not in {"long", "short"}:
        raise HTTPException(status_code=422, detail=f"未知の信用建玉 side={side!r}")
    data = _load_margin_positions_state()
    pos_id = _next_margin_position_id(data)
    opened = date.today()
    pos = {
        "id": pos_id,
        "ticker": ticker,
        "name": name or ticker,
        "side": side,
        "shares": round(float(quantity), 6),
        "entry_price": float(price),
        "current_price": float(price),
        "currency": (currency or "JPY").upper(),
        "account": _normalize_margin_account(account),
        "investment_type": investment_type,
        "position_type": "一般信用",
        "opened": opened.isoformat(),
        "expiry": (opened + timedelta(days=180)).isoformat(),
        "memo": f"execution_event_id={event_id}" if event_id else "",
        "source_event_id": event_id,
        "closed": False,
    }
    data["positions"].append(pos)
    return data, pos


def _close_short_positions(
    *,
    ticker: str,
    quantity: Optional[float],
    price: float,
    currency: Optional[str],
    sell_all: bool,
    event_id: Optional[str],
) -> tuple[dict, float, float, list[int], str]:
    data = _load_margin_positions_state()
    open_shorts = [
        p for p in data.get("positions", [])
        if not p.get("closed")
        and str(p.get("side") or "").lower() == "short"
        and str(p.get("ticker") or "") == ticker
    ]
    if not open_shorts:
        raise HTTPException(status_code=422, detail=f"{ticker} の未決済空売り建玉が見つかりません")

    total_open = sum(float(p.get("shares") or 0) for p in open_shorts)
    if sell_all:
        cover_qty = total_open if quantity is None else min(float(quantity), total_open)
    elif quantity is not None:
        cover_qty = float(quantity)
    else:
        raise HTTPException(status_code=422, detail="返済買い数量を入力してください（全返済なら sell_all=true）")
    if cover_qty <= 0:
        raise HTTPException(status_code=422, detail="返済買い quantity は正の値にしてください")
    if cover_qty > total_open + 1e-9:
        raise HTTPException(
            status_code=422,
            detail=f"{ticker} の返済数量 {cover_qty:g} が空売り建玉 {total_open:g} を超えています",
        )

    eff_currency = (currency or open_shorts[0].get("currency") or "JPY").upper()
    for pos in open_shorts:
        pos_currency = str(pos.get("currency") or eff_currency).upper()
        if pos_currency != eff_currency:
            raise HTTPException(
                status_code=422,
                detail=f"{ticker} の空売り建玉通貨が混在しています ({eff_currency}/{pos_currency})",
            )

    remaining = cover_qty
    realized_pnl_jpy = 0.0
    closed_ids: list[int] = []
    fx = _get_fx_rate() if eff_currency == "USD" else 1.0
    closed_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    for pos in open_shorts:
        if remaining <= 1e-9:
            break
        pos_shares = float(pos.get("shares") or 0)
        use_qty = min(pos_shares, remaining)
        entry = float(pos.get("entry_price") or 0)
        pnl_raw = (entry - float(price)) * use_qty
        pnl_jpy = pnl_raw * fx
        realized_pnl_jpy += pnl_jpy
        remaining -= use_qty

        if use_qty >= pos_shares - 1e-9:
            pos["closed"] = True
            pos["close_price"] = float(price)
            pos["closed_at"] = closed_at
            pos["close_event_id"] = event_id
            pos["realized_pnl_jpy"] = round(pnl_jpy, 0)
            closed_ids.append(int(pos.get("id")))
        else:
            pos["shares"] = round(pos_shares - use_qty, 6)
            pos["partial_closed_shares"] = round(float(pos.get("partial_closed_shares") or 0) + use_qty, 6)
            pos["last_cover_price"] = float(price)
            pos["last_cover_event_id"] = event_id
            closed_fragment = {
                **copy.deepcopy(pos),
                "id": _next_margin_position_id(data),
                "shares": round(use_qty, 6),
                "closed": True,
                "close_price": float(price),
                "closed_at": closed_at,
                "close_event_id": event_id,
                "realized_pnl_jpy": round(pnl_jpy, 0),
                "source_open_position_id": pos.get("id"),
                "memo": f"partial cover from position {pos.get('id')} event_id={event_id}",
            }
            data["positions"].append(closed_fragment)
            closed_ids.append(int(closed_fragment["id"]))

    return data, cover_qty, realized_pnl_jpy, closed_ids, eff_currency


def _validate_fill_trade_inputs(
    *,
    ticker: str,
    direction: str,
    status: str,
    quantity: Optional[float],
    price: Optional[float],
    sell_all: bool,
) -> None:
    """
    executed/partial の trade は holdings/cash または margin_positions と event_ledger を同時に動かす。
    price や quantity が欠けたまま通すと、保有株数だけが変わり現金と ledger が欠落する。
    """
    st = (status or "").lower()
    dr = (direction or "").lower()
    if st in {"skip", "cancelled", "ordered"} or dr == "hold":
        return
    if dr not in {"buy", "sell", "margin_buy", "short", "cover"}:
        return
    if st not in _FILL_STATUSES:
        return

    if price is None or float(price) <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"{ticker} {dr} を約定済みにするには約定価格 price が必須です",
        )
    if dr in {"buy", "margin_buy", "short"} and (quantity is None or float(quantity) <= 0):
        raise HTTPException(
            status_code=422,
            detail=f"{ticker} {dr} を約定済みにするには正の quantity が必須です",
        )
    if dr == "sell":
        if not sell_all and (quantity is None or float(quantity) <= 0):
            raise HTTPException(
                status_code=422,
                detail="売却数量を入力してください（全売却の場合は sell_all: true を指定）",
            )
        if sell_all and quantity is not None and float(quantity) <= 0:
            raise HTTPException(
                status_code=422,
                detail="sell_all=true の場合、quantity は省略するか正の値にしてください",
            )
    if dr == "cover":
        if not sell_all and (quantity is None or float(quantity) <= 0):
            raise HTTPException(
                status_code=422,
                detail="返済買い数量を入力してください（全返済の場合は sell_all: true を指定）",
            )
        if sell_all and quantity is not None and float(quantity) <= 0:
            raise HTTPException(
                status_code=422,
                detail="cover の sell_all=true では quantity は省略するか正の値にしてください",
            )


def _apply_event_to_ledger(
    *,
    event_id: Optional[str],
    ticker: str,
    direction: str,
    quantity: Optional[float],
    price: Optional[float],
    currency: Optional[str],
    account: Optional[str],
    investment_type: str,
    status: str,
    sell_all: bool,
    name: Optional[str],
    execution_owner: Optional[str] = None,
    execution_broker: Optional[str] = None,
    execution_position_keys: Optional[list[str]] = None,
) -> dict:
    """
    Idempotent ledger event posting の core。
    Returns: {"updated": bool, "message": str, "realized_pnl_jpy": Optional[float],
              "cash_delta": Optional[float], "cash_currency": Optional[str]}
    """
    no_op = {"updated": False, "message": "変更なし",
             "realized_pnl_jpy": None, "cash_delta": None, "cash_currency": None}

    if direction == "hold" or status in ("skip", "cancelled", "ordered"):
        return no_op
    recovered = _recover_prepared_portfolio_application(event_id)
    if recovered is not None:
        return recovered
    if _ledger_event_exists(event_id):
        return {
            **no_op,
            "updated": True,
            "message": "既に event_ledger に反映済み（再適用をスキップ）",
            "event_id": event_id,
            "already_applied": True,
        }

    _validate_fill_trade_inputs(
        ticker=ticker,
        direction=direction,
        status=status,
        quantity=quantity,
        price=price,
        sell_all=sell_all,
    )

    # ── MARGIN OPEN / COVER ──────────────────────────────────
    # 信用買い・空売り・返済買いは現物 holdings ではなく margin_positions.json に記録する。
    # 通常 sell と混ぜると「新規空売り」が「保有株の売却」に潰れるため、先に分岐する。
    if direction in _MARGIN_OPEN_DIRECTIONS:
        eff_currency = (currency or ("JPY" if ticker.endswith(_JPY_SUFFIXES) else None))
        if not eff_currency:
            raise HTTPException(
                status_code=422,
                detail=f"{ticker} の信用取引には currency を明示してください（.T/.JP は自動判定）",
            )
        eff_currency = eff_currency.upper()
        eff_account = _normalize_margin_account(account)
        margin_before = _load_margin_positions_state()
        side = "long" if direction == "margin_buy" else "short"
        margin_after, pos = _open_margin_position(
            ticker=ticker,
            side=side,
            quantity=float(quantity or 0),
            price=float(price or 0),
            currency=eff_currency,
            account=eff_account,
            investment_type=investment_type,
            event_id=event_id,
            name=name,
        )
        try:
            _save_margin_positions_state(margin_after)
            _record_trade_event(
                event_id=event_id,
                direction=direction,
                ticker=ticker,
                price=price,
                quantity=quantity,
                currency=eff_currency,
                account=eff_account,
                execution_owner=execution_owner,
                execution_broker=execution_broker,
                cash_route=None,
                execution_position_key=(execution_position_keys or [None])[0],
            )
        except Exception as e:
            _save_margin_positions_state(margin_before)
            raise e
        try:
            _append_trade(direction, ticker, price, quantity, None, None)
        except Exception as e:
            print(f"[trade_history] append skip: {e}")
        _invalidate_portfolio_cache()
        label = "信用買い建玉" if direction == "margin_buy" else "空売り建玉"
        return {
            "updated": True,
            "message": f"{ticker}: {label} {float(quantity or 0):g} 株 @ {float(price or 0):g} を追加",
            "realized_pnl_jpy": None,
            "cash_delta": None,
            "cash_currency": None,
            "event_id": event_id,
            "margin_position_id": pos.get("id"),
            "margin_side": side,
        }

    if direction in _MARGIN_CLOSE_DIRECTIONS:
        margin_before = _load_margin_positions_state()
        margin_after, cover_qty, realized_pnl_jpy, closed_ids, eff_currency = _close_short_positions(
            ticker=ticker,
            quantity=quantity,
            price=float(price or 0),
            currency=currency,
            sell_all=sell_all,
            event_id=event_id,
        )
        eff_account = _normalize_margin_account(account)
        try:
            _save_margin_positions_state(margin_after)
            _record_trade_event(
                event_id=event_id,
                direction=direction,
                ticker=ticker,
                price=price,
                quantity=cover_qty,
                currency=eff_currency,
                account=eff_account,
                pnl_jpy=realized_pnl_jpy,
                execution_owner=execution_owner,
                execution_broker=execution_broker,
                cash_route=None,
                execution_position_key=(execution_position_keys or [None])[0],
            )
        except Exception as e:
            _save_margin_positions_state(margin_before)
            raise e
        try:
            _append_trade(direction, ticker, price, cover_qty, None, realized_pnl_jpy)
        except Exception as e:
            print(f"[trade_history] append skip: {e}")
        _invalidate_portfolio_cache()
        return {
            "updated": True,
            "message": f"{ticker}: 空売り {cover_qty:g} 株を返済買い",
            "realized_pnl_jpy": round(realized_pnl_jpy, 0),
            "cash_delta": None,
            "cash_currency": eff_currency,
            # Codex round4 #4: cover/sell_all は cover_qty を内部算出するため、
            # executed notional 用に実約定数量を返す (caller の quantity は None になり得る)。
            "applied_quantity": cover_qty,
            "applied_price": (float(price) if price is not None else None),
            "event_id": event_id,
            "margin_closed_position_ids": closed_ids,
            "margin_side": "short",
        }

    holdings = _load_required_dict(HOLDINGS_FILE, "holdings.json")
    account_state = _load_required_dict(ACCOUNT_FILE, "account.json")
    original_holdings = copy.deepcopy(holdings)
    original_account = copy.deepcopy(account_state)
    scope_status, key, candidate_position_keys = _resolve_holding_scope(
        holdings,
        ticker,
        account=account,
        execution_owner=execution_owner,
        execution_broker=execution_broker,
        execution_position_keys=execution_position_keys,
    )
    if scope_status == "ambiguous":
        raise PortfolioApplicationPending(
            "holding_scope_ambiguous",
            f"{ticker} の保有候補が複数あり、適用先を一意に決められません",
            candidate_position_keys=candidate_position_keys,
        )
    pnl_pct  = None
    pnl_jpy  = None

    # ── BUY ──────────────────────────────────────────────
    if direction == "buy":
        if key:
            pos        = holdings[key]
            if execution_owner and not pos.get("owner"):
                pos["owner"] = execution_owner
            if execution_broker and not pos.get("broker"):
                pos["broker"] = {
                    "rakuten": "楽天証券",
                    "sbi": "SBI証券（妻）" if execution_owner == "wife" else "SBI証券",
                }.get(execution_broker, execution_broker)
            old_shares = float(pos.get("shares", 0))
            old_price  = float(pos.get("entry_price", price or 0))
            add_shares = float(quantity or 0)

            if add_shares > 0 and price:
                new_shares = old_shares + add_shares
                new_avg    = (old_price * old_shares + price * add_shares) / new_shares
                pos["shares"]      = round(new_shares, 6)
                pos["entry_price"] = round(new_avg, 4)
                msg = f"{ticker}: {old_shares} → {pos['shares']} 株、平均取得単価 {pos['entry_price']}"
            else:
                msg = f"{ticker}: 数量/価格が未入力のため株数のみ記録"

            # 既存ポジションの通貨・口座を継承（cash 計算で使う）
            eff_currency = pos.get("currency") or currency
            eff_account  = pos.get("account") or account or "特定"
            route_owner = execution_owner
            route_broker = execution_broker
            if not route_owner or not route_broker:
                from execution_safety import canonical_broker, canonical_owner
                route_owner = route_owner or canonical_owner(pos.get("owner"))
                route_broker = route_broker or canonical_broker(pos.get("broker"))
        else:
            if account and "NISA" in account and (not execution_owner or not execution_broker):
                raise PortfolioApplicationPending(
                    "holding_scope_unresolved",
                    f"{ticker} のNISA新規保有にはownerとbrokerが必要です",
                )
            detected_currency = currency or _auto_detect_currency(ticker, holdings)
            if not detected_currency:
                raise PortfolioApplicationPending(
                    "currency_unresolved",
                    f"{ticker} は新規銘柄のためcurrencyを解決できません",
                )
            new_key = _holding_key_for_new_position(
                holdings,
                ticker,
                account,
                execution_owner=execution_owner,
                execution_broker=execution_broker,
            )
            broker_label = {
                "rakuten": "楽天証券",
                "sbi": "SBI証券（妻）" if execution_owner == "wife" else "SBI証券",
            }.get(execution_broker or "", execution_broker or "")
            holdings[new_key] = {
                "ticker":          ticker,
                "entry_price":     price or 0,
                "shares":          float(quantity or 0),
                "entry_date":      datetime.now().strftime("%Y-%m-%d"),
                "account":         account or "特定",
                "currency":        detected_currency,
                "name":            name or ticker,
                "investment_type": investment_type,
                "owner":           execution_owner or "",
                "broker":          broker_label,
            }
            msg = f"{ticker}: 新規ポジション追加 ({detected_currency})"
            eff_currency = detected_currency
            eff_account  = account or "特定"
            route_owner = execution_owner
            route_broker = execution_broker

        account_after, holdings, cash_delta, cash_route = _compute_routed_cash_change(
            account_state,
            holdings,
            direction="buy",
            price=price,
            quantity=quantity,
            currency=eff_currency,
            account=eff_account,
            execution_owner=route_owner,
            execution_broker=route_broker,
        )
        _commit_portfolio_event(
            holdings_before=original_holdings,
            account_before=original_account,
            holdings_after=holdings,
            account_after=account_after,
            trade_args=("buy", ticker, price, quantity, None, None),
            event_kwargs={
                "event_id": event_id,
                "direction": "buy",
                "ticker": ticker,
                "price": price,
                "quantity": quantity,
                "currency": eff_currency,
                "account": eff_account,
                "execution_owner": route_owner,
                "execution_broker": route_broker,
                "cash_route": cash_route,
                "execution_position_key": key or new_key,
            },
            recovery_result={
                "updated": True,
                "message": msg,
                "realized_pnl_jpy": None,
                "cash_delta": cash_delta,
                "cash_currency": eff_currency,
                "cash_route": cash_route,
                "position_key": key or new_key,
                "event_id": event_id,
            },
        )
        return {"updated": True, "message": msg, "realized_pnl_jpy": None,
                "cash_delta": cash_delta, "cash_currency": eff_currency,
                "cash_route": cash_route, "position_key": key or new_key,
                "event_id": event_id}

    # ── SELL ─────────────────────────────────────────────
    if direction == "sell":
        if not key:
            all_candidates = _find_holding_matches(holdings, ticker)
            raise PortfolioApplicationPending(
                "holding_scope_unresolved",
                f"{ticker} の適用対象保有を解決できません",
                candidate_position_keys=all_candidates,
            )

        pos        = holdings[key]
        old_shares = float(pos.get("shares", 0))

        # P0-3: SELL 超過チェック（旧実装は max(0, old - sell) で黙って全売却していた）
        if quantity is None and not sell_all:
            raise HTTPException(
                status_code=422,
                detail="売却数量を入力してください（全売却の場合は sell_all: true を指定）",
            )
        sell_qty = float(quantity) if quantity is not None else old_shares
        if sell_qty > old_shares + 1e-9 and not sell_all:
            raise PortfolioApplicationPending(
                "holding_quantity_conflict",
                f"{ticker} のSELL数量 {sell_qty} が保有 {old_shares} を超えています",
                candidate_position_keys=[key],
            )
        # sell_all=true で quantity が大きい場合は old_shares でクリップ（全売却扱い）
        if sell_all and quantity is not None and float(quantity) > old_shares:
            sell_qty = old_shares

        entry_p  = float(pos.get("entry_price", 0))
        # M3: holdingにcurrencyが欠落していても黙ってUSD扱いにしない。
        # BUY新規ポジションと同じ自動判定を試み、それでも不明ならfail-closedで422。
        eff_currency = pos.get("currency") or _auto_detect_currency(ticker, holdings)
        if not eff_currency:
            raise PortfolioApplicationPending(
                "currency_unresolved",
                f"{ticker} のcurrencyが保有にもtickerにも解決できません",
                candidate_position_keys=[key],
            )
        eff_account  = pos.get("account", "特定")
        from execution_safety import canonical_broker, canonical_owner
        route_owner = execution_owner or canonical_owner(pos.get("owner"))
        route_broker = execution_broker or canonical_broker(pos.get("broker"))

        if price and entry_p:
            pnl_pct = (price - entry_p) / entry_p * 100
            pnl_raw = (price - entry_p) * sell_qty
            pnl_jpy = pnl_raw * _get_fx_rate() if eff_currency == "USD" else pnl_raw

        new_shares = old_shares - sell_qty
        if new_shares <= 1e-9:
            del holdings[key]
            msg = f"{ticker}: 全売却 → holdings から削除"
        else:
            holdings[key]["shares"] = round(new_shares, 6)
            msg = f"{ticker}: {old_shares} → {new_shares} 株"

        account_after, holdings, cash_delta, cash_route = _compute_routed_cash_change(
            account_state,
            holdings,
            direction="sell",
            price=price,
            quantity=sell_qty,
            currency=eff_currency,
            account=eff_account,
            execution_owner=route_owner,
            execution_broker=route_broker,
        )
        _commit_portfolio_event(
            holdings_before=original_holdings,
            account_before=original_account,
            holdings_after=holdings,
            account_after=account_after,
            trade_args=("sell", ticker, price, sell_qty, pnl_pct, pnl_jpy),
            event_kwargs={
                "event_id": event_id,
                "direction": "sell",
                "ticker": ticker,
                "price": price,
                "quantity": sell_qty,
                "currency": eff_currency,
                "account": eff_account,
                "pnl_jpy": pnl_jpy,
                "execution_owner": route_owner,
                "execution_broker": route_broker,
                "cash_route": cash_route,
                "execution_position_key": key,
            },
            recovery_result={
                "updated": True,
                "message": msg,
                "realized_pnl_jpy": pnl_jpy,
                "cash_delta": cash_delta,
                "cash_currency": eff_currency,
                "cash_route": cash_route,
                "position_key": key,
                "event_id": event_id,
            },
        )
        return {"updated": True, "message": msg, "realized_pnl_jpy": pnl_jpy,
                "cash_delta": cash_delta, "cash_currency": eff_currency,
                "cash_route": cash_route, "position_key": key,
                "event_id": event_id}

    return no_op


def _compute_cash_change(acc: dict, *, direction: str, price: Optional[float], quantity: Optional[float],
                         currency: Optional[str], account: Optional[str]) -> tuple[dict, Optional[float]]:
    """
    P0-4: 約定の現金 ± を account.json に反映した新状態を計算する。
    信用 / 持株会 口座は対象外（給与天引きや margin trade は別ロジック）。
    書き込みはしない。呼び出し側が holdings と一緒に commit する。
    """
    next_acc = copy.deepcopy(acc)
    if account in _CASH_EXEMPT_ACCOUNTS:
        return next_acc, None
    if price is None or quantity is None:
        return next_acc, None
    if float(price) <= 0 or float(quantity) <= 0:
        return next_acc, None

    sign = -1.0 if direction == "buy" else +1.0
    gross = float(price) * float(quantity)
    delta = round(sign * gross, 2)

    cur = (currency or "JPY").upper()
    if cur == "JPY":
        new_jpy = float(next_acc.get("balance", 0) or 0) + delta
        if new_jpy < 0:
            raise HTTPException(
                status_code=400,
                detail=f"楽天 JPY 残高不足: 現在 ¥{next_acc.get('balance', 0):,.0f}, 必要差分 ¥{delta:+,.0f}",
            )
        next_acc["balance"] = round(new_jpy, 2)
    elif cur == "USD":
        new_usd = float(next_acc.get("usd_balance", 0) or 0) + delta
        if new_usd < 0:
            raise HTTPException(
                status_code=400,
                detail=f"USD 残高不足: 現在 ${next_acc.get('usd_balance', 0):,.2f}, 必要差分 ${delta:+,.2f}",
            )
        next_acc["usd_balance"] = round(new_usd, 2)
    else:
        # その他通貨 (EUR 等) は現状対応なし
        return next_acc, None

    # total_cash の再計算
    jpy = float(next_acc.get("balance", 0) or 0)
    usd = float(next_acc.get("usd_balance", 0) or 0)
    fx  = float(next_acc.get("fx_rate_usdjpy", 150) or 150)
    next_acc["jpy_equivalent_usd"] = int(round(usd * fx))
    next_acc["total_cash"]   = int(round(jpy + usd * fx))
    next_acc["last_updated"] = datetime.now().date().isoformat()
    return next_acc, delta


def _compute_routed_cash_change(
    account_state: dict,
    holdings: dict,
    *,
    direction: str,
    price: Optional[float],
    quantity: Optional[float],
    currency: Optional[str],
    account: Optional[str],
    execution_owner: Optional[str],
    execution_broker: Optional[str],
) -> tuple[dict, dict, Optional[float], Optional[str]]:
    """Apply cash to the exact owner/broker wallet without cross-account fallback."""
    next_holdings = copy.deepcopy(holdings)
    if account in _CASH_EXEMPT_ACCOUNTS:
        return copy.deepcopy(account_state), next_holdings, None, None
    if price is None or quantity is None or float(price) <= 0 or float(quantity) <= 0:
        return copy.deepcopy(account_state), next_holdings, None, None

    owner = str(execution_owner or "").lower()
    broker = str(execution_broker or "").lower()
    cur = str(currency or "").upper()
    if not owner or not broker:
        raise PortfolioApplicationPending(
            "cash_route_unresolved",
            "約定を反映するowner×brokerの現金ルートを解決できません",
        )

    if owner == "husband" and broker == "rakuten" and cur in {"JPY", "USD"}:
        try:
            next_account, delta = _compute_cash_change(
                account_state,
                direction=direction,
                price=price,
                quantity=quantity,
                currency=cur,
                account=account,
            )
        except HTTPException as exc:
            if exc.status_code != 400:
                raise
            raise PortfolioApplicationPending(
                "cash_balance_insufficient",
                str(exc.detail),
            ) from exc
        return next_account, next_holdings, delta, "account.json"

    route_key = None
    if owner == "husband" and broker == "sbi" and cur == "JPY":
        route_key = "CASH_JPY_SBI"
    elif owner == "wife" and broker == "sbi" and cur == "JPY":
        route_key = "CASH_JPY_SBI_WIFE"
    if route_key is None:
        raise PortfolioApplicationPending(
            "cash_route_unresolved",
            f"{owner}×{broker}×{cur} の現金ルートは未定義です",
        )

    cash = next_holdings.get(route_key)
    if not isinstance(cash, dict):
        raise PortfolioApplicationPending(
            "cash_route_unresolved",
            f"現金行 {route_key} が見つかりません",
        )
    sign = -1.0 if direction == "buy" else 1.0
    delta = round(sign * float(price) * float(quantity), 2)
    current = float(cash.get("shares", 0) or 0)
    effective = round(current + delta, 2)

    if route_key == "CASH_JPY_SBI_WIFE":
        if cash.get("reported_balance_jpy") is None:
            cash["reported_balance_jpy"] = current
        if not cash.get("reported_as_of"):
            cash["reported_as_of"] = "2026-05-12"
        cash["ledger_delta_since_report_jpy"] = round(
            float(cash.get("ledger_delta_since_report_jpy", 0) or 0) + delta,
            2,
        )
        cash["balance_status"] = "estimated_negative" if effective < 0 else "estimated"
        cash["reconciliation_required"] = True
    elif effective < 0:
        raise PortfolioApplicationPending(
            "cash_balance_insufficient",
            f"{route_key} の推定残高が不足しています",
        )
    cash["shares"] = effective
    cash["last_ledger_update"] = datetime.now().isoformat(timespec="seconds")
    return copy.deepcopy(account_state), next_holdings, delta, route_key


def _sync_cash_mirrors_from_account(holdings: dict, account_state: dict) -> None:
    """
    account.json is the cash source of truth for manual execution entry.
    Keep holdings.json cash mirror rows in lockstep so portfolio_integrity
    does not drift after UI/API executions.
    """
    mirror_map = (
        ("CASH_JPY", "balance", 2),
        ("CASH_USD", "usd_balance", 2),
    )
    for holding_key, account_key, ndigits in mirror_map:
        rec = holdings.get(holding_key)
        if not isinstance(rec, dict):
            continue
        value = account_state.get(account_key)
        if value is None:
            continue
        rec["shares"] = round(float(value), ndigits)
        rec["entry_price"] = rec.get("entry_price", 1.0) or 1.0
        rec["investment_type"] = rec.get("investment_type") or "cash"
        rec["currency"] = rec.get("currency") or ("JPY" if holding_key == "CASH_JPY" else "USD")
        rec["note"] = f"account.json cash mirror sync {datetime.now().date().isoformat()}"


def _recover_prepared_portfolio_application(event_id: Optional[str]) -> Optional[dict]:
    """Finish an interrupted JSON application from the SQLite write-ahead journal."""
    if not event_id:
        return None
    from event_ledger import complete_portfolio_application, get_portfolio_application
    journal = get_portfolio_application(event_id)
    if not journal or journal.get("status") != "prepared":
        return None
    holdings_after = json.loads(str(journal["holdings_after_json"]))
    account_after = json.loads(str(journal["account_after_json"]))
    event_kwargs = json.loads(str(journal["event_kwargs_json"]))
    result = json.loads(str(journal["result_json"]))
    _save_json(HOLDINGS_FILE, holdings_after)
    _save_json(ACCOUNT_FILE, account_after)
    if not _ledger_event_exists(event_id):
        _record_trade_event(**event_kwargs)
    complete_portfolio_application(event_id)
    _invalidate_portfolio_cache()
    result["recovered_interrupted_application"] = True
    return result


def _commit_portfolio_event(*, holdings_before: dict, account_before: dict,
                            holdings_after: dict, account_after: dict,
                            trade_args: tuple, event_kwargs: dict,
                            recovery_result: dict) -> None:
    """
    複数 JSON + ledger の疑似トランザクション。
    完全な SQLite transaction ではないが、validation 後だけ書き、ledger 失敗時は JSON を巻き戻す。
    """
    wrote_holdings = False
    wrote_account = False
    event_id = str(event_kwargs.get("event_id") or "")
    try:
        _sync_cash_mirrors_from_account(holdings_after, account_after)
        if event_id:
            from event_ledger import prepare_portfolio_application
            prepare_portfolio_application(
                event_id=event_id,
                holdings_after=holdings_after,
                account_after=account_after,
                event_kwargs=event_kwargs,
                result=recovery_result,
            )
        _save_json(HOLDINGS_FILE, holdings_after)
        wrote_holdings = True
        _save_json(ACCOUNT_FILE, account_after)
        wrote_account = True
        _record_trade_event(**event_kwargs)
        if event_id:
            from event_ledger import complete_portfolio_application
            complete_portfolio_application(event_id)
    except Exception as e:
        if event_id and _ledger_event_exists(event_id):
            # The durable event proves this exact after-state owns the event.
            # Keep both JSON state and prepared journal so a retry can finish.
            raise e
        if wrote_holdings:
            _save_json(HOLDINGS_FILE, holdings_before)
        if wrote_account:
            _save_json(ACCOUNT_FILE, account_before)
        if event_id:
            from event_ledger import discard_portfolio_application
            discard_portfolio_application(event_id)
        raise e

    try:
        _append_trade(*trade_args)
    except Exception as e:
        print(f"[trade_history] append skip: {e}")

    _invalidate_portfolio_cache()


def _apply_to_portfolio(req: ExecutionRequest, *, event_id: Optional[str]) -> dict:
    """POST /api/actions/execute path のための薄い wrapper。"""
    return _apply_event_to_ledger(
        event_id=event_id,
        ticker=req.ticker,
        direction=req.direction.value,
        quantity=req.quantity,
        price=req.price,
        currency=(req.currency.value if req.currency else None),
        account=(req.account.value if req.account else None),
        investment_type=req.investment_type.value,
        status=req.status.value,
        sell_all=req.sell_all,
        name=req.name,
        execution_owner=req.execution_owner,
        execution_broker=req.execution_broker,
        execution_position_keys=req.execution_position_keys,
    )


def _log_action_stage_executed(
    *,
    analysis_id: Optional[str] = None,
    ticker: str,
    direction: str,
    account: Optional[str],
    investment_type: Optional[str],
    price: Optional[float],
    quantity: Optional[float],
    currency: Optional[str],
    portfolio_result: Optional[dict],
    as_of: str,
) -> None:
    """
    action_stage_log の executed ステージへ JPY 換算 notional で記録する。

    Codex re-review #1:
      - 実際に ledger 反映された fill のみ記録 (portfolio_result.updated=True)。
        status=ordered 等の未約定や、再反映なしの編集では記録しない。
      - notional は JPY 換算。USD 建ては FX を掛ける (cash_delta は cash_currency 建て)。
      - POST (新規約定) と PATCH (ordered→executed 遷移) の両経路から呼ぶ。
    失敗しても約定保存は成立済みなので握りつぶす。

    Codex re-review round3 #3: 冪等再適用 (already_applied=True) は updated=True を
    返すが ledger には新規反映していないので executed として記録しない (重複防止)。
    """
    if not isinstance(portfolio_result, dict) or not portfolio_result.get("updated"):
        return
    if portfolio_result.get("already_applied"):
        return
    try:
        from action_stage_log import log_executed as _asl_exec
        # notional(JPY): cash_delta(cash_currency 建て) を優先し JPY 換算。
        # Codex round4 #4: cover/sell_all は cash_delta=None かつ caller quantity=None に
        # なり得るので、ledger が返す applied_quantity/applied_price を fallback に使う。
        notional = None
        cd = portfolio_result.get("cash_delta")
        cc = (portfolio_result.get("cash_currency") or currency or "").upper()
        eff_qty = (portfolio_result.get("applied_quantity")
                   if portfolio_result.get("applied_quantity") is not None else quantity)
        eff_price = (portfolio_result.get("applied_price")
                     if portfolio_result.get("applied_price") is not None else price)
        if isinstance(cd, (int, float)):
            fx = _get_fx_rate() if cc == "USD" else 1.0
            notional = abs(float(cd)) * float(fx)
        elif eff_price is not None and eff_qty is not None:
            fx = _get_fx_rate() if cc == "USD" else 1.0
            notional = abs(float(eff_price) * float(eff_qty)) * float(fx)
        _asl_exec(
            analysis_id=analysis_id or "execution",
            as_of=as_of,
            action={
                "type":    direction,
                "ticker":  ticker,
                "account": account or "",
                "tier":    investment_type or "",
                "source":  "execution",
            },
            estimated_notional_jpy=(round(notional) if notional is not None else None),
        )
    except Exception as _asl_e:
        print(f"[action_stage_log] executed log skip: {_asl_e}")


# ============================================================
# エンドポイント
# ============================================================

@router.post("/api/actions/execute")
async def save_execution(req: ExecutionRequest):
    """
    P1-4: ExecutionRequest による厳格なバリデーション。
    入力不備は 422 で返し、通貨未指定は新規銘柄時のみエラー。
    """
    _validate_action_state_link(req)
    linked_readiness, linked_block_reasons = _enforce_ai_order_readiness(req)
    _enforce_discretionary_order_funding(req)
    _enforce_ordered_exit_inventory(req)
    _validate_contribution_link(req)
    plan_metadata = _snapshot_execution_plan_metadata(req)
    _validate_fill_trade_inputs(
        ticker=req.ticker,
        direction=req.direction.value,
        status=req.status.value,
        quantity=req.quantity,
        price=req.price,
        sell_all=req.sell_all,
    )
    exec_id = _execution_id_from_key(req.idempotency_key)
    request_hash = _execution_request_hash(req)
    from event_ledger import (
        complete_execution_idempotency,
        reserve_execution_idempotency,
    )
    registry, created = reserve_execution_idempotency(
        idempotency_key=req.idempotency_key,
        request_hash=request_hash,
        execution_id=exec_id,
    )
    if not created:
        if registry.get("request_hash") != request_hash:
            raise HTTPException(
                status_code=409,
                detail="同じidempotency_keyが異なる約定payloadで使用されています",
            )
        if registry.get("response_json"):
            response = json.loads(str(registry["response_json"]))
            response["idempotent_replay"] = True
            return response
        existing_record = _execution_record_by_id(exec_id)
        if existing_record is not None:
            response = _response_for_execution_record(existing_record, idempotent_replay=True)
            complete_execution_idempotency(
                idempotency_key=req.idempotency_key,
                response={**response, "idempotent_replay": False},
                application_status=response["portfolio_application_status"],
            )
            return response
    try:
        with process_lock("portfolio_ledger"):
            # Another worker may have completed this deterministic execution
            # while this request was waiting for the ledger lock.
            existing_record = _execution_record_by_id(exec_id)
            if existing_record is not None:
                response = _response_for_execution_record(existing_record, idempotent_replay=True)
                complete_execution_idempotency(
                    idempotency_key=req.idempotency_key,
                    response={**response, "idempotent_replay": False},
                    application_status=response["portfolio_application_status"],
                )
                return response
            # ① ポートフォリオ反映
            pending_reason = None
            try:
                portfolio_result = _apply_to_portfolio(req, event_id=_make_execution_event_id(exec_id))
            except PortfolioApplicationPending as exc:
                if req.status not in {Status.executed, Status.partial}:
                    raise HTTPException(status_code=409, detail=exc.message) from exc
                pending_reason = exc
                portfolio_result = {
                    "updated": False,
                    "message": "約定事実を保存しました。ポートフォリオへの適用は解決待ちです",
                    "realized_pnl_jpy": None,
                    "cash_delta": None,
                    "cash_currency": (req.currency.value if req.currency else None),
                    "event_id": None,
                }

            # ② 実行ログ保存
            data    = _load_execution_log()
            records = [_normalize_execution_record(r) for r in data.get("executions", [])]

            # v5.1: Implementation Shortfall（AI 提示価格と約定価格の差）
            try:
                from execution_quality import _compute_shortfall_bps
                _shortfall_bps = _compute_shortfall_bps(
                    req.price, req.decision_price, req.direction.value
                )
            except Exception:
                _shortfall_bps = None

            record = {
                "id":              exec_id,
                "idempotency_key": req.idempotency_key,
                "saved_at":        datetime.now().isoformat(),
                "ticker":          req.ticker,
                "direction":       req.direction.value,
                "action":          req.action,
                "status":          req.status.value,
                "price":           req.price,
                "quantity":        req.quantity,
                "currency":        (req.currency.value if req.currency else None),
                # P0-8: ordered → executed PATCH 経由の replay 用に保存
                "account":         (req.account.value if req.account else None),
                "investment_type": req.investment_type.value,
                "execution_owner": req.execution_owner,
                "execution_broker": req.execution_broker,
                "execution_position_keys": req.execution_position_keys,
                "contribution_id":     req.contribution_id,
                "sell_all":        req.sell_all,
                "name":            req.name,
                "note":            req.note,
                # A-8: 執行品質トラッキング
                "order_type":       req.order_type,
                "bid_at_order":     req.bid_at_order,
                "ask_at_order":     req.ask_at_order,
                "executed_at_time": req.executed_at_time,
                # v5.1: AI 指値判断 + Implementation Shortfall
                "limit_price":               req.limit_price,
                "decision_price":            req.decision_price,
                "decision_ts":               req.decision_ts,
                "ai_recommended_order_type": req.ai_recommended_order_type,
                "ai_recommended_limit":      req.ai_recommended_limit,
                "shortfall_bps":             round(_shortfall_bps, 2) if _shortfall_bps is not None else None,
                # P0-8: ledger 反映済みフラグ（idempotency 保証）
                "portfolio_applied":    bool(portfolio_result["updated"]),
                "portfolio_applied_at": datetime.now().isoformat() if portfolio_result["updated"] else None,
                "portfolio_updated":    portfolio_result["updated"],
                "portfolio_message":    portfolio_result["message"],
                "realized_pnl_jpy":     portfolio_result.get("realized_pnl_jpy"),
                "cash_delta":           portfolio_result.get("cash_delta"),
                "cash_currency":        portfolio_result.get("cash_currency"),
                "event_id":             portfolio_result.get("event_id"),
                "cash_route":           portfolio_result.get("cash_route"),
                "position_key":         portfolio_result.get("position_key"),
                "portfolio_application_status": (
                    "pending" if pending_reason
                    else ("applied" if portfolio_result["updated"] else "not_applicable")
                ),
                "portfolio_application_pending": bool(pending_reason),
                "portfolio_application_reasons": ([{
                    "code": pending_reason.code,
                    "message": pending_reason.message,
                }] if pending_reason else []),
                "candidate_position_keys": (
                    pending_reason.candidate_position_keys if pending_reason else []
                ),
                "margin_position_id":   portfolio_result.get("margin_position_id"),
                "margin_closed_position_ids": portfolio_result.get("margin_closed_position_ids"),
                "margin_side":          portfolio_result.get("margin_side"),
                **plan_metadata,
            }
            try:
                _record_cash = abs(float(portfolio_result.get("cash_delta")))
                _record_currency = str(
                    portfolio_result.get("cash_currency")
                    or (req.currency.value if req.currency else "")
                ).upper()
                record["notional_jpy"] = round(
                    _record_cash * (_get_fx_rate() if _record_currency == "USD" else 1.0)
                )
            except (TypeError, ValueError):
                pass
            if req.analysis_id:
                record["analysis_id"] = req.analysis_id
            if req.action_state_id:
                record["action_state_id"] = req.action_state_id
            if req.policy_override_reason:
                record["policy_override_reason"] = req.policy_override_reason
            if req.analysis_id and not req.action_state_id:
                record["provenance_incomplete"] = True
            if (
                req.status in {Status.executed, Status.partial}
                and req.account in {Account.nisa_g, Account.nisa_t}
                and (not req.execution_owner or not req.execution_broker)
            ):
                record["provenance_incomplete"] = True
                record["provenance_incomplete_reason"] = "nisa_owner_or_broker_missing"
            if req.status == Status.ordered and linked_readiness is not None:
                record["readiness_at_order"] = linked_readiness
                record["execution_block_reasons_at_order"] = linked_block_reasons
            if req.status in {Status.executed, Status.partial} and linked_readiness not in {None, "ready"}:
                record["executed_despite_readiness"] = True
                record["readiness_at_execution"] = linked_readiness
                record["execution_block_reasons_at_execution"] = linked_block_reasons
            action_state_id = _sync_action_state_for_execution(
                ticker=req.ticker,
                direction=req.direction.value,
                execution_status=req.status.value,
                note=f"execution:{exec_id} status={req.status.value}",
                action_state_id=req.action_state_id,
                execution_owner=req.execution_owner,
                execution_broker=req.execution_broker,
                execution_account=(req.account.value if req.account else None),
                execution_investment_type=req.investment_type.value,
                execution_position_keys=req.execution_position_keys,
            )
            if action_state_id:
                record["action_state_id"] = action_state_id
            records.append(record)
            if len(records) > 500:
                records = records[-500:]
            data["executions"] = records
            _save_json(EXEC_FILE, data)
    except LockBusy as e:
        raise HTTPException(status_code=409, detail="portfolio ledger is busy") from e

    # F3: action_stage_log の executed ステージへ記録。
    # Codex re-review #1: 実際に ledger 反映された fill のみ記録する
    # (status=ordered 等の未約定は除外)。notional は JPY 換算で記録する。
    _log_action_stage_executed(
        analysis_id=req.analysis_id,
        ticker=req.ticker,
        direction=req.direction.value,
        account=(req.account.value if req.account else ""),
        investment_type=req.investment_type.value,
        price=req.price,
        quantity=req.quantity,
        currency=(req.currency.value if req.currency else None),
        portfolio_result=portfolio_result,
        as_of=record["saved_at"],
    )

    response = _response_for_execution_record(record, idempotent_replay=False)
    complete_execution_idempotency(
        idempotency_key=req.idempotency_key,
        response=response,
        application_status=response["portfolio_application_status"],
    )
    return response


@router.post("/api/actions/executions/{exec_id}/resolve-portfolio")
async def resolve_execution_portfolio(exec_id: str, req: PortfolioResolutionRequest):
    """Resolve an intentionally pending fill without replaying its execution fact."""
    from event_ledger import update_execution_idempotency_response

    try:
        with process_lock("portfolio_ledger"):
            data = _load_execution_log()
            records = [_normalize_execution_record(r) for r in data.get("executions", [])]
            idx = next((i for i, row in enumerate(records) if row.get("id") == exec_id), None)
            if idx is None:
                raise HTTPException(status_code=404, detail="execution が見つかりません")
            record = records[idx]
            if record.get("status") not in {"executed", "partial"}:
                raise HTTPException(status_code=409, detail="約定済みexecutionだけを解決できます")

            current_status = record.get("portfolio_application_status")
            if current_status in {"applied", "externally_reconciled"}:
                return _response_for_execution_record(record, idempotent_replay=True)

            if req.resolution == "externally_reconciled":
                if not req.external_reconcile_source:
                    raise HTTPException(
                        status_code=422,
                        detail="externally_reconciled には external_reconcile_source が必須です",
                    )
                owner = req.execution_owner or record.get("execution_owner")
                broker = req.execution_broker or record.get("execution_broker")
                account = req.account.value if req.account else record.get("account")
                if not owner or not broker:
                    raise HTTPException(
                        status_code=422,
                        detail="外部照合でもexecution_ownerとexecution_brokerの確定が必要です",
                    )
                investment_type = (
                    req.investment_type.value
                    if req.investment_type
                    else record.get("investment_type") or "medium"
                )
                position_keys = (
                    [req.execution_position_key]
                    if req.execution_position_key
                    else record.get("execution_position_keys") or []
                )
                record.update({
                    "execution_owner": owner,
                    "execution_broker": broker,
                    "account": account,
                    "investment_type": investment_type,
                    "execution_position_keys": position_keys,
                    "position_key": req.execution_position_key or record.get("position_key"),
                    "portfolio_application_status": "externally_reconciled",
                    "portfolio_application_pending": False,
                    "portfolio_application_reasons": [],
                    "candidate_position_keys": [],
                    "portfolio_reconciled_externally": True,
                    "externally_reconciled": True,
                    "external_reconcile_source": req.external_reconcile_source,
                    "portfolio_resolved_at": datetime.now().isoformat(),
                    "portfolio_message": "外部ポートフォリオで照合済み（内部台帳は未更新）",
                })
                if owner and broker:
                    record.pop("provenance_incomplete", None)
                    record.pop("provenance_incomplete_reason", None)
            else:
                owner = req.execution_owner or record.get("execution_owner")
                broker = req.execution_broker or record.get("execution_broker")
                account = req.account.value if req.account else record.get("account")
                investment_type = (
                    req.investment_type.value
                    if req.investment_type
                    else record.get("investment_type") or "medium"
                )
                position_keys = (
                    [req.execution_position_key]
                    if req.execution_position_key
                    else record.get("execution_position_keys")
                )
                try:
                    result = _apply_event_to_ledger(
                        event_id=_make_execution_event_id(exec_id),
                        ticker=str(record.get("ticker") or ""),
                        direction=str(record.get("direction") or ""),
                        quantity=record.get("quantity"),
                        price=record.get("price"),
                        currency=record.get("currency"),
                        account=account,
                        investment_type=investment_type,
                        status=str(record.get("status") or ""),
                        sell_all=bool(record.get("sell_all")),
                        name=record.get("name"),
                        execution_owner=owner,
                        execution_broker=broker,
                        execution_position_keys=position_keys,
                    )
                except PortfolioApplicationPending as exc:
                    record["portfolio_application_reasons"] = [{
                        "code": exc.code,
                        "message": exc.message,
                    }]
                    record["candidate_position_keys"] = exc.candidate_position_keys
                    records[idx] = record
                    data["executions"] = records
                    _save_json(EXEC_FILE, data)
                    raise HTTPException(status_code=409, detail=exc.message) from exc

                record.update({
                    "execution_owner": owner,
                    "execution_broker": broker,
                    "account": account,
                    "investment_type": investment_type,
                    "execution_position_keys": position_keys,
                    "position_key": result.get("position_key") or req.execution_position_key,
                    "portfolio_applied": True,
                    "portfolio_updated": True,
                    "portfolio_applied_at": datetime.now().isoformat(),
                    "portfolio_application_status": "applied",
                    "portfolio_application_pending": False,
                    "portfolio_application_reasons": [],
                    "candidate_position_keys": [],
                    "portfolio_resolved_at": datetime.now().isoformat(),
                    "portfolio_message": result.get("message"),
                    "realized_pnl_jpy": result.get("realized_pnl_jpy"),
                    "cash_delta": result.get("cash_delta"),
                    "cash_currency": result.get("cash_currency"),
                    "cash_route": result.get("cash_route"),
                    "event_id": result.get("event_id"),
                })
                if owner and broker:
                    record.pop("provenance_incomplete", None)
                    record.pop("provenance_incomplete_reason", None)

            records[idx] = record
            data["executions"] = records
            _save_json(EXEC_FILE, data)
            response = _response_for_execution_record(record, idempotent_replay=False)
            update_execution_idempotency_response(
                exec_id,
                response=response,
                application_status=response["portfolio_application_status"],
            )
            return response
    except LockBusy as exc:
        raise HTTPException(status_code=409, detail="portfolio ledger is busy") from exc


@router.get("/api/actions/executions")
async def get_executions():
    data = _load_execution_log()
    data["executions"] = [_normalize_execution_record(r) for r in data.get("executions", [])]
    return data


@router.get("/api/actions/pending-stops")
async def get_pending_stops():
    """未発注ストップ/重要アクション一覧 + ブロック状態を返す"""
    try:
        from action_state_tracker import get_all_pending, check_new_position_block
        pending = get_all_pending(days_threshold=0)
        block   = check_new_position_block()
        return {
            "ok":      True,
            "blocked": block["blocked"],
            "reason":  block["reason"],
            "pending": pending,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "pending": []}


@router.patch("/api/actions/status/{action_id}")
async def update_action_status(action_id: str, req: StatusPatchRequest):
    """Cancel a recommendation without fabricating an order or fill record."""
    if req.status in {"placed", "filled"}:
        raise HTTPException(
            status_code=409,
            detail="placed/filled は execution record と同時に記録してください (/api/actions/execute)",
        )
    try:
        from action_state_tracker import update_status
        ok = update_status(action_id, req.status, note=req.note)
        return {"ok": ok, "action_id": action_id, "status": req.status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.delete("/api/actions/executions/{exec_id}")
async def delete_execution(exec_id: str):
    try:
        with process_lock("portfolio_ledger"):
            data = _load_execution_log()
            records = [_normalize_execution_record(r) for r in data.get("executions", [])]
            target = next((r for r in records if r.get("id") == exec_id), None)
            if target is None:
                return {"ok": False, "message": "記録が見つかりません"}
            if _is_portfolio_applied(target):
                raise HTTPException(
                    status_code=409,
                    detail="portfolio 反映済みの実行記録は削除できません。取り消しは reversal event で処理してください。",
                )
            if target.get("status") in {"executed", "partial", "filled"}:
                raise HTTPException(
                    status_code=409,
                    detail="約定事実は削除できません。適用待ちはresolve-portfolioで解決してください。",
                )
            if target.get("status") in {"ordered", "partial"}:
                _sync_action_state_for_execution(
                    ticker=target.get("ticker") or "",
                    direction=target.get("direction") or "",
                    execution_status="cancelled",
                    note=f"execution:{exec_id} deleted/cancelled",
                    action_state_id=target.get("action_state_id"),
                )
            data["executions"] = [r for r in records if r.get("id") != exec_id]
            _save_json(EXEC_FILE, data)
            return {"ok": True}
    except LockBusy as e:
        raise HTTPException(status_code=409, detail="portfolio ledger is busy") from e


# ============================================================
# Fix 1A (2026-04-24): 実行記録の部分更新 PATCH エンドポイント
# 問題: 注文中カードを submit 後に価格/数量/ステータスを修正できなかった。
# この PATCH で price / quantity / note / status / currency を個別更新可能に。
# ============================================================

class ExecutionPatchRequest(BaseModel):
    """実行記録の部分更新リクエスト。全フィールド optional。"""
    price:    Optional[float]    = None
    quantity: Optional[float]    = None
    note:     Optional[str]      = None
    status:   Optional[Status]   = None
    currency: Optional[Currency] = None

    @field_validator("price", "quantity")
    @classmethod
    def _non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("price/quantity は 0 以上")
        return v


@router.patch("/api/actions/executions/{exec_id}")
async def patch_execution(exec_id: str, req: ExecutionPatchRequest):
    """
    既存の実行記録を部分更新する。
    body 内の指定フィールドのみ上書き、未指定フィールドは保持。
    ordered → executed への状態遷移など、あとから埋めるユースケースに対応。
    """
    try:
        with process_lock("portfolio_ledger"):
            data = _load_execution_log()
            records = [_normalize_execution_record(r) for r in data.get("executions", [])]

            idx = next((i for i, r in enumerate(records) if r.get("id") == exec_id), -1)
            if idx < 0:
                raise HTTPException(status_code=404, detail=f"execution id '{exec_id}' not found")

            rec = dict(records[idx])  # shallow copy
            old_status = rec.get("status")
            was_applied = _is_portfolio_applied(rec)

            patch = req.model_dump(exclude_none=True)
            # Enum を生の文字列に展開して保存（JSON 互換性のため）
            if "status" in patch and hasattr(patch["status"], "value"):
                patch["status"] = patch["status"].value
            if "currency" in patch and hasattr(patch["currency"], "value"):
                patch["currency"] = patch["currency"].value

            rec.update(patch)
            rec["edited_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                from execution_quality import _compute_shortfall_bps
                _shortfall_bps = _compute_shortfall_bps(
                    rec.get("price"),
                    rec.get("decision_price"),
                    rec.get("direction"),
                )
                rec["shortfall_bps"] = round(_shortfall_bps, 2) if _shortfall_bps is not None else None
            except Exception:
                rec["shortfall_bps"] = rec.get("shortfall_bps")

            # ── P0-8: ordered -> executed/partial の状態遷移時だけ portfolio 反映 ──
            # 既に executed/partial の過去レコード編集では再反映しない。
            # 旧 portfolio_updated=true も applied 扱い。
            portfolio_result = None
            transitioned_to_fill = old_status not in ("executed", "partial") and rec.get("status") in ("executed", "partial")
            if transitioned_to_fill and (rec.get("analysis_id") or rec.get("action_state_id")):
                latest_readiness, latest_reasons = _linked_ai_readiness_values(
                    analysis_id=rec.get("analysis_id"),
                    action_state_id=rec.get("action_state_id"),
                    ticker=str(rec.get("ticker") or ""),
                    direction=str(rec.get("direction") or ""),
                )
                if latest_readiness == "unknown" and rec.get("readiness_at_order"):
                    latest_readiness = str(rec.get("readiness_at_order"))
                    latest_reasons = [
                        row for row in (rec.get("execution_block_reasons_at_order") or [])
                        if isinstance(row, dict)
                    ]
                if latest_readiness not in {None, "ready"}:
                    rec["executed_despite_readiness"] = True
                    rec["readiness_at_execution"] = latest_readiness
                    rec["execution_block_reasons_at_execution"] = latest_reasons
            if transitioned_to_fill and not was_applied:
                try:
                    portfolio_result = _apply_event_to_ledger(
                        event_id=_make_execution_event_id(exec_id),
                        ticker=rec.get("ticker"),
                        direction=rec.get("direction") or "hold",
                        quantity=rec.get("quantity"),
                        price=rec.get("price"),
                        currency=rec.get("currency"),
                        account=rec.get("account"),
                        investment_type=rec.get("investment_type") or "medium",
                        status=rec.get("status") or "executed",
                        sell_all=bool(rec.get("sell_all", False)),
                        name=rec.get("name"),
                        execution_owner=rec.get("execution_owner"),
                        execution_broker=rec.get("execution_broker"),
                        execution_position_keys=rec.get("execution_position_keys"),
                    )
                except PortfolioApplicationPending as exc:
                    portfolio_result = {
                        "updated": False,
                        "message": "約定事実を保存しました。ポートフォリオへの適用は解決待ちです",
                        "event_id": None,
                    }
                    rec["portfolio_application_status"] = "pending"
                    rec["portfolio_application_pending"] = True
                    rec["portfolio_application_reasons"] = [{
                        "code": exc.code,
                        "message": exc.message,
                    }]
                    rec["candidate_position_keys"] = exc.candidate_position_keys
                if portfolio_result.get("updated"):
                    rec["portfolio_applied"]    = True
                    rec["portfolio_applied_at"] = datetime.now().isoformat()
                    rec["portfolio_updated"]    = True
                    rec["portfolio_message"]    = portfolio_result.get("message")
                    rec["realized_pnl_jpy"]     = portfolio_result.get("realized_pnl_jpy")
                    rec["cash_delta"]           = portfolio_result.get("cash_delta")
                    rec["cash_currency"]        = portfolio_result.get("cash_currency")
                    rec["event_id"]             = portfolio_result.get("event_id")
                    rec["margin_position_id"]   = portfolio_result.get("margin_position_id")
                    rec["margin_closed_position_ids"] = portfolio_result.get("margin_closed_position_ids")
                    rec["margin_side"]          = portfolio_result.get("margin_side")
                    rec["cash_route"]           = portfolio_result.get("cash_route")
                    rec["position_key"]         = portfolio_result.get("position_key")
                    rec["portfolio_application_status"] = "applied"
                    rec["portfolio_application_pending"] = False
                    rec["portfolio_application_reasons"] = []

            if "status" in patch:
                action_state_id = _sync_action_state_for_execution(
                    ticker=rec.get("ticker") or "",
                    direction=rec.get("direction") or "",
                    execution_status=rec.get("status") or "",
                    note=f"execution:{exec_id} status={rec.get('status')}",
                    action_state_id=rec.get("action_state_id"),
                    execution_owner=rec.get("execution_owner"),
                    execution_broker=rec.get("execution_broker"),
                    execution_account=rec.get("account"),
                    execution_investment_type=rec.get("investment_type"),
                    execution_position_keys=rec.get("execution_position_keys"),
                )
                if action_state_id:
                    rec["action_state_id"] = action_state_id

            records[idx] = _normalize_execution_record(rec)
            data["executions"] = records
            _save_json(EXEC_FILE, data)
    except LockBusy as e:
        raise HTTPException(status_code=409, detail="portfolio ledger is busy") from e

    # F3 / Codex re-review #1: ordered→executed の PATCH 約定も executed ログへ記録。
    # _apply_event_to_ledger が反映した場合のみ (portfolio_result.updated)。
    _log_action_stage_executed(
        analysis_id=rec.get("analysis_id"),
        ticker=rec.get("ticker") or "",
        direction=rec.get("direction") or "",
        account=rec.get("account"),
        investment_type=rec.get("investment_type"),
        price=rec.get("price"),
        quantity=rec.get("quantity"),
        currency=rec.get("currency"),
        portfolio_result=portfolio_result,
        as_of=rec.get("edited_at") or datetime.now().isoformat(),
    )

    return {
        "ok": True,
        "execution": rec,
        "updated_fields": list(patch.keys()),
        "portfolio": portfolio_result,
    }

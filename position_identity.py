"""position_identity.py — Stage 0B: Position / AccountResource / NisaCapacity 鮮度権威

背景 (2026-07-27 インシデント):
`execution_readiness.portfolio_snapshot_health()` は holdings.json の
ファイル全体の更新時刻 (実体は無く OS mtime にフォールバック) と、
action_executions.json 全体の最新 portfolio_applied 実行時刻の最大値だけで
「鮮度」を判定していた。

実データで確認した実際の混入経路:
  - AVGO/XLF の holdings.json エントリは note="楽天CSV保有同期 2026-07-14"
    のまま最終更新が止まっていた
  - 無関係な LLY の約定が 2026-07-23T00:17:45 に portfolio_applied=True と
    なり、file-wide の execution_ledger_current=True (=全ポジション「新鮮」
    24h 相当にクランプ) を成立させた
  - この file-wide チェックは risk_increasing (買い系) でしか評価されず、
    売り系 (trim/sell) には一切適用されていなかった

この module は「鮮度」を PositionIdentity 単位で解決し、上記2つの穴 —
(a) 無関係な銘柄の更新に影響される (b) 売り系に鮮度チェックが無い — を
両方塞ぐ。account.json/現金残高 (AccountResourceIdentity 相当) は
既存の evaluate_cash_buying_power() が別途 fail-closed で扱っているため、
本 module のスコープは PositionIdentity (現物保有) に絞る。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# 証券会社で一度確認した数量・取得原価は、時間経過だけでは変化しない。
# 2026-07-30 のユーザー判断により、ポジション snapshot は固定時間 TTL
# ではなく event-based validity とする。同一 PositionIdentity に対する
# snapshot 後の fill / 訂正不能な約定が見つかったときだけ invalidated に
# 落とす。市場価格・ニュース等の時系列データの TTL とは別契約である。


@dataclass(frozen=True)
class PositionIdentity:
    """売却対象を一意に定める鍵。

    ticker 単体では株式分割・シンボル変更・投信の内部コード違いで同一銘柄が
    分断されうるため、常に owner/broker/account のスコープと組で扱う。
    """
    owner: str
    broker: str
    account: str
    canonical_instrument_id: str

    @property
    def key(self) -> str:
        return f"{self.owner}|{self.broker}|{self.account}|{self.canonical_instrument_id}"


@dataclass(frozen=True)
class AccountResourceIdentity:
    """買付資金を一意に定める鍵。

    settled_cash / available_to_trade / margin_buying_power を混同しない
    よう resource_kind を持つ。既存の evaluate_cash_buying_power() が扱う
    ルート (account.json, CASH_JPY_SBI 等) と対応させるための識別子。
    """
    owner: str
    broker: str
    account: str
    currency: str
    resource_kind: str = "cash"

    @property
    def key(self) -> str:
        return f"{self.owner}|{self.broker}|{self.account}|{self.currency}|{self.resource_kind}"


@dataclass(frozen=True)
class NisaCapacityIdentity:
    """NISA枠を一意に定める鍵。"""
    owner: str
    broker: str
    account: str
    nisa_type: str
    tax_year: int

    @property
    def key(self) -> str:
        return f"{self.owner}|{self.broker}|{self.account}|{self.nisa_type}|{self.tax_year}"


def canonical_instrument_id(ticker: str) -> str:
    """symbol変更等を将来吸収するための正規化フック。現状は大文字化のみ。"""
    return str(ticker or "").strip().upper()


def infer_owner_from_holding(
    account_raw: str | None,
    *,
    key: str | None = None,
) -> Optional[str]:
    """Infer owner only from a positive owner marker.

    Missing ``_WIFE`` is not evidence that a record belongs to the husband.
    Ambiguous records return None so execution and NISA planning fail closed.
    """
    if "妻" in str(account_raw or "") or (key and "_WIFE" in key.upper()):
        return "wife"
    if "夫" in str(account_raw or "") or (key and "_HUSBAND" in key.upper()):
        return "husband"
    return None


def position_identity_for_holding(entry: dict, *, key: str | None = None) -> Optional[PositionIdentity]:
    """holdings.json のエントリから PositionIdentity を作る。

    owner/broker/account は execution_safety の既存正規化関数 (canonical_owner
    / canonical_broker / canonical_account) にそのまま通す。これは
    action dict 側 (execution_owner/execution_broker/execution_account) が
    同じ関数で正規化されているため — 別々の正規化ロジックを持つと、同じ
    実在ポジションを指しているのに鍵が一致しない (=鮮度チェックが常に
    "unknown" になる) という新しいバグを生む。

    ticker/account/broker のいずれかが欠落、または正規化後に owner/broker/
    account のいずれかが空文字になるレコードは fail-closed で None を返す
    (推測で埋めない)。owner フィールド自体を持たない既存レコードが大半
    なため、account名の「妻/夫」表記や key の "_WIFE/_HUSBAND"
    サフィックスから確認できる場合のみ補う。それ以外は fail-closed で
    None を返す。
    """
    from execution_safety import canonical_account, canonical_broker, canonical_owner

    ticker = entry.get("ticker")
    account_raw = entry.get("account")
    broker_raw = entry.get("broker")
    if not ticker or not account_raw or not broker_raw:
        return None

    owner_raw = entry.get("owner")
    if not owner_raw:
        owner_raw = infer_owner_from_holding(account_raw, key=key)
    if not owner_raw:
        return None

    owner = canonical_owner(owner_raw)
    broker = canonical_broker(broker_raw)
    account = canonical_account(account_raw)
    if not owner or not broker or not account:
        return None

    return PositionIdentity(
        owner=owner,
        broker=broker,
        account=account,
        canonical_instrument_id=canonical_instrument_id(ticker),
    )


def position_identity_for_action(action: dict) -> Optional[PositionIdentity]:
    """priority_action / action_state エントリから PositionIdentity を作る。

    position_identity_for_holding() と同じ正規化関数を使うことで、
    holdings.json 側と action 側の鍵が必ず一致するようにする。
    """
    from execution_safety import canonical_account, canonical_broker, canonical_owner

    ticker = action.get("ticker")
    if not ticker:
        return None
    owner = canonical_owner(action.get("execution_owner") or action.get("owner"))
    broker = canonical_broker(action.get("execution_broker") or action.get("broker"))
    account = canonical_account(action.get("execution_account") or action.get("account"))
    if not owner or not broker or not account:
        return None
    return PositionIdentity(
        owner=owner,
        broker=broker,
        account=account,
        canonical_instrument_id=canonical_instrument_id(ticker),
    )


_NOTE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def parse_note_sync_date(note: object) -> Optional[datetime]:
    """holdings.json エントリの note (自由記述) から証券会社同期日を抽出する。

    例: "楽天CSV保有同期 2026-07-14" -> datetime(2026,7,14)
    パースできない場合は None (呼び出し元は「不明」として fail-closed に扱う)。
    """
    if not note:
        return None
    m = _NOTE_DATE_RE.search(str(note))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return None


def _load_json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _find_holding_entry(position: PositionIdentity, *, base_dir: Path) -> dict | None:
    raw = _load_json_object(base_dir / "holdings.json") or {}
    positions = raw.get("holdings") or raw.get("positions") or raw
    if not isinstance(positions, dict):
        return None
    for key, v in positions.items():
        if not isinstance(v, dict):
            continue
        candidate = position_identity_for_holding(v, key=key)
        if candidate == position:
            return v
    return None


def _parse_sync_timestamp(entry: dict) -> tuple[datetime | None, str]:
    """Resolve an identity-scoped broker snapshot timestamp.

    Structured reconciliation fields are authoritative.  The historical note
    parser remains as an explicitly labelled compatibility source so old
    holdings become stale/review instead of being silently treated as current.
    """
    for key in (
        "source_as_of",
        "reported_as_of",
        "broker_reconciled_at",
        "reconciled_at",
        "last_updated",
    ):
        value = entry.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed, f"holding.{key}"
        except ValueError:
            continue
    return parse_note_sync_date(entry.get("note")), (
        "holdings_note_legacy" if parse_note_sync_date(entry.get("note")) else "unknown"
    )


def _as_jst(value: datetime) -> datetime:
    jst = ZoneInfo("Asia/Tokyo")
    return value.replace(tzinfo=jst) if value.tzinfo is None else value.astimezone(jst)


_BROKER_CONFIRMATION_FIELDS = (
    "external_execution_id",
    "broker_source",
    "broker_reported_at",
    "filled_quantity",
    "filled_price",
    "reconciled_at",
    "reconciliation_snapshot_hash",
)


def is_complete_broker_confirmed_fill(row: dict, *, require_applied: bool = True) -> bool:
    """Whether this fill can extend authority without another broker CSV."""
    from execution_safety import parse_timestamp

    if not bool(row.get("broker_confirmed_filled")):
        return False
    if any(row.get(field) in (None, "") for field in _BROKER_CONFIRMATION_FIELDS):
        return False
    if position_identity_for_action(row) is None:
        return False
    if require_applied and not bool(row.get("portfolio_applied") or row.get("portfolio_updated")):
        return False
    try:
        if float(row.get("filled_quantity")) <= 0 or float(row.get("filled_price")) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    return (
        parse_timestamp(row.get("broker_reported_at")) is not None
        and parse_timestamp(row.get("reconciled_at")) is not None
    )


def _broker_event_authority_time(row: dict) -> datetime | None:
    from execution_safety import parse_timestamp

    for field in (
        "reconciled_at",
        "broker_reported_at",
        "executed_at_time",
        "saved_at",
    ):
        parsed = parse_timestamp(row.get(field))
        if parsed is not None:
            return _as_jst(parsed)
    return None


def _resolve_position_execution_authority(
    position: PositionIdentity,
    *,
    base_dir: Path,
    snapshot_as_of: datetime,
) -> dict:
    """Resolve post-snapshot fills into an event-based authority chain.

    A complete broker snapshot remains authoritative until a position-changing
    event occurs.  A complete Web/broker-confirmed fill that was applied to the
    local portfolio advances authority for the exact PositionIdentity.
    Unconfirmed, incomplete, unapplied, or ambiguously routed fills invalidate
    it.  This permits initial snapshot + Web delta entry without weakening the
    broker-evidence boundary.
    """
    try:
        from execution_reconciliation import (
            execution_temporal_order,
            load_effective_execution_records,
        )
        from execution_safety import is_fill_record

        rows = load_effective_execution_records(base_dir=base_dir)
    except Exception:
        return {
            "authority_as_of": _as_jst(snapshot_as_of),
            "authority_source": "holding_snapshot",
            "invalidating_event": {
                "reason": "execution_ledger_unreadable",
                "execution_id": None,
            },
        }

    candidates: list[tuple[datetime | None, dict, PositionIdentity | None]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(
            row.get("event_type")
            or row.get("status")
            or row.get("reconciliation_status")
            or ""
        ).lower()
        externally_confirmed_fill = bool(
            row.get("broker_confirmed_filled")
        ) or status in {"broker_confirmed_filled", "broker_confirmed"}
        if not (is_fill_record(row) or externally_confirmed_fill):
            continue
        if str(row.get("ticker") or "").strip().upper() != position.canonical_instrument_id:
            continue
        candidate = position_identity_for_action(row)
        if candidate is not None and candidate != position:
            continue
        candidates.append((_broker_event_authority_time(row), row, candidate))

    candidates.sort(
        key=lambda item: (
            item[0] is None,
            item[0] or datetime.max.replace(tzinfo=ZoneInfo("Asia/Tokyo")),
        )
    )
    authority_as_of = _as_jst(snapshot_as_of)
    authority_source = "holding_snapshot"
    for event_time, row, candidate in candidates:
        temporal = execution_temporal_order(row, authority_as_of.isoformat())
        if temporal.get("temporal_order") == "before_snapshot":
            continue
        if (
            candidate == position
            and event_time is not None
            and is_complete_broker_confirmed_fill(row)
            and row.get("execution_reconciliation_status") != "review"
        ):
            authority_as_of = max(authority_as_of, event_time)
            authority_source = (
                "broker_confirmed_web_fill"
                if row.get("broker_source") == "web_manual_confirmation"
                else "broker_confirmed_fill"
            )
            continue
        return {
            "authority_as_of": authority_as_of,
            "authority_source": authority_source,
            "invalidating_event": {
                "reason": str(
                    temporal.get("temporal_order") or "temporal_order_unknown"
                ),
                "execution_id": row.get("id") or row.get("action_state_id"),
                "execution_position_identity": (
                    candidate.key if candidate is not None else None
                ),
                "temporal_order": temporal,
                "broker_confirmation_complete": is_complete_broker_confirmed_fill(row),
            },
        }
    return {
        "authority_as_of": authority_as_of,
        "authority_source": authority_source,
        "invalidating_event": None,
    }


def _snapshot_invalidating_execution(
    position: PositionIdentity,
    *,
    base_dir: Path,
    snapshot_as_of: datetime,
) -> dict | None:
    """Compatibility view returning only the invalidating event."""
    return _resolve_position_execution_authority(
        position,
        base_dir=base_dir,
        snapshot_as_of=snapshot_as_of,
    )["invalidating_event"]


def position_freshness(
    position: PositionIdentity,
    *,
    base_dir: Path,
    now: datetime,
    holdings_entry: dict | None = None,
) -> dict:
    """このポジション固有の証券会社確認状態を返す。

    他銘柄の更新や無関係な約定 (action_executions.json 側のイベント) には
    一切影響されない。「鮮度を進めてよいのは broker_confirmed_filled または
    再照合済みイベントのみ」という Stage 0B の契約どおり、構造化された
    holdings の照合時刻と、完全な外部根拠を持つ約定だけを見る。自由記述 note
    の日付は旧データ互換のため明示的に legacy source として扱う。

    Returns:
        {
          "status": "fresh" | "invalidated" | "unknown",
          "synced_at": ISO-8601 timestamp | None,
          "age_hours": float | None,
          "source": str,
          "validation_mode": "event_based",
        }
    """
    entry = holdings_entry if holdings_entry is not None else _find_holding_entry(position, base_dir=base_dir)

    if entry is None:
        return {
            "status": "unknown",
            "synced_at": None,
            "age_hours": None,
            "source": "unknown",
            "validation_mode": "event_based",
        }

    holding_sync, holding_source = _parse_sync_timestamp(entry)
    if holding_sync is None:
        return {
            "status": "unknown",
            "synced_at": None,
            "age_hours": None,
            "source": "unknown",
            "validation_mode": "event_based",
        }
    synced_at, source = holding_sync, holding_source

    synced_jst = _as_jst(synced_at)
    now_jst = _as_jst(now)
    authority = _resolve_position_execution_authority(
        position,
        base_dir=base_dir,
        snapshot_as_of=synced_jst,
    )
    effective_synced_at = authority["authority_as_of"]
    age_hours = max(0.0, (now_jst - effective_synced_at).total_seconds() / 3600)
    invalidating = authority["invalidating_event"]
    status = "invalidated" if invalidating else "fresh"

    return {
        "status": status,
        "synced_at": effective_synced_at.isoformat(),
        "age_hours": round(age_hours, 1),
        "source": (
            authority["authority_source"]
            if authority["authority_source"] != "holding_snapshot"
            else source
        ),
        "validation_mode": "event_based",
        "invalidating_event": invalidating,
    }

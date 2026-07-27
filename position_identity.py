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

# 鮮度の閾値は execution_readiness.portfolio_snapshot_health() の
# file-wide 版 (72h=stale / 24h=degraded) とそろえる。ポジション単位に
# しても「何時間で危険域か」という運用上の意味は変えない。
DEGRADED_AFTER_HOURS = 24.0
STALE_AFTER_HOURS = 72.0


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
    なため、account名の「妻」表記や key の "_WIFE" サフィックスから
    推定できる場合のみ補う。それ以外は本アプリの既定運用者 (夫名義) と
    する — これは既存コードベース全体の前提 (CASH_JPY=夫, CASH_JPY_SBI_WIFE=妻)
    と揃えたものであり、本 module が新たに導入する推測ではない。
    """
    from execution_safety import canonical_account, canonical_broker, canonical_owner

    ticker = entry.get("ticker")
    account_raw = entry.get("account")
    broker_raw = entry.get("broker")
    if not ticker or not account_raw or not broker_raw:
        return None

    owner_raw = entry.get("owner")
    if not owner_raw:
        owner_raw = "wife" if ("妻" in str(account_raw) or (key and "_WIFE" in key.upper())) else "husband"

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


def position_freshness(
    position: PositionIdentity,
    *,
    base_dir: Path,
    now: datetime,
    holdings_entry: dict | None = None,
) -> dict:
    """このポジション固有の鮮度を返す。

    他銘柄の更新や無関係な約定 (action_executions.json 側のイベント) には
    一切影響されない。「鮮度を進めてよいのは broker_confirmed_filled または
    再照合済みイベントのみ」という Stage 0B の契約どおり、ここで見ているのは
    holdings.json への実際の証券会社同期記録 (note フィールド) のみ。

    Returns:
        {
          "status": "fresh" | "degraded" | "stale" | "unknown",
          "synced_at": "YYYY-MM-DD" | None,
          "age_hours": float | None,
          "source": "holdings_note" | "unknown",
        }
    """
    entry = holdings_entry if holdings_entry is not None else _find_holding_entry(position, base_dir=base_dir)

    if entry is None:
        return {"status": "unknown", "synced_at": None, "age_hours": None, "source": "unknown"}

    synced_at = parse_note_sync_date(entry.get("note"))
    if synced_at is None:
        return {"status": "unknown", "synced_at": None, "age_hours": None, "source": "unknown"}

    now_naive = now.replace(tzinfo=None) if now.tzinfo is not None else now
    age_hours = max(0.0, (now_naive - synced_at).total_seconds() / 3600)

    if age_hours > STALE_AFTER_HOURS:
        status = "stale"
    elif age_hours > DEGRADED_AFTER_HOURS:
        status = "degraded"
    else:
        status = "fresh"

    return {
        "status": status,
        "synced_at": synced_at.date().isoformat(),
        "age_hours": round(age_hours, 1),
        "source": "holdings_note",
    }

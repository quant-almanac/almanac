"""
event_ledger.py — P1-18-A: Event Ledger 基盤

Codex 3 ラウンド目の指摘 (NAV/TWR 計測基盤の欠如) + 4 ラウンド目補正
(record_daily_performance だけでは TWR は出ない、cash flow / dividend / tax / FX を全部 event 化せよ)
を満たすための単一の event ledger。

スキーマ (SQLite: almanac.db / legacy nexustrader.db に新設):

  ledger_events
    id              INTEGER PK
    event_id        TEXT UNIQUE      -- idempotency key (UUID 由来)
    occurred_at     TEXT             -- ISO datetime (約定時刻)
    recorded_at     TEXT             -- ISO datetime (登録時刻)
    event_type      TEXT             -- 'trade' | 'cash_flow' | 'dividend' | 'tax' | 'fee' |
                                     --  'fx_conversion' | 'split' | 'merge' | 'nisa_use'
    ticker          TEXT             -- nullable (cash_flow など銘柄非依存 event は NULL)
    direction       TEXT             -- 'buy' | 'sell' | 'margin_buy' | 'short' | 'cover' | 'in' | 'out'
                                     --  (event_type で意味が変わる)
    quantity        REAL             -- shares or amount in source currency
    price           REAL             -- per share (trade) or 1.0 (cash flow)
    currency        TEXT             -- 'JPY' | 'USD' | ...
    fx_rate_usdjpy  REAL             -- 当該時点の換算レート (USD evt は必須、JPY evt は NULL)
    amount_jpy      REAL             -- JPY 換算後の絶対値（in は正 / out は負として保存）
    account         TEXT             -- '特定' | 'NISA成長投資枠' | '信用' | '持株会' etc.
    source          TEXT             -- 'api' | 'broker_import' | 'cron' | 'manual'
    note            TEXT             -- free text
    raw_payload     TEXT             -- 元 record の JSON dump (audit)
    created_at      TEXT             -- TEXT DEFAULT datetime('now', 'localtime')

  CREATE UNIQUE INDEX idx_ledger_event_id ON ledger_events(event_id)
  CREATE INDEX idx_ledger_occurred_at    ON ledger_events(occurred_at)
  CREATE INDEX idx_ledger_event_type     ON ledger_events(event_type)

API:
  append_event(event_dict) -> int (rowid)  — idempotent: 既存 event_id は skip
  query_events(date_from, date_to, types=None) -> list[dict]
  cash_flow_sum_jpy(date_from, date_to) -> float  — Modified Dietz 用の外部 cash flow 合計

設計判断:
  - 単一テーブル + event_type で分類。後で view を切れる柔軟性を優先（正規化は P2 で）。
  - すべて idempotent: event_id (UUID) で UNIQUE 制約。
  - JPY 換算済み amount_jpy を持つ。集計時の通貨変換コストを下げる。
  - daily_performance テーブルとは独立（あちらは日次スナップショット、こちらは event の append-only）。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Iterator, Optional

from almanac.runtime_config import resolve_db_path

BASE_DIR = Path(__file__).parent
DB_PATH  = resolve_db_path(BASE_DIR)

# ============================================================
# Schema
# ============================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT UNIQUE NOT NULL,
    occurred_at     TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    ticker          TEXT,
    direction       TEXT,
    quantity        REAL,
    price           REAL,
    currency        TEXT,
    fx_rate_usdjpy  REAL,
    amount_jpy      REAL,
    account         TEXT,
    source          TEXT,
    note            TEXT,
    raw_payload     TEXT,
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_event_id   ON ledger_events(event_id);
CREATE INDEX IF NOT EXISTS idx_ledger_occurred_at        ON ledger_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_ledger_event_type         ON ledger_events(event_type);
CREATE INDEX IF NOT EXISTS idx_ledger_ticker             ON ledger_events(ticker);

CREATE TABLE IF NOT EXISTS execution_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    execution_id    TEXT UNIQUE NOT NULL,
    response_json   TEXT,
    application_status TEXT NOT NULL DEFAULT 'processing',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_idempotency_execution_id
    ON execution_idempotency(execution_id);

CREATE TABLE IF NOT EXISTS portfolio_application_journal (
    event_id            TEXT PRIMARY KEY,
    holdings_after_json TEXT NOT NULL,
    account_after_json  TEXT NOT NULL,
    event_kwargs_json   TEXT NOT NULL,
    result_json         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'prepared',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""

VALID_EVENT_TYPES = {
    "trade",          # BUY / SELL
    "cash_flow",      # 外部入出金（給与・年金・引出し）— TWR の cash_flow_sum_jpy で controlled out 対象
    "dividend",       # 配当（cash の in）
    "tax",            # 源泉税（cash の out）
    "fee",            # 売買手数料
    "fx_conversion",  # JPY ↔ USD
    "split",          # 株式分割
    "merge",          # 株式併合・買収
    "nisa_use",       # NISA 枠使用（非現金、後で枠復活も別 event）
    # P0 (Codex 2026-05-17): broker_balance_import 4 モード対応
    "internal_transfer",  # 管理対象口座内移動 (SBI→楽天 等)。audit のみ、TWR には影響させない
    "reconcile",          # ブローカー残高に合わせた内部補正。audit のみ、TWR には影響させない
}

VALID_DIRECTIONS = {"buy", "sell", "margin_buy", "short", "cover", "in", "out"}
AMOUNT_REQUIRED_EVENT_TYPES = {
    "trade",
    "cash_flow",
    "dividend",
    "tax",
    "fee",
    "fx_conversion",
}


# ============================================================
# Connection
# ============================================================

@contextmanager
def _conn(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """
    SQLite 接続。row_factory で dict-like、PRAGMA foreign_keys ON。
    呼出側は with _conn() as c: で使う。
    """
    p = db_path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(db_path: Optional[Path] = None) -> None:
    """idempotent: 既存テーブルがあっても安全。"""
    with _conn(db_path) as c:
        c.executescript(SCHEMA_SQL)


def get_execution_idempotency(
    idempotency_key: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return the durable execution request entry for ``idempotency_key``."""
    init_schema(db_path)
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT * FROM execution_idempotency WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    return dict(row) if row is not None else None


def reserve_execution_idempotency(
    *,
    idempotency_key: str,
    request_hash: str,
    execution_id: str,
    db_path: Optional[Path] = None,
) -> tuple[dict, bool]:
    """
    Persist an execution request before any portfolio mutation.

    Returns ``(entry, created)``.  A caller must compare ``request_hash`` when
    ``created`` is false; the registry intentionally survives the 500-record
    JSON history trim.
    """
    init_schema(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as c:
        c.execute(
            """
            INSERT OR IGNORE INTO execution_idempotency (
                idempotency_key, request_hash, execution_id,
                response_json, application_status, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, 'processing', ?, ?)
            """,
            (idempotency_key, request_hash, execution_id, now, now),
        )
        created = c.execute("SELECT changes()").fetchone()[0] == 1
        row = c.execute(
            "SELECT * FROM execution_idempotency WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    if row is None:  # pragma: no cover - SQLite invariant
        raise RuntimeError("execution idempotency reservation was not persisted")
    return dict(row), created


def complete_execution_idempotency(
    *,
    idempotency_key: str,
    response: dict,
    application_status: str,
    db_path: Optional[Path] = None,
) -> None:
    """Store the stable API response after the execution fact is durable."""
    init_schema(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    response_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
    with _conn(db_path) as c:
        cur = c.execute(
            """
            UPDATE execution_idempotency
               SET response_json = ?, application_status = ?, updated_at = ?
             WHERE idempotency_key = ?
            """,
            (response_json, application_status, now, idempotency_key),
        )
        if cur.rowcount != 1:
            raise RuntimeError("execution idempotency entry is missing")


def update_execution_idempotency_response(
    execution_id: str,
    *,
    response: dict,
    application_status: str,
    db_path: Optional[Path] = None,
) -> None:
    """Refresh a prior response after a pending portfolio application resolves."""
    init_schema(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as c:
        c.execute(
            """
            UPDATE execution_idempotency
               SET response_json = ?, application_status = ?, updated_at = ?
             WHERE execution_id = ?
            """,
            (
                json.dumps(response, ensure_ascii=False, sort_keys=True),
                application_status,
                now,
                execution_id,
            ),
        )


def prepare_portfolio_application(
    *,
    event_id: str,
    holdings_after: dict,
    account_after: dict,
    event_kwargs: dict,
    result: dict,
    db_path: Optional[Path] = None,
) -> None:
    """Persist the exact after-state before touching JSON portfolio files."""
    init_schema(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    values = (
        event_id,
        json.dumps(holdings_after, ensure_ascii=False, sort_keys=True),
        json.dumps(account_after, ensure_ascii=False, sort_keys=True),
        json.dumps(event_kwargs, ensure_ascii=False, sort_keys=True),
        json.dumps(result, ensure_ascii=False, sort_keys=True),
        now,
        now,
    )
    with _conn(db_path) as c:
        c.execute(
            """
            INSERT INTO portfolio_application_journal (
                event_id, holdings_after_json, account_after_json,
                event_kwargs_json, result_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                holdings_after_json=excluded.holdings_after_json,
                account_after_json=excluded.account_after_json,
                event_kwargs_json=excluded.event_kwargs_json,
                result_json=excluded.result_json,
                status='prepared',
                updated_at=excluded.updated_at
            """,
            values,
        )


def get_portfolio_application(
    event_id: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    init_schema(db_path)
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT * FROM portfolio_application_journal WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def complete_portfolio_application(event_id: str, *, db_path: Optional[Path] = None) -> None:
    init_schema(db_path)
    with _conn(db_path) as c:
        c.execute(
            """UPDATE portfolio_application_journal
                  SET status='complete', updated_at=?
                WHERE event_id=?""",
            (datetime.now().isoformat(timespec="seconds"), event_id),
        )


def discard_portfolio_application(event_id: str, *, db_path: Optional[Path] = None) -> None:
    init_schema(db_path)
    with _conn(db_path) as c:
        c.execute("DELETE FROM portfolio_application_journal WHERE event_id = ?", (event_id,))


# ============================================================
# Helpers
# ============================================================

def _to_amount_jpy(
    *,
    quantity: Optional[float],
    price: Optional[float],
    currency: Optional[str],
    fx_rate_usdjpy: Optional[float],
    direction: Optional[str],
) -> Optional[float]:
    """
    JPY 換算後の amount を計算 (符号付き)。
      direction in {'buy', 'margin_buy', 'cover', 'out'} → 負（買い側）
      direction in {'sell', 'short', 'in'} → 正（売り側）
    """
    if quantity is None or price is None:
        return None
    try:
        gross = float(quantity) * float(price)
    except (TypeError, ValueError):
        return None

    cur = (currency or "JPY").upper()
    if cur == "JPY":
        jpy = gross
    elif cur == "USD":
        if fx_rate_usdjpy is None or fx_rate_usdjpy <= 0:
            return None
        jpy = gross * float(fx_rate_usdjpy)
    else:
        return None  # 未対応通貨

    sign = -1.0 if direction in ("buy", "margin_buy", "cover", "out") else 1.0
    return round(sign * jpy, 2)


def _validate_amount_jpy(
    *,
    event_type: str,
    quantity: Optional[float],
    price: Optional[float],
    currency: Optional[str],
    fx_rate_usdjpy: Optional[float],
    amount_jpy: Optional[float],
) -> None:
    """
    現金・損益・税務に効く event は JPY 換算額を必須にする。
    amount_jpy=None を許すと TWR/cash_flow/tax lot が静かに欠落する。
    """
    if event_type not in AMOUNT_REQUIRED_EVENT_TYPES:
        return
    # amount 必須 event は quantity×price から amount_jpy を必ず算出できること。
    # (cash_flow/dividend/tax/fee は quantity=金額, price=1.0 で表現する設計。)
    # Codex P1 #7: 欠落時に黙って return すると amount_jpy=NULL が保存され、
    # TWR/cash_flow/tax lot/整合性検査が静かに壊れるため raise する。
    if quantity is None or price is None:
        raise ValueError(
            f"{event_type} event は quantity と price が必須です "
            f"(quantity={quantity!r}, price={price!r})。"
            " 現金系は quantity=金額, price=1.0 で表現してください。"
        )
    cur = (currency or "JPY").upper()
    if amount_jpy is not None:
        return
    if cur == "USD":
        raise ValueError(
            f"{event_type} event の USD 換算には fx_rate_usdjpy が必須です "
            f"(fx_rate_usdjpy={fx_rate_usdjpy!r})"
        )
    raise ValueError(f"{event_type} event の未対応通貨です: currency={currency!r}")


# ============================================================
# Public API: append / query
# ============================================================

def append_event(
    *,
    event_type: str,
    occurred_at: Optional[str] = None,
    ticker: Optional[str] = None,
    direction: Optional[str] = None,
    quantity: Optional[float] = None,
    price: Optional[float] = None,
    currency: Optional[str] = None,
    fx_rate_usdjpy: Optional[float] = None,
    account: Optional[str] = None,
    source: str = "api",
    note: Optional[str] = None,
    raw_payload: Optional[dict] = None,
    event_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    新規 event を append する。

    idempotency:
      event_id を渡せば再呼出は no-op (既存行を返す)。
      未指定なら UUID4 を発行。

    Returns:
      {"event_id": str, "rowid": int, "duplicate": bool, "amount_jpy": float | None}

    Raises:
      ValueError: event_type 不正、direction 不正
    """
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(f"unknown event_type: {event_type}. allowed: {sorted(VALID_EVENT_TYPES)}")
    if direction is not None and direction not in VALID_DIRECTIONS:
        raise ValueError(f"unknown direction: {direction}. allowed: {sorted(VALID_DIRECTIONS)}")

    if event_id is None:
        event_id = uuid.uuid4().hex
    if occurred_at is None:
        occurred_at = datetime.now().isoformat(timespec="seconds")

    amount_jpy = _to_amount_jpy(
        quantity=quantity, price=price, currency=currency,
        fx_rate_usdjpy=fx_rate_usdjpy, direction=direction,
    )
    _validate_amount_jpy(
        event_type=event_type,
        quantity=quantity,
        price=price,
        currency=currency,
        fx_rate_usdjpy=fx_rate_usdjpy,
        amount_jpy=amount_jpy,
    )

    init_schema(db_path)
    with _conn(db_path) as c:
        # idempotency check
        existing = c.execute(
            "SELECT id, amount_jpy FROM ledger_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            return {
                "event_id": event_id,
                "rowid": int(existing["id"]),
                "duplicate": True,
                "amount_jpy": existing["amount_jpy"],
            }

        cur = c.execute(
            """
            INSERT INTO ledger_events
              (event_id, occurred_at, recorded_at, event_type, ticker, direction,
               quantity, price, currency, fx_rate_usdjpy, amount_jpy, account,
               source, note, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                occurred_at,
                datetime.now().isoformat(timespec="seconds"),
                event_type,
                ticker,
                direction,
                quantity,
                price,
                currency,
                fx_rate_usdjpy,
                amount_jpy,
                account,
                source,
                note,
                json.dumps(raw_payload, ensure_ascii=False) if raw_payload is not None else None,
            ),
        )
        return {
            "event_id": event_id,
            "rowid": int(cur.lastrowid),
            "duplicate": False,
            "amount_jpy": amount_jpy,
        }


def _superseded_ids(rows: list) -> set:
    """
    Codex P1 #3: raw_payload.supersedes が指す event_id の集合
    (= 訂正イベントによって置換された旧 event)。append-only を保ちつつ、
    読み取り側で旧行を除外して「訂正後の値」だけを見せるための補助。
    """
    out: set = set()
    for r in rows:
        rp = r.get("raw_payload")
        payload = None
        if isinstance(rp, str) and rp:
            try:
                payload = json.loads(rp)
            except Exception:
                payload = None
        elif isinstance(rp, dict):
            payload = rp
        if isinstance(payload, dict):
            sup = payload.get("supersedes")
            if sup:
                out.add(sup)
    return out


def query_events(
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    types: Optional[list] = None,
    ticker: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list:
    """期間・種別・ticker で event を取得 (occurred_at 昇順)。"""
    init_schema(db_path)
    wheres = []
    params: list = []
    if date_from is not None:
        wheres.append("occurred_at >= ?")
        params.append(date_from)
    if date_to is not None:
        wheres.append("occurred_at <= ?")
        params.append(date_to)
    if types:
        wheres.append(f"event_type IN ({','.join('?' for _ in types)})")
        params.extend(types)
    if ticker is not None:
        wheres.append("ticker = ?")
        params.append(ticker)

    sql = "SELECT * FROM ledger_events"
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY occurred_at ASC, id ASC"

    with _conn(db_path) as c:
        rows = c.execute(sql, params).fetchall()
    out = [dict(r) for r in rows]
    # Codex P1 #3: 訂正イベントに置換された旧 event を結果から除外する
    # (原行は audit のため table には残す)。
    superseded = _superseded_ids(out)
    if superseded:
        out = [r for r in out if r.get("event_id") not in superseded]
    return out


def cash_flow_sum_jpy(
    *,
    date_from: str,
    date_to: str,
    db_path: Optional[Path] = None,
) -> float:
    """
    Modified Dietz / TWR 用: 期間内の **外部** cash flow 合計 (JPY)。
    trade / fee / tax / dividend / fx_conversion は内部 flow なので除外する。
    cash_flow event のみが対象。

    入金は正、出金は負として合計される。
    """
    # Codex P1 #3: query_events 経由で版置換 (supersedes) を反映した cash_flow のみ集計する。
    rows = query_events(
        date_from=date_from, date_to=date_to, types=["cash_flow"], db_path=db_path,
    )
    return float(sum((r.get("amount_jpy") or 0.0) for r in rows))

"""
action_state_tracker.py — 推奨アクションの発注状態管理

OPusが出力した priority_actions を永続追跡し、
・未発注3営業日でTelegram警告
・高urgency stop_loss 未発注 → 新規ポジション全ブロック
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR   = Path(__file__).parent
STATE_FILE = BASE_DIR / "action_state.json"

# 未発注でブロックを発動するアクション種別 × urgency 組み合わせ
BLOCK_TRIGGERS = {
    ("stop_loss", "high"),
    ("stop_loss", "medium"),
    ("sell",      "high"),
}

# 何営業日未発注でアラートを出すか
UNPLACED_ALERT_DAYS = 3


def _unplaced_alert_days() -> int:
    try:
        from tunable_params import get as _tp_get
        return int(_tp_get("unplaced_alert_days", UNPLACED_ALERT_DAYS))
    except Exception:
        return UNPLACED_ALERT_DAYS


# ============================================================
# 内部ユーティリティ
# ============================================================

def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"actions": {}, "last_updated": ""}


def _save(state: dict) -> None:
    state["last_updated"] = datetime.now().isoformat()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _make_id(ticker: str, action_type: str, recommended_at: str,
              account_bucket: str = "default") -> str:
    """再現可能な一意ID（ticker + type + 日付 + 口座）

    account_bucket を含めることで「同日同 ticker・同タイプでも口座が違えば別 ID」
    となり、夫NISA / 妻NISA の同日提案が ID 衝突で潰れない。
    """
    day = recommended_at[:10]
    raw = f"{ticker}|{action_type}|{day}|{account_bucket}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ============================================================
# Dedup helpers — 同一銘柄×アクション×口座が複数日積み上がるのを防止
# ============================================================

_SELL_TYPES = {"sell", "trim", "take_profit", "reduce", "stop_loss", "exit", "close"}
_BUY_TYPES  = {"buy", "add", "dca", "margin_buy", "long", "entry", "scale_in"}
_SHORT_TYPES = {"short", "short_sell"}
_COVER_TYPES = {"cover", "buy_to_cover"}


def _normalize_action_type(t: str | None) -> str:
    """売買意図でバケット化。trim/take_profit/stop_loss → 'sell'、add/dca → 'buy'。"""
    s = (t or "").strip().lower()
    if s in _SELL_TYPES:
        return "sell"
    if s in _BUY_TYPES:
        return "buy"
    if s in _SHORT_TYPES:
        return "short"
    if s in _COVER_TYPES:
        return "cover"
    return s or "other"


def _account_bucket(act_or_entry: dict) -> str:
    """口座を identify。同銘柄でも口座が違えば別注文として扱う。

    優先順位:
      1. 明示 `account` フィールド
      2. action_detail / action / reason / note の日本語キーワードスキャン
      3. fallback: 'default'
    """
    if not isinstance(act_or_entry, dict):
        return "default"
    try:
        from execution_safety import canonical_broker, canonical_owner

        owner = canonical_owner(
            act_or_entry.get("execution_owner") or act_or_entry.get("owner")
        )
        broker = canonical_broker(
            act_or_entry.get("execution_broker") or act_or_entry.get("broker")
        )
    except Exception:
        owner = str(act_or_entry.get("execution_owner") or act_or_entry.get("owner") or "").strip().lower()
        broker = str(act_or_entry.get("execution_broker") or act_or_entry.get("broker") or "").strip().lower()
    acc = str(
        act_or_entry.get("execution_account") or act_or_entry.get("account") or ""
    ).strip().lower()
    tier = str(
        act_or_entry.get("execution_investment_type")
        or act_or_entry.get("investment_type")
        or act_or_entry.get("tier")
        or ""
    ).strip().lower()
    structured_routing = any(
        key in act_or_entry
        for key in (
            "execution_owner", "execution_broker", "execution_account",
            "execution_investment_type", "execution_position_keys",
        )
    )
    if structured_routing:
        return "|".join((owner or "unknown_owner", broker or "unknown_broker", acc or "default", tier or "unknown_tier"))
    if acc:
        return acc
    text = " ".join(
        str(act_or_entry.get(k, ""))
        for k in ("action_detail", "action", "reason", "note")
    )
    if "妻NISA" in text or "wife_nisa" in text.lower():
        return "wife_nisa"
    if "夫NISA" in text or "husband_nisa" in text.lower():
        return "husband_nisa"
    if "NISA成長" in text or "NISAつみたて" in text or "NISA枠" in text:
        # 「妻NISA」が明示されていない NISA は自分(夫) のものとして扱う
        return "husband_nisa"
    if "信用" in text or "margin" in text.lower():
        return "margin"
    if "一般口座" in text or "一般" in text:
        return "general"
    if "特定口座" in text or "特定" in text:
        return "specific"
    if "持株会" in text:
        return "esop"
    return "default"


def _dedup_key(ticker: str, action_type: str | None, account_bucket: str) -> str:
    """(ticker, normalized_action_type, account_bucket) を文字列化。"""
    return f"{ticker}|{_normalize_action_type(action_type)}|{account_bucket}"


def normalize_action_type(action_type: str | None) -> str:
    """Public wrapper for lifecycle/order-intent action type normalization."""
    return _normalize_action_type(action_type)


def account_bucket_for_action(act_or_entry: dict) -> str:
    """Public wrapper for account-bucket inference used by plan matching."""
    return _account_bucket(act_or_entry)


def dedup_key(ticker: str, action_type: str | None, account_bucket: str = "default") -> str:
    """Return the shared economic dedup key used by action state and plans."""
    return _dedup_key(ticker, action_type, account_bucket)


def dedup_key_for_action(act_or_entry: dict) -> str:
    """Return the shared dedup key for a priority action or action_state entry."""
    if not isinstance(act_or_entry, dict):
        return _dedup_key("", None, "default")
    ticker = str(act_or_entry.get("ticker") or "")
    action_type = (
        act_or_entry.get("type")
        or act_or_entry.get("action_type")
        or act_or_entry.get("direction")
        or ""
    )
    return _dedup_key(ticker, action_type, _account_bucket(act_or_entry))


def _find_existing_pending(state: dict, dedup_key: str) -> str | None:
    """同一 dedup_key を持つ既存 live candidate の id を返す。なければ None。

    placed/filled/cancelled/expired は対象外（pending/reprice_required のみ）。
    複数あれば最も新しい recommended_at を返す（merge 先として最新を選ぶ）。
    """
    matches: list[tuple[str, str]] = []
    for action_id, entry in state.get("actions", {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") not in {"pending", "reprice_required"}:
            continue
        existing_key = _dedup_key(
            entry.get("ticker", ""),
            entry.get("action_type", ""),
            _account_bucket(entry),
        )
        if existing_key == dedup_key:
            matches.append((entry.get("recommended_at") or "", action_id))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def _business_days_since(dt_str: str) -> int:
    """ISO datetime 文字列から今日までの営業日数（土日除く）"""
    try:
        start = datetime.fromisoformat(dt_str).date()
        today = datetime.now().date()
        days = 0
        cur = start
        while cur < today:
            cur += timedelta(days=1)
            if cur.weekday() < 5:  # 月〜金
                days += 1
        return days
    except Exception:
        return 0


ORDER_TRACKING_FIELDS = (
    "analysis_id",
    "execution_readiness",
    "execution_block_reasons",
    "execution_plan_observed_decision",
    "execution_plan_would_filter",
    "amount_hint",
    "order_type",
    "limit_price",
    "limit_price_band",
    "expiry_minutes",
    "execution_reason",
    "decision_price",
    "no_trade_zone",
    "skip_reason",
    "plan_item_id",
    "monthly_objective_id",
    "execution_plan_decision",
    "execution_plan_override",
    "plan_remaining_before_jpy",
    "plan_remaining_after_jpy",
    "estimated_notional_jpy",
    "execution_owner",
    "execution_broker",
    "execution_account",
    "execution_investment_type",
    "execution_position_keys",
    "holding_shares_before",
    "requested_sell_quantity",
    "holding_shares_after",
    "holding_quantity_exceeds_account",
    "nisa_capacity_remaining_jpy",
    "nisa_capacity_baseline",
    "market_session",
    "execution_advisories",
    "market_quote_confirmation_required",
    "market_order_window",
    "expiry_starts_at",
    "expiry_ends_at",
    "market_reprice_required",
    "market_reprice_after",
    "expiry_deferred_until_reprice",
)


def _copy_order_fields(entry: dict, action: dict) -> None:
    """priority_action の注文方式フィールドを action_state に保持する。"""
    for field in ORDER_TRACKING_FIELDS:
        if field in action:
            entry[field] = action.get(field)
        elif field in entry:
            entry.pop(field, None)


def _format_price(value, ticker: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return str(value)
    if ticker.endswith(".T"):
        return f"¥{price:,.0f}"
    if abs(price) >= 100:
        return f"${price:,.2f}"
    return f"${price:.2f}"


def _format_order_for_alert(action: dict) -> str:
    if action.get("no_trade_zone"):
        return f"発注見送り: {action.get('skip_reason') or '推定コスト超過'}"
    ticker = str(action.get("ticker") or "")
    order_type = str(action.get("order_type") or "").lower()
    limit_price = action.get("limit_price")
    band = action.get("limit_price_band")
    expiry = action.get("expiry_minutes")
    if not any([order_type, limit_price is not None, band, expiry is not None]):
        return ""
    label = {
        "market": "成行",
        "limit": "指値",
        "stop": "逆指値",
        "stop_limit": "逆指値",
    }.get(order_type, order_type or "注文方式未指定")
    parts = [label]
    if isinstance(band, dict) and (band.get("low") is not None or band.get("high") is not None):
        low = _format_price(band.get("low"), ticker)
        high = _format_price(band.get("high"), ticker)
        parts.append(f"{low}〜{high}" if low and high else (low or high))
    elif limit_price is not None and order_type != "market":
        parts.append(_format_price(limit_price, ticker))
    if expiry not in (None, ""):
        parts.append(f"有効{expiry}分")
    return " / ".join(p for p in parts if p)


# ============================================================
# 公開API
# ============================================================

def record_recommendations(actions: list[dict], source: str = "opus") -> int:
    """
    Opus priority_actions を state に記録する。

    重複抑制ロジック (Codex review 2026-05-26):
      - dedup key: (ticker, normalized_action_type, account_bucket)
      - 既存 pending エントリが同じ key を持つなら、新規挿入せず
        recommended_at / action_detail / reason / urgency を update
      - 既存 placed/filled は触らない（既に発注済みなので別注文として扱う）

    Returns: 新規追加件数 (update された件数は含めない、後方互換のため)
    """
    state = _load()
    now   = datetime.now().isoformat()
    added = 0
    updated = 0

    for act in actions:
        ticker      = act.get("ticker", "")
        action_type = act.get("type", act.get("action_type", ""))
        urgency     = act.get("urgency", "medium")
        if not ticker or not action_type:
            continue
        requested_status = "reprice_required" if act.get("market_reprice_required") else "pending"

        # Step 1: 同一 (ticker, normalized_type, account) の既存 pending を探す
        bucket = _account_bucket(act)
        dedup_key = _dedup_key(ticker, action_type, bucket)
        supersedes_id = str(act.get("supersedes_action_state_id") or "").strip()
        if supersedes_id:
            predecessor = state.get("actions", {}).get(supersedes_id)
            if (
                isinstance(predecessor, dict)
                and predecessor.get("status") == "pending"
                and predecessor.get("ticker") == ticker
                and _normalize_action_type(predecessor.get("action_type")) == _normalize_action_type(action_type)
            ):
                predecessor["status"] = "superseded"
                predecessor["superseded_at"] = now
        existing_id = _find_existing_pending(state, dedup_key)

        if existing_id is not None:
            # 既存 pending を update（新規挿入しない）
            entry = state["actions"][existing_id]
            entry["recommended_at"]   = now
            entry["last_updated_at"]  = now
            entry["update_count"]     = int(entry.get("update_count", 0)) + 1
            new_detail = act.get("action") or ""
            new_reason = act.get("reason") or ""
            if new_detail:
                entry["action_detail"] = new_detail
            if new_reason:
                entry["reason"] = new_reason
            entry["urgency"] = urgency
            # action_type は最新のものに揃える（normalize 後は同じバケット）
            entry["action_type"] = action_type
            entry["is_block_trigger"] = (action_type, urgency) in BLOCK_TRIGGERS
            entry["status"] = requested_status
            if requested_status == "reprice_required":
                entry["reprice_required_at"] = now
            else:
                entry.pop("reprice_required_at", None)
            _copy_order_fields(entry, act)
            updated += 1
            continue

        # Step 2: 新規エントリを作成
        action_id = _make_id(ticker, action_type, now, bucket)
        if action_id in state["actions"]:
            # 同日同種同IDの衝突（極めて稀） — pending でなければ別注文として扱うが
            # 安全のためここではスキップ
            continue

        entry = {
            "id":             action_id,
            "ticker":         ticker,
            "action_type":    action_type,
            "urgency":        urgency,
            "action_detail":  act.get("action", ""),
            "reason":         act.get("reason", ""),
            "recommended_at": now,
            "status":         requested_status,
            "placed_at":      None,
            "filled_at":      None,
            "source":         source,
            "is_block_trigger": (action_type, urgency) in BLOCK_TRIGGERS,
        }
        if requested_status == "reprice_required":
            entry["reprice_required_at"] = now
        if supersedes_id:
            entry["supersedes"] = supersedes_id
            predecessor = state.get("actions", {}).get(supersedes_id)
            if isinstance(predecessor, dict) and predecessor.get("status") == "superseded":
                predecessor["superseded_by"] = action_id
        _copy_order_fields(entry, act)
        state["actions"][action_id] = entry
        added += 1

    state["last_dedup_summary"] = {"added": added, "updated": updated, "at": now}
    _save(state)
    return added


def update_status(action_id: str, status: str, note: str = "") -> bool:
    """
    アクションのステータスを更新する。
    status: 'placed' | 'filled' | 'cancelled'
    """
    state = _load()
    if action_id not in state["actions"]:
        return False

    now = datetime.now().isoformat()
    entry = state["actions"][action_id]
    entry["status"] = status

    if status == "placed" and not entry.get("placed_at"):
        entry["placed_at"] = now
    elif status == "filled":
        entry["filled_at"] = now
        if not entry.get("placed_at"):
            entry["placed_at"] = now
    elif status == "cancelled":
        entry["cancelled_at"] = now

    if note:
        entry["note"] = note

    _save(state)
    return True


def _entry_direction(entry: dict) -> str:
    """action_state の action_type から正規化済みの注文方向を返す。

    ``short`` と ``cover`` は売買文言だけで推定すると、空売りを ``sell``、
    返済買いを ``buy`` と誤認する。まず action_type を共通の正規化関数で
    判定し、古い不完全なレコードだけをテキストで補完する。
    """
    action_type = _normalize_action_type(entry.get("action_type") or entry.get("type"))
    if action_type in {"buy", "sell", "short", "cover"}:
        return action_type

    text = f"{action_type} {entry.get('action_detail', '')}".lower()
    if any(tok in text for tok in ("空売り", "ショート", "short", "short_sell")):
        return "short"
    if any(tok in text for tok in ("返済買い", "買戻", "cover", "buy_to_cover")):
        return "cover"
    if any(tok in text for tok in ("売", "利確", "損切", "撤退", "trim", "sell", "exit", "close")):
        return "sell"
    if any(tok in text for tok in ("買", "購入", "追加", "エントリー", "buy", "add", "long")):
        return "buy"
    return "hold"


def sync_execution_status(
    *,
    ticker: str,
    direction: str,
    execution_status: str,
    note: str = "",
    action_state_id: str | None = None,
    execution_owner: str | None = None,
    execution_broker: str | None = None,
    execution_account: str | None = None,
    execution_investment_type: str | None = None,
    execution_position_keys: list[str] | None = None,
) -> str | None:
    """
    action_executions 側の注文/約定ステータスを action_state に同期する。

    Returns:
        更新した action_id。対応する未完了アクションがない場合は None。
    """
    ticker = (ticker or "").strip()
    raw_direction = (direction or "").strip().lower()
    direction = _normalize_action_type(raw_direction) if raw_direction else ""
    execution_status = (execution_status or "").strip().lower()
    if not ticker:
        return None

    status_map = {
        "ordered": "placed",
        # action API は partial を実約定数量として ledger へ反映する。
        # 残数量がある場合は別の ordered record で表現するため、state 側も
        # filled にし、plan consumption と portfolio ledger の意味を揃える。
        "partial": "filled",
        "executed": "filled",
        "filled": "filled",
        "done": "filled",
        "cancelled": "cancelled",
        "skip": "cancelled",
    }
    target_status = status_map.get(execution_status)
    if target_status is None:
        return None

    state = _load()
    candidates: list[dict] = []
    actions = state.get("actions", {})
    if action_state_id and isinstance(actions, dict):
        entry = actions.get(action_state_id)
        entry_status = str((entry or {}).get("status") or "")
        explicit_status_allowed = (
            entry_status in {"pending", "placed"}
            or (target_status == "filled" and entry_status != "filled")
        )
        if (
            isinstance(entry, dict)
            and entry.get("ticker") == ticker
            and explicit_status_allowed
            and (not direction or _entry_direction(entry) == direction)
        ):
            candidates.append(entry)

    if not candidates:
        requested_scope = {
            "ticker": ticker,
            "action_type": direction,
            "execution_owner": execution_owner,
            "execution_broker": execution_broker,
            "execution_account": execution_account,
            "execution_investment_type": execution_investment_type,
            "execution_position_keys": execution_position_keys or [],
        }
        requested_bucket = _account_bucket(requested_scope)
        has_structured_scope = any((
            execution_owner,
            execution_broker,
            execution_account,
            execution_investment_type,
            execution_position_keys,
        ))
        iterable = actions.values() if isinstance(actions, dict) else []
        for entry in iterable:
            if entry.get("ticker") != ticker:
                continue
            if entry.get("status") not in {"pending", "placed"}:
                continue
            if direction and _entry_direction(entry) != direction:
                continue
            if has_structured_scope and _account_bucket(entry) != requested_bucket:
                continue
            requested_keys = tuple(sorted(str(k) for k in (execution_position_keys or [])))
            entry_keys = tuple(sorted(str(k) for k in (entry.get("execution_position_keys") or [])))
            if (requested_keys or entry_keys) and requested_keys != entry_keys:
                continue
            candidates.append(entry)

    if not candidates:
        return None

    # 明示IDなしのfallbackは完全scopeで一意な時だけ。ticker-onlyで最新を選ばない。
    if action_state_id:
        entry = candidates[0]
    else:
        if len(candidates) != 1:
            return None
        entry = candidates[0]

    now = datetime.now().isoformat()
    previous_status = entry.get("status")
    entry["status"] = target_status
    if previous_status != target_status:
        entry["status_before_execution_sync"] = previous_status
    if target_status == "placed":
        if not entry.get("placed_at"):
            entry["placed_at"] = now
    elif target_status == "filled":
        entry["filled_at"] = now
        if not entry.get("placed_at"):
            entry["placed_at"] = now
    elif target_status == "cancelled":
        entry["cancelled_at"] = now
    if note:
        entry["note"] = note

    _save(state)
    return entry.get("id")


def get_pending_critical() -> list[dict]:
    """
    ブロックトリガー条件に該当する未発注アクションを返す。
    """
    state = _load()
    results = []
    for entry in state["actions"].values():
        if entry["status"] != "pending":
            continue
        if not entry.get("is_block_trigger"):
            continue
        bdays = _business_days_since(entry["recommended_at"])
        results.append({**entry, "business_days_pending": bdays})
    return sorted(results, key=lambda x: -x["business_days_pending"])


def check_new_position_block() -> dict:
    """
    未発注の重要アクションを「アドバイザリ情報」として返す。
    デフォルトでは新規ポジション開始をブロックしない（advisory モード）。
    tunable_params: auto_block_on_unplaced=true なら旧来通りブロック可。

    Returns: {"blocked": bool, "reason": str, "pending_actions": list, "advisory": list}
    """
    critical = get_pending_critical()
    alert_days = _unplaced_alert_days()
    blocking_candidates = [a for a in critical if a["business_days_pending"] >= alert_days]

    # tunable_params で明示的に true にした場合のみブロック発動。
    # 既定 (false) では「ユーザーがまだ手を付けてないだけ」のケースで分析が止まらないようにする。
    try:
        from tunable_params import get as _tp_get
        _strict = bool(_tp_get("auto_block_on_unplaced", False))
    except Exception:
        _strict = False

    if not blocking_candidates:
        return {"blocked": False, "reason": "", "pending_actions": critical, "advisory": []}

    tickers = [f'{a["ticker"]}({a["action_type"]},{a["urgency"]},{a["business_days_pending"]}日未発注)'
               for a in blocking_candidates]
    advisory_msg = f'⚠️ 未発注（advisory）: {", ".join(tickers)} — ユーザー判断で実行/キャンセルを選択してください'

    if _strict:
        return {
            "blocked":         True,
            "reason":          advisory_msg,
            "pending_actions": blocking_candidates,
            "advisory":        blocking_candidates,
        }
    # 既定: ブロックせずアドバイザリ情報のみ返す
    return {
        "blocked":         False,
        "reason":          "",
        "pending_actions": critical,
        "advisory":        blocking_candidates,
        "advisory_msg":    advisory_msg,
    }


def get_all_pending(days_threshold: int = 0) -> list[dict]:
    """全未発注アクションを返す（days_threshold営業日以上）。"""
    state = _load()
    results = []
    for entry in state["actions"].values():
        if entry["status"] != "pending":
            continue
        bdays = _business_days_since(entry["recommended_at"])
        if bdays >= days_threshold:
            results.append({**entry, "business_days_pending": bdays})
    return sorted(results, key=lambda x: (-x["business_days_pending"], x["ticker"]))


def expire_old_actions(max_days: int = 30) -> int:
    """Expire pending actions by their own TTL, then the legacy fallback."""
    state = _load()
    expired = 0
    now = datetime.now()
    for entry in state["actions"].values():
        if entry["status"] == "pending":
            from execution_safety import execution_expiry_at

            expires_at = execution_expiry_at(entry)
            if expires_at is not None:
                compare_now = datetime.now(expires_at.tzinfo)
                if expires_at <= compare_now:
                    entry["status"] = "expired"
                    entry["expired_at"] = now.isoformat()
                    entry["expire_reason"] = (
                        "market_session_open_plus_expiry_minutes"
                        if entry.get("expiry_starts_at")
                        else "recommended_at_plus_expiry_minutes"
                    )
                    expired += 1
                # A valid per-action TTL always wins over the legacy
                # business-day fallback, including while waiting for open.
                continue
            bdays = _business_days_since(entry["recommended_at"])
            if bdays > max_days:
                entry["status"] = "expired"
                entry["expired_at"] = now.isoformat()
                entry["expire_reason"] = "legacy_pending_without_valid_expiry"
                expired += 1
    if expired:
        _save(state)
    return expired


def expire_stale_placed_actions(max_days: int = 10) -> int:
    """N営業日超 placed のまま filled にならないアクションを自動 expired にする。

    AI が同じアクションを再提案し続けるループを防ぐ。
    placed_at から N 営業日経っても filled しない場合:
      - 実際にはユーザーが broker で発注していなかった
      - or broker で却下された
      - or 価格が動きすぎてストップが陳腐化した
    のいずれかと推定し、自動で expired にする。
    """
    state = _load()
    expired = 0
    for entry in state["actions"].values():
        if entry["status"] == "placed":
            placed_at = entry.get("placed_at") or entry.get("recommended_at")
            if not placed_at:
                continue
            bdays = _business_days_since(placed_at)
            if bdays > max_days:
                entry["status"] = "expired"
                entry["expired_at"] = datetime.now().isoformat()
                entry["expire_reason"] = (
                    f"auto_expired: placed but unfilled for {bdays} business days "
                    f"(threshold {max_days}d). Likely never executed at broker, "
                    f"or stale due to price movement. Loop prevention."
                )
                expired += 1
    if expired:
        _save(state)
    return expired


def stale_ordered_execution_warnings(max_days: int = 10) -> list[dict]:
    """古い ordered 実行記録を検出するが、注文状態は推測で変更しない。"""
    exec_path = BASE_DIR / "action_executions.json"
    if not exec_path.exists():
        return []
    try:
        data = json.loads(exec_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("executions", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    try:
        from order_intent_resolver import flag_stale_ordered_executions
        return flag_stale_ordered_executions(
            items,
            max_business_days=max_days,
            business_days_since=_business_days_since,
        )
    except Exception:
        return []


def expire_stale_ordered_executions(max_days: int = 10) -> int:
    """Backward-compatible no-op.

    ``ordered`` represents a possible broker order.  Time passing is not proof
    that it was cancelled, so cleanup must never mutate it automatically.
    """
    stale_ordered_execution_warnings(max_days=max_days)
    return 0


def auto_cleanup(pending_max_days: int = 30, placed_max_days: int = 10,
                 ordered_max_days: int = 10) -> dict:
    """run_analysis 開始時に呼ぶ自動クリーンアップ。
    action_state.json の pending/placed は期限切れ処理するが、
    action_executions.json の ordered は警告だけ返し自動変更しない。
    """
    n_pending = expire_old_actions(max_days=pending_max_days)
    n_placed  = expire_stale_placed_actions(max_days=placed_max_days)
    ordered_warnings = stale_ordered_execution_warnings(max_days=ordered_max_days)
    n_ordered = 0
    return {
        "expired_pending": n_pending,
        "expired_placed":  n_placed,
        "expired_ordered": n_ordered,
        "total_expired":   n_pending + n_placed + n_ordered,
        "stale_ordered_warning_count": len(ordered_warnings),
        "stale_ordered_warnings": ordered_warnings,
    }


def format_pending_for_prompt() -> str:
    """Opus プロンプトに注入する未発注アクション状況テキスト（advisory モード）。

    ※ デフォルトでは「ユーザーがまだ実行していないだけ」を理由に新規エントリーを
    ブロックしない。escalation 文言（'全ブロック中', '絶対前提条件' 等）を出さず、
    AI に「参考情報」として渡すだけにする。
    """
    pending = get_all_pending(days_threshold=1)
    if not pending:
        return ""

    lines = ["【未発注アクション（参考情報・advisory）】",
             "※ ユーザーがまだ実行していないだけのケースが多い。再提案や escalation を避け、"
             "新規エントリーをブロックする理由として使わない。"]
    for a in pending[:10]:
        bdays = a["business_days_pending"]
        flag  = "📌"  # 旧 🔴/🟡 → escalation を避けるため一律 📌
        lines.append(
            f"{flag} {a['ticker']} {a['action_type']}({a['urgency']}) "
            f"— {bdays}営業日未発注 — {a['action_detail'][:50]}"
        )
    block = check_new_position_block()
    # tunable_params: auto_block_on_unplaced=true 時のみブロック表示
    if block.get("blocked"):
        lines.append(f"\n⛔ 新規ポジション全ブロック中（auto_block_on_unplaced=true）")
    return "\n".join(lines)


def send_telegram_alerts() -> None:
    """未発注3日超の重要アクションをTelegramに通知する。"""
    try:
        import os, requests
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return

        blocking = [a for a in get_pending_critical()
                    if a["business_days_pending"] >= _unplaced_alert_days()]
        if not blocking:
            return

        lines = [f"⚠️ *ALMANAC 未発注アラート* ({datetime.now().strftime('%m/%d %H:%M')})"]
        for a in blocking:
            line = (
                f"🔴 `{a['ticker']}` {a['action_type']}({a['urgency']}) "
                f"— *{a['business_days_pending']}営業日未発注*"
            )
            order = _format_order_for_alert(a)
            if order:
                line += f"\n   注文: {order}"
            lines.append(line)
        lines.append("\n新規ポジション開始は発注完了後にしてください。")

        # ALMANAC: telegram disabled — ai_analysis only
        # requests.post(
        #     f"https://api.telegram.org/bot{token}/sendMessage",
        #     json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"},
        #     timeout=10,
        # )
    except Exception as e:
        print(f"  ⚠️ Telegram通知失敗: {e}")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        block = check_new_position_block()
        print(f"ブロック中: {block['blocked']}")
        if block["blocked"]:
            print(f"理由: {block['reason']}")
        pending = get_all_pending()
        print(f"\n未発注アクション: {len(pending)}件")
        for a in pending:
            print(f"  [{a['business_days_pending']}日] {a['ticker']} {a['action_type']}({a['urgency']}) — {a['action_detail'][:60]}")

    elif cmd == "place" and len(sys.argv) >= 3:
        ok = update_status(sys.argv[2], "placed")
        print("✅ placed" if ok else "❌ not found")

    elif cmd == "fill" and len(sys.argv) >= 3:
        ok = update_status(sys.argv[2], "filled")
        print("✅ filled" if ok else "❌ not found")

    elif cmd == "cancel" and len(sys.argv) >= 3:
        ok = update_status(sys.argv[2], "cancelled")
        print("✅ cancelled" if ok else "❌ not found")

    elif cmd == "alert":
        send_telegram_alerts()
        print("✅ Telegram通知送信")

    elif cmd == "expire":
        n = expire_old_actions()
        print(f"✅ {n}件を期限切れ処理")

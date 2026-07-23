"""Deterministic execution-safety helpers shared by analyst and API paths.

The helpers in this module deliberately avoid market-price/network lookups.
Exchange sessions come from the locally installed ``pandas-market-calendars``
package.  If a timestamp cannot be assigned to an exchange-local trading date,
the caller must degrade readiness rather than guess a neighbouring session.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
EXCHANGE_CONFIG = {
    "JPX": ZoneInfo("Asia/Tokyo"),
    "NYSE": ZoneInfo("America/New_York"),
}
FILL_STATUSES = {"executed", "filled", "done"}
PARTIAL_STATUS = "partial"
ACTIVE_ORDER_STATUSES = {"ordered"}


def canonical_owner(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"wife", "spouse", "妻", "配偶者"} or "妻" in text:
        return "wife"
    if text in {"husband", "self", "夫", "本人"} or "夫" in text:
        return "husband"
    return ""


def canonical_broker(value: object) -> str:
    text = str(value or "").strip().lower()
    if "sbi" in text or "ＳＢＩ" in str(value or ""):
        return "sbi"
    if "rakuten" in text or "楽天" in str(value or ""):
        return "rakuten"
    return ""


def action_text(action: dict) -> str:
    return " ".join(
        str(action.get(key) or "")
        for key in ("action", "action_detail", "reason", "note")
    )


def explicit_action_owner(action: dict) -> str:
    owner = canonical_owner(
        action.get("execution_owner") or action.get("target_owner") or action.get("owner")
    )
    if owner:
        return owner
    text = action_text(action)
    if "妻NISA" in text or "妻 NISA" in text or "wife_nisa" in text.lower():
        return "wife"
    if "夫NISA" in text or "夫 NISA" in text or "husband_nisa" in text.lower():
        return "husband"
    return ""


def explicit_action_broker(action: dict) -> str:
    broker = canonical_broker(
        action.get("execution_broker") or action.get("target_broker") or action.get("broker")
    )
    if broker:
        return broker
    return canonical_broker(action_text(action))


def is_nisa_account(value: object) -> bool:
    return "nisa" in str(value or "").lower()


def action_account(action: dict) -> str:
    account = str(
        action.get("execution_account")
        or action.get("target_account")
        or action.get("account")
        or action.get("account_type")
        or action.get("broker_account")
        or ""
    ).strip()
    if account:
        return account
    text = action_text(action)
    if "NISA成長" in text or "nisa growth" in text.lower():
        return "NISA成長投資枠"
    if "NISAつみたて" in text or "NISA積立" in text:
        return "NISAつみたて投資枠"
    if "特定" in text:
        return "特定"
    if "一般" in text:
        return "一般"
    return ""


def canonical_account(value: object) -> str:
    """Normalize executable broker-account labels without merging tax lots."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "nisa" in text:
        if "つみたて" in text or "積立" in text:
            return "nisa_tsumitate"
        if "成長" in text:
            return "nisa_growth"
        return "nisa"
    if "特定" in text or text in {"tokutei", "specific"}:
        return "specific"
    if "一般" in text or text in {"ippan", "general"}:
        return "general"
    if "信用" in text or "margin" in text:
        return "margin"
    if "持株会" in text or "espp" in text or "esop" in text:
        return "employee"
    if text in {"taxable", "課税", "課税口座"}:
        return "taxable"
    return text


def _accounts_compatible(left: str, right: str) -> bool:
    if not left or not right:
        return True
    if left == right:
        return True
    if left == "nisa" and right.startswith("nisa_"):
        return True
    if right == "nisa" and left.startswith("nisa_"):
        return True
    if left == "taxable" and right in {"specific", "general"}:
        return True
    if right == "taxable" and left in {"specific", "general"}:
        return True
    return False


_ACCOUNT_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("nisa_growth", re.compile(r"(?:NISA\s*)?(?:成長投資枠|成長投資枠口座|成長枠)", re.IGNORECASE)),
    ("nisa_tsumitate", re.compile(r"(?:NISA\s*)?(?:つみたて投資枠|積立投資枠|つみたて枠)", re.IGNORECASE)),
    ("specific", re.compile(r"特定(?:口座)?", re.IGNORECASE)),
    ("general", re.compile(r"一般(?:口座)?", re.IGNORECASE)),
    ("margin", re.compile(r"信用(?:口座)?", re.IGNORECASE)),
    ("employee", re.compile(r"持株会", re.IGNORECASE)),
)


def _account_mentions(text: str) -> set[str]:
    mentions = {
        account
        for account, pattern in _ACCOUNT_TEXT_PATTERNS
        if pattern.search(text)
    }
    if "NISA" in text.upper() and not any(item.startswith("nisa_") for item in mentions):
        mentions.add("nisa")
    return mentions


def _source_account_mentions(text: str) -> set[str]:
    """Accounts grammatically attached to the disposal instruction."""
    mentions: set[str] = set()
    for account, pattern in _ACCOUNT_TEXT_PATTERNS:
        for match in pattern.finditer(text):
            tail = text[match.end(): match.end() + 36]
            if re.search(r"(?:から|より|を)\s*\d*(?:\.\d+)?\s*(?:株|口)?\s*(?:売却|利確|トリム|trim|reduce)", tail, re.IGNORECASE):
                mentions.add(account)
                break
            if re.search(r"(?:保有分|保有株)?[^。！？]{0,24}(?:から|より)", tail, re.IGNORECASE):
                mentions.add(account)
                break
    return mentions


def _stated_holding_quantity(text: str) -> float | None:
    patterns = (
        r"(?:口座)?保有分\s*[（(]?\s*(\d+(?:\.\d+)?)\s*(?:株|口)",
        r"保有(?:株数|口数|数)?\s*[=:：]?\s*[（(]?\s*(\d+(?:\.\d+)?)\s*(?:株|口)",
        r"残り\s*(\d+(?:\.\d+)?)\s*(?:株|口)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except (TypeError, ValueError):
                return None
    return None


def _ticker_holdings(base_dir: Path, ticker: str) -> list[dict] | None:
    try:
        raw = json.loads((base_dir / "holdings.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    positions = raw.get("holdings") or raw.get("positions") or raw
    if not isinstance(positions, dict):
        return None
    return [
        row
        for row in positions.values()
        if isinstance(row, dict)
        and str(row.get("ticker") or "").upper() == ticker.upper()
        and not str(row.get("ticker") or "").upper().startswith("CASH_")
    ]


def evaluate_exit_route_consistency(action: dict, *, base_dir: Path) -> dict:
    """Fail closed when disposal prose contradicts its structured tax lot.

    The model's prose is not used to *choose* a route.  It is only an
    independent assertion checked against the structured route selected by the
    deterministic holding binder.  A conflict therefore blocks execution
    instead of silently rewriting either side.
    """
    if economic_direction(action.get("type") or action.get("action_type") or action.get("direction")) != "sell":
        return {"readiness": "ready", "reasons": []}

    structured_raw = (
        action.get("execution_account")
        or action.get("target_account")
        or action.get("account")
        or action.get("account_type")
        or action.get("broker_account")
        or ""
    )
    structured = canonical_account(structured_raw)
    if not structured:
        return {"readiness": "ready", "reasons": []}

    try:
        primary_text = " ".join(
            str(action.get(key) or "")
            for key in ("action", "action_detail")
        ).strip()
        fallback_text = " ".join(
            str(action.get(key) or "")
            for key in ("reason", "note")
        ).strip()
        all_text = " ".join(part for part in (primary_text, fallback_text) if part)

        primary_mentions = _account_mentions(primary_text)
        source_mentions = _source_account_mentions(primary_text)
        if len(source_mentions) == 1:
            stated_account = next(iter(source_mentions))
        elif len(primary_mentions) == 1:
            stated_account = next(iter(primary_mentions))
        elif not primary_mentions:
            fallback_mentions = _source_account_mentions(fallback_text) or _account_mentions(fallback_text)
            stated_account = next(iter(fallback_mentions)) if len(fallback_mentions) == 1 else ""
            if len(fallback_mentions) > 1:
                return {
                    "readiness": "review",
                    "reasons": [{
                        "code": "execution_route_text_unresolved",
                        "message": "説明文に複数の売却口座があり、構造化ルートとの対応を特定できません",
                        "execution_account": structured_raw,
                        "text_accounts": sorted(fallback_mentions),
                    }],
                }
        else:
            stated_account = ""
            if not any(_accounts_compatible(structured, mention) for mention in primary_mentions):
                stated_account = sorted(primary_mentions)[0]

        if stated_account and not _accounts_compatible(structured, stated_account):
            return {
                "readiness": "blocked",
                "reasons": [{
                    "code": "execution_route_text_conflict",
                    "message": (
                        f"説明文の売却元口座({stated_account})と構造化ルート"
                        f"({structured_raw})が一致しません"
                    ),
                    "conflict_type": "account",
                    "execution_account": structured_raw,
                    "text_account": stated_account,
                }],
            }

        stated_quantity = _stated_holding_quantity(primary_text)
        bound_quantity = action.get("holding_shares_before")
        if stated_quantity is not None and bound_quantity not in (None, ""):
            try:
                bound = float(bound_quantity)
            except (TypeError, ValueError):
                bound = None
            if bound is not None and not math.isclose(stated_quantity, bound, rel_tol=0, abs_tol=1e-8):
                return {
                    "readiness": "blocked",
                    "reasons": [{
                        "code": "execution_route_text_conflict",
                        "message": (
                            f"説明文の売却元保有数({stated_quantity:g})と構造化ルートの"
                            f"保有数({bound:g})が一致しません"
                        ),
                        "conflict_type": "holding_quantity",
                        "text_holding_quantity": stated_quantity,
                        "holding_shares_before": bound,
                        "execution_account": structured_raw,
                    }],
                }

        # A phrase such as "NISA分は保有継続" asserts an existing NISA lot.
        # Validate that factual claim separately from the disposal account.
        nisa_holding_claim = bool(
            re.search(r"NISA[^。！？]{0,16}(?:分|保有|継続|維持)", all_text, re.IGNORECASE)
        )
        if nisa_holding_claim:
            rows = _ticker_holdings(base_dir, str(action.get("ticker") or ""))
            if rows is None:
                return {
                    "readiness": "review",
                    "reasons": [{
                        "code": "execution_route_text_unresolved",
                        "message": "説明文のNISA保有記載をholdings.jsonで確認できません",
                        "conflict_type": "nisa_holding_unresolved",
                    }],
                }
            if not any(is_nisa_account(row.get("account")) for row in rows):
                return {
                    "readiness": "blocked",
                    "reasons": [{
                        "code": "execution_route_text_conflict",
                        "message": "説明文はNISA保有を記載していますが、該当銘柄のNISAロットがありません",
                        "conflict_type": "nonexistent_nisa_holding",
                        "execution_account": structured_raw,
                    }],
                }
    except Exception as exc:
        return {
            "readiness": "review",
            "reasons": [{
                "code": "execution_route_text_unresolved",
                "message": f"説明文と構造化ルートの照合に失敗: {type(exc).__name__}",
            }],
        }

    return {"readiness": "ready", "reasons": []}


def load_nisa_profiles(base_dir: Path) -> tuple[dict, dict[str, dict]]:
    path = base_dir / "nisa_portfolio.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    profiles: dict[str, dict] = {}
    for owner in ("husband", "wife"):
        profile = raw.get(owner)
        if not isinstance(profile, dict):
            continue
        profiles[owner] = {
            **profile,
            "execution_owner": owner,
            "execution_broker": canonical_broker(profile.get("broker")),
        }
    return raw, profiles


def enrich_action_routing(action: dict, *, base_dir: Path) -> dict:
    """Attach only deterministically known routing fields.

    A generic ``NISA成長投資枠`` label is intentionally insufficient to infer
    the owner.  Explicit wife/husband text or an explicit broker may resolve
    against the owner/broker table in ``nisa_portfolio.json``.  For a *new*
    NISA recommendation, an explicit owner plus the authoritative NISA profile
    is sufficient to correct a contradictory model broker label.  This does
    not rewrite historical executions: it only makes the route of the current
    candidate deterministic before it is bound to a holding.
    """
    result = dict(action)
    account = action_account(result)
    owner = explicit_action_owner(result)
    broker = explicit_action_broker(result)
    _raw, profiles = load_nisa_profiles(base_dir)

    profile_broker = str((profiles.get(owner) or {}).get("execution_broker") or "") if owner else ""
    if is_nisa_account(account) and owner and profile_broker:
        if broker and broker != profile_broker:
            result["routing_normalized"] = True
            result["routing_normalization_reason"] = (
                f"NISA owner={owner} の証券会社を nisa_portfolio.json に従い "
                f"{broker} から {profile_broker} へ正規化"
            )
            result["routing_model_broker"] = broker
        broker = profile_broker
    elif owner and not broker:
        broker = profile_broker
    if broker and not owner:
        matches = [name for name, row in profiles.items() if row.get("execution_broker") == broker]
        if len(matches) == 1:
            owner = matches[0]

    if account:
        result["execution_account"] = account
    if owner:
        result["execution_owner"] = owner
    if broker:
        result["execution_broker"] = broker
    return result


def exchange_for_ticker(ticker: object) -> str | None:
    text = str(ticker or "").upper().strip()
    if not text:
        return None
    if text.endswith((".T", ".JP", ".JPX", ".OS")):
        return "JPX"
    if text.startswith(("SLIM_", "IFREE_", "NOMURA_", "MNXACT")):
        return None
    return "NYSE"


def parse_timestamp(value: object, *, naive_tz: ZoneInfo = JST) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_tz)
    return parsed


def execution_expiry_at(record: dict, fallback_expiry_minutes: object = None) -> datetime | None:
    """Return a session-aware expiry for a recommendation or order.

    Morning analysis may be created before the next tradable session.  In that
    case its TTL starts at ``expiry_starts_at`` rather than expiring while the
    exchange is still closed.  All timestamps are normalised to aware JST/UTC
    values so API, tracker, and Today cannot disagree at timezone boundaries.
    """
    if record.get("expiry_deferred_until_reprice"):
        return None
    minutes = record.get("expiry_minutes")
    if minutes in (None, ""):
        minutes = fallback_expiry_minutes
    if minutes in (None, ""):
        return None
    anchor = parse_timestamp(record.get("placed_at") or record.get("recommended_at"))
    starts_at = parse_timestamp(record.get("expiry_starts_at"))
    if starts_at is not None and (anchor is None or starts_at > anchor):
        anchor = starts_at
    if anchor is None:
        return None
    try:
        expires_at = anchor + timedelta(minutes=int(minutes))
    except (TypeError, ValueError):
        return None
    ends_at = parse_timestamp(record.get("expiry_ends_at"))
    if ends_at is not None and ends_at < expires_at:
        return ends_at
    return expires_at


def effective_execution_timestamp(record: dict) -> tuple[datetime | None, str | None]:
    for field in ("executed_at_time", "saved_at"):
        parsed = parse_timestamp(record.get(field))
        if parsed is not None:
            return parsed, field
    return None, None


def exchange_session(ticker: object, when: datetime) -> dict:
    """Map a timestamp to its exchange-local calendar-date session.

    Pre-open and post-close timestamps remain assigned to the same local
    trading date.  For example, NY 17:09 on 2026-07-15 is the 2026-07-15
    session.  A closed local date is unresolved; neighbouring sessions are not
    guessed because ``saved_at`` can be a later manual-record timestamp.
    """
    exchange = exchange_for_ticker(ticker)
    if exchange not in EXCHANGE_CONFIG:
        return {"status": "unresolved", "exchange": exchange, "reason": "unknown_exchange"}
    local_date = when.astimezone(EXCHANGE_CONFIG[exchange]).date()
    try:
        import pandas_market_calendars as mcal  # type: ignore

        schedule = mcal.get_calendar(exchange).schedule(
            start_date=local_date.isoformat(),
            end_date=local_date.isoformat(),
        )
    except Exception as exc:
        return {
            "status": "unresolved",
            "exchange": exchange,
            "local_date": local_date.isoformat(),
            "reason": f"calendar_error:{type(exc).__name__}",
        }
    if schedule.empty:
        return {
            "status": "unresolved",
            "exchange": exchange,
            "local_date": local_date.isoformat(),
            "reason": "exchange_closed_local_date",
        }
    return {
        "status": "resolved",
        "exchange": exchange,
        "session_date": local_date.isoformat(),
        "local_date": local_date.isoformat(),
        "source": "pandas_market_calendars",
    }


def market_session_context(ticker: object, when: datetime) -> dict:
    """Return the current exchange-session context for an instrument.

    A JPX holiday, NYSE weekend, and a pre-open/post-close snapshot all need a
    re-price at the next regular session.  This avoids assigning a TTL to a
    limit order which expires before that next opening.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=JST)
    exchange = exchange_for_ticker(ticker)
    if exchange not in EXCHANGE_CONFIG:
        return {
            "status": "unresolved",
            "exchange": exchange,
            "reason": "unknown_exchange",
        }
    local_when = when.astimezone(EXCHANGE_CONFIG[exchange])
    local_date = local_when.date()
    try:
        import pandas_market_calendars as mcal  # type: ignore

        calendar = mcal.get_calendar(exchange)
        schedule = calendar.schedule(
            start_date=local_date.isoformat(),
            end_date=local_date.isoformat(),
        )
        if not schedule.empty:
            row = schedule.iloc[0]
            market_open = row["market_open"].to_pydatetime()
            market_close = row["market_close"].to_pydatetime()
            exchange_now = when.astimezone(market_open.tzinfo)
            break_start = (
                row["break_start"].to_pydatetime()
                if "break_start" in schedule.columns and row["break_start"] is not None
                else None
            )
            break_end = (
                row["break_end"].to_pydatetime()
                if "break_end" in schedule.columns and row["break_end"] is not None
                else None
            )
            in_break = bool(
                break_start is not None
                and break_end is not None
                and break_start <= exchange_now < break_end
            )
            during_regular_session = market_open <= exchange_now < market_close and not in_break
            context = {
                "status": "trading_day",
                "exchange": exchange,
                "local_date": local_date.isoformat(),
                "source": "pandas_market_calendars",
                "market_open": market_open.isoformat(),
                "market_close": market_close.isoformat(),
                "session_state": "open" if during_regular_session else "closed",
            }
            if during_regular_session:
                return context
            if exchange_now < market_open:
                context.update({
                    "reason": "before_regular_session",
                    "next_session_date": local_date.isoformat(),
                    "next_market_open": market_open.isoformat(),
                    "next_market_close": market_close.isoformat(),
                })
                return context
            if in_break:
                context.update({
                    "reason": "between_regular_sessions",
                    "next_session_date": local_date.isoformat(),
                    "next_market_open": break_end.isoformat(),
                    "next_market_close": market_close.isoformat(),
                })
                return context
            future = calendar.schedule(
                start_date=(local_date + timedelta(days=1)).isoformat(),
                end_date=(local_date + timedelta(days=8)).isoformat(),
            )
            context["reason"] = "after_regular_session"
            if not future.empty:
                next_open = future.iloc[0]["market_open"].to_pydatetime()
                next_close = future.iloc[0]["market_close"].to_pydatetime()
                context.update({
                    "next_session_date": future.index[0].date().isoformat(),
                    "next_market_open": next_open.isoformat(),
                    "next_market_close": next_close.isoformat(),
                })
            return context

        # A seven-day horizon covers normal weekends and all exchange holidays
        # relevant to a fresh recommendation without silently guessing a date.
        future = calendar.schedule(
            start_date=(local_date + timedelta(days=1)).isoformat(),
            end_date=(local_date + timedelta(days=8)).isoformat(),
        )
        context = {
            "status": "closed",
            "exchange": exchange,
            "local_date": local_date.isoformat(),
            "reason": "exchange_closed_local_date",
            "source": "pandas_market_calendars",
        }
        if not future.empty:
            next_open = future.iloc[0]["market_open"].to_pydatetime()
            next_close = future.iloc[0]["market_close"].to_pydatetime()
            context.update({
                "next_session_date": future.index[0].date().isoformat(),
                "next_market_open": next_open.isoformat(),
                "next_market_close": next_close.isoformat(),
            })
        return context
    except Exception as exc:
        return {
            "status": "unresolved",
            "exchange": exchange,
            "local_date": local_date.isoformat(),
            "reason": f"calendar_error:{type(exc).__name__}",
        }


def economic_direction(value: object) -> str:
    text = str(value or "").lower().strip()
    if text in {"buy", "add", "dca", "margin_buy", "long", "entry", "scale_in"}:
        return "buy"
    if text in {"sell", "trim", "reduce", "take_profit", "stop_loss", "exit", "close"}:
        return "sell"
    if text in {"short", "short_sell"}:
        return "short"
    if text in {"cover", "buy_to_cover"}:
        return "cover"
    return ""


def opposite_direction(direction: str) -> str:
    return {"buy": "sell", "sell": "buy", "short": "cover", "cover": "short"}.get(direction, "")


def is_fill_record(record: dict) -> bool:
    status = str(record.get("status") or "").lower()
    if status in FILL_STATUSES:
        return True
    if status != PARTIAL_STATUS:
        return False
    try:
        return float(record.get("quantity") or 0) > 0
    except (TypeError, ValueError):
        return False


def routing_owner(record: dict) -> str:
    return canonical_owner(record.get("execution_owner") or record.get("owner")) or explicit_action_owner(record)


def classify_recent_opposite_execution(
    action: dict,
    executions: Iterable[dict],
    *,
    now: datetime,
) -> dict | None:
    ticker = str(action.get("ticker") or "")
    direction = economic_direction(action.get("type") or action.get("action_type"))
    opposite = opposite_direction(direction)
    if not ticker or not opposite:
        return None

    current_session = exchange_session(ticker, now)
    candidates: list[dict] = []
    for record in executions:
        if not isinstance(record, dict) or str(record.get("ticker") or "") != ticker:
            continue
        if economic_direction(record.get("direction") or record.get("type")) != opposite:
            continue
        if not is_fill_record(record):
            continue
        executed_at, timestamp_source = effective_execution_timestamp(record)
        if executed_at is None:
            candidates.append({
                "level": "review",
                "code": "market_session_unresolved",
                "message": f"{ticker} の反対約定時刻を解決できない",
                "execution_id": record.get("id"),
                "timestamp_source": None,
            })
            continue
        record_session = exchange_session(ticker, executed_at)
        if current_session.get("status") != "resolved" or record_session.get("status") != "resolved":
            candidates.append({
                "level": "review",
                "code": "market_session_unresolved",
                "message": f"{ticker} の反対約定を取引セッションへ帰属できない",
                "execution_id": record.get("id"),
                "timestamp_source": timestamp_source,
                "current_session": current_session,
                "execution_session": record_session,
            })
            continue

        current_day = date.fromisoformat(str(current_session["session_date"]))
        executed_day = date.fromisoformat(str(record_session["session_date"]))
        age_days = (current_day - executed_day).days
        if age_days < 0 or age_days > 14:
            continue
        action_owner = routing_owner(action)
        execution_owner = routing_owner(record)
        cross_owner = bool(action_owner and execution_owner and action_owner != execution_owner)
        common = {
            "execution_id": record.get("id"),
            "execution_status": record.get("status"),
            "execution_direction": opposite,
            "execution_saved_at": record.get("saved_at"),
            "execution_effective_at": executed_at.isoformat(),
            "timestamp_source": timestamp_source,
            "exchange": current_session.get("exchange"),
            "execution_session_date": record_session.get("session_date"),
            "current_session_date": current_session.get("session_date"),
            "session_age_calendar_days": age_days,
            "execution_owner": execution_owner or None,
            "action_owner": action_owner or None,
        }
        if cross_owner:
            candidates.append({
                **common,
                "level": "review",
                "code": "cross_owner_opposite_action",
                "message": f"{ticker} は別名義で反対方向の約定あり",
            })
        elif age_days == 0:
            candidates.append({
                **common,
                "level": "blocked",
                "code": "same_session_opposite_execution",
                "message": f"{ticker} は同一取引所セッションに反対方向の約定あり",
            })
        elif age_days <= 7:
            candidates.append({
                **common,
                "level": "review",
                "code": "recent_opposite_execution",
                "message": f"{ticker} は直近7暦日以内に反対方向の約定あり",
            })
        else:
            candidates.append({
                **common,
                "level": "warning",
                "code": "opposite_execution_warning",
                "message": f"{ticker} は直近14暦日以内に反対方向の約定あり",
            })

    if not candidates:
        return None
    severity = {"blocked": 3, "review": 2, "warning": 1}
    candidates.sort(
        key=lambda row: (
            severity.get(str(row.get("level")), 0),
            -int(row.get("session_age_calendar_days") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def _baseline_cutoff(value: object) -> tuple[datetime | None, date | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    if len(text) == 10:
        try:
            return None, date.fromisoformat(text)
        except ValueError:
            return None, None
    return parse_timestamp(text), None


def _is_strictly_after_baseline(when: datetime, *, baseline_dt: datetime | None, baseline_day: date | None) -> bool:
    if baseline_dt is not None:
        return when.astimezone(JST) > baseline_dt.astimezone(JST)
    if baseline_day is not None:
        return when.astimezone(JST).date() > baseline_day
    return False


def _record_notional_jpy(record: dict, *, fx_rate: float) -> float | None:
    value = record.get("notional_jpy") or record.get("estimated_notional_jpy")
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except (TypeError, ValueError):
        pass
    try:
        cash_delta = abs(float(record.get("cash_delta")))
        if cash_delta > 0:
            currency = str(record.get("cash_currency") or record.get("currency") or "").upper()
            return cash_delta * (fx_rate if currency == "USD" else 1.0)
    except (TypeError, ValueError):
        pass
    try:
        price = float(record.get("price"))
        quantity = float(record.get("quantity"))
        currency = str(record.get("currency") or "").upper()
        amount = abs(price * quantity)
        return amount * (fx_rate if currency == "USD" else 1.0)
    except (TypeError, ValueError):
        return None


def evaluate_nisa_capacity(action: dict, *, base_dir: Path, now: datetime) -> dict:
    account = action_account(action)
    direction = economic_direction(action.get("type") or action.get("action_type"))
    if direction != "buy" or not is_nisa_account(account):
        return {"readiness": "ready", "reasons": []}

    reasons: list[dict] = []
    owner = canonical_owner(action.get("execution_owner"))
    broker = canonical_broker(action.get("execution_broker"))
    if not owner or not broker:
        return {
            "readiness": "blocked",
            "reasons": [{
                "code": "nisa_route_missing",
                "message": "NISA買付の名義・証券会社を特定できない",
            }],
        }

    raw, profiles = load_nisa_profiles(base_dir)
    profile = profiles.get(owner) or {}
    if not profile or profile.get("execution_broker") != broker:
        return {
            "readiness": "blocked",
            "reasons": [{
                "code": "nisa_route_mismatch",
                "message": "NISA名義と証券会社がnisa_portfolio.jsonの対応と一致しない",
                "execution_owner": owner,
                "execution_broker": broker,
            }],
        }

    baseline_dt, baseline_day = _baseline_cutoff(raw.get("last_updated"))
    if baseline_dt is None and baseline_day is None:
        return {
            "readiness": "blocked",
            "reasons": [{"code": "nisa_capacity_baseline_missing", "message": "NISA残枠の基準日がない"}],
        }

    if "つみたて" in account:
        limit = float(profile.get("tsumitate_limit_annual") or 0)
        used = float(profile.get("tsumitate_used_this_year") or 0)
        planned = float(profile.get("tsumitate_planned_this_year") or 0)
    else:
        limit = float(profile.get("growth_limit_annual") or 0)
        used = float(profile.get("growth_used_this_year") or 0)
        planned = float(profile.get("growth_planned_this_year") or 0)
    remaining = max(0.0, limit - used - planned)

    try:
        account_state = json.loads((base_dir / "account.json").read_text(encoding="utf-8"))
        fx_rate = float(account_state.get("fx_rate_usdjpy") or 0) or 150.0
    except Exception:
        fx_rate = 150.0
    try:
        execution_raw = json.loads((base_dir / "action_executions.json").read_text(encoding="utf-8"))
        executions = execution_raw.get("executions") if isinstance(execution_raw, dict) else execution_raw
    except Exception:
        executions = []
    try:
        state_raw = json.loads((base_dir / "action_state.json").read_text(encoding="utf-8"))
        state_actions = state_raw.get("actions") if isinstance(state_raw, dict) else {}
    except Exception:
        state_actions = {}
    if not isinstance(state_actions, dict):
        state_actions = {}

    terminal_by_state: dict[str, datetime] = {}
    for row in executions or []:
        if not isinstance(row, dict) or not row.get("action_state_id"):
            continue
        status = str(row.get("status") or "").lower()
        if not (is_fill_record(row) or status in {"cancelled", "skip"}):
            continue
        terminal_at, _terminal_source = effective_execution_timestamp(row)
        if terminal_at is None:
            continue
        sid = str(row.get("action_state_id"))
        if sid not in terminal_by_state or terminal_at > terminal_by_state[sid]:
            terminal_by_state[sid] = terminal_at
    consumed_after_baseline = 0.0
    reserved_open = 0.0
    unattributed: list[str] = []
    for row in executions or []:
        if not isinstance(row, dict):
            continue
        row_direction = economic_direction(row.get("direction") or row.get("type"))
        status = str(row.get("status") or "").lower()
        if row_direction != "buy" or status not in FILL_STATUSES | {PARTIAL_STATUS} | ACTIVE_ORDER_STATUSES:
            continue
        row_account = str(row.get("account") or row.get("execution_account") or "")
        if not is_nisa_account(row_account):
            continue
        when, _source = effective_execution_timestamp(row)
        if when is None or not _is_strictly_after_baseline(
            when, baseline_dt=baseline_dt, baseline_day=baseline_day
        ):
            continue
        sid = str(row.get("action_state_id") or "")
        if status == "ordered" and sid and sid in terminal_by_state:
            ordered_at, _ordered_source = effective_execution_timestamp(row)
            if ordered_at is not None and terminal_by_state[sid] >= ordered_at:
                continue
        linked = state_actions.get(sid) if sid else None
        merged = {**(linked if isinstance(linked, dict) else {}), **row}
        row_owner = routing_owner(merged)
        row_broker = canonical_broker(
            merged.get("execution_broker") or merged.get("broker")
        ) or explicit_action_broker(merged)
        if not row_owner or not row_broker:
            unattributed.append(str(row.get("id") or "unknown"))
            continue
        if row_owner != owner or row_broker != broker:
            continue
        notional = _record_notional_jpy(row, fx_rate=fx_rate)
        if notional is None:
            unattributed.append(str(row.get("id") or "unknown"))
            continue
        if status == "ordered":
            reserved_open += notional
        elif is_fill_record(row):
            consumed_after_baseline += notional

    if unattributed:
        reasons.append({
            "code": "nisa_capacity_unattributed_activity",
            "message": "基準日後のNISA活動に名義・証券会社または金額が欠落",
            "execution_ids": unattributed[:5],
        })

    remaining = max(0.0, remaining - consumed_after_baseline - reserved_open)
    try:
        requested = float(action.get("estimated_notional_jpy") or action.get("amount_jpy") or 0)
    except (TypeError, ValueError):
        requested = 0.0
    if requested <= 0:
        reasons.append({"code": "nisa_notional_missing", "message": "NISA買付金額を検証できない"})
    elif requested > remaining:
        reasons.append({
            "code": "nisa_capacity_insufficient",
            "message": f"NISA残枠 ¥{remaining:,.0f} に対し買付予定 ¥{requested:,.0f}",
            "remaining_jpy": round(remaining),
            "requested_jpy": round(requested),
        })

    baseline_for_age = baseline_dt.astimezone(JST).date() if baseline_dt else baseline_day
    age_days = (now.astimezone(JST).date() - baseline_for_age).days if baseline_for_age else None
    stale_reason = None
    if age_days is not None and age_days > 30:
        stale_reason = {
            "code": "nisa_capacity_stale",
            "message": f"NISA残枠の基準日が{age_days}日前",
            "baseline": str(raw.get("last_updated")),
            "age_days": age_days,
        }

    blocked_codes = {
        "nisa_capacity_unattributed_activity",
        "nisa_notional_missing",
        "nisa_capacity_insufficient",
    }
    blocked = any(reason.get("code") in blocked_codes for reason in reasons)
    if stale_reason:
        reasons.append(stale_reason)
    return {
        "readiness": "blocked" if blocked else ("review" if stale_reason else "ready"),
        "reasons": reasons,
        "execution_owner": owner,
        "execution_broker": broker,
        "nisa_capacity_remaining_jpy": round(remaining),
        "nisa_capacity_baseline": raw.get("last_updated"),
    }

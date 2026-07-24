"""Deterministic execution-readiness classification for AI recommendations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import re

from macro_event_calendar import evaluate_macro_event_gate, load_macro_event_state
from discretionary_funding import evaluate_discretionary_funding, load_execution_plan_state


RISK_INCREASING = {"buy", "add", "dca", "margin_buy", "short", "short_sell"}
EXIT_ACTION_TYPES = {"sell", "trim", "reduce", "take_profit", "stop_loss", "exit", "close"}
FUND_PREFIXES = ("SLIM_", "IFREE_", "MNXACT", "NOMURA_")
SEVERITY = {"ready": 0, "review": 1, "blocked": 2}


def _parse_local_timestamp(value: object, *, local_tz: ZoneInfo) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_tz)
        return dt
    except Exception:
        return None


def _file_timestamp(path: Path, *, now: datetime, timestamp_keys: tuple[str, ...]) -> datetime | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    dt = None
    if isinstance(raw, dict):
        for key in timestamp_keys:
            value = raw.get(key)
            # A date-only value describes the business date, not midnight as
            # the snapshot creation time.  Treating 2026-07-14 as 00:00 made a
            # file imported late that evening look >24h old the next morning.
            # Use another precise key or the file mtime instead.
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "").strip()):
                continue
            dt = _parse_local_timestamp(value, local_tz=ZoneInfo("Asia/Tokyo"))
            if dt:
                break
    if dt is None:
        try:
            dt = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
        except Exception:
            return None
    return dt.astimezone(now.tzinfo)


def _file_age_hours(path: Path, *, now: datetime, timestamp_keys: tuple[str, ...]) -> float | None:
    dt = _file_timestamp(path, now=now, timestamp_keys=timestamp_keys)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 3600)


def _latest_applied_execution_at(base_dir: Path, *, now: datetime) -> datetime | None:
    """Newest execution that explicitly says its portfolio mutation succeeded."""
    try:
        raw = json.loads((base_dir / "action_executions.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    rows = raw.get("executions") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return None
    latest = None
    terminal = {"executed", "partial", "filled", "done"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").lower() not in terminal:
            continue
        if row.get("portfolio_applied") is not True:
            continue
        dt = None
        for key in ("portfolio_applied_at", "executed_at_time", "saved_at"):
            dt = _parse_local_timestamp(row.get(key), local_tz=ZoneInfo("Asia/Tokyo"))
            if dt:
                break
        if dt is not None:
            dt = dt.astimezone(now.tzinfo)
            latest = dt if latest is None or dt > latest else latest
    return latest


def portfolio_snapshot_health(base_dir: Path, *, now: datetime) -> dict:
    account_age = _file_age_hours(base_dir / "account.json", now=now, timestamp_keys=("last_updated", "as_of"))
    holdings_path = base_dir / "holdings.json"
    holdings_at = _file_timestamp(holdings_path, now=now, timestamp_keys=("last_updated", "as_of"))
    holdings_age = max(0.0, (now - holdings_at).total_seconds() / 3600) if holdings_at else None
    latest_applied_at = _latest_applied_execution_at(base_dir, now=now)

    # holdings.json is a quantity ledger, not a market-price feed.  Between
    # daily analyses it remains execution-current when the latest explicitly
    # applied fill and the holdings write are the same transaction.  Extend
    # the soft 24h window to at most seven days in that case; periodic broker
    # reconciliation is still required beyond the hard window.
    execution_ledger_current = bool(
        holdings_at
        and latest_applied_at
        and holdings_age is not None
        and 24 < holdings_age <= 24 * 7
        and latest_applied_at <= holdings_at + timedelta(seconds=5)
    )
    effective_holdings_age = 24.0 if execution_ledger_current else holdings_age
    known = [age for age in (account_age, effective_holdings_age) if age is not None]
    max_age = max(known) if known else None
    if max_age is None:
        status = "unknown"
    elif max_age > 72:
        status = "stale"
    elif max_age > 24:
        status = "degraded"
    else:
        status = "fresh"
    return {
        "status": status,
        "max_age_hours": round(max_age, 1) if max_age is not None else None,
        "account_age_hours": round(account_age, 1) if account_age is not None else None,
        "holdings_age_hours": round(holdings_age, 1) if holdings_age is not None else None,
        "execution_ledger_current": execution_ledger_current,
        "latest_applied_execution_at": latest_applied_at.isoformat() if latest_applied_at else None,
        "holdings_snapshot_at": holdings_at.isoformat() if holdings_at else None,
    }


def _technical_entry(base_dir: Path, ticker: str) -> dict | None:
    try:
        raw = json.loads((base_dir / "technical_state.json").read_text(encoding="utf-8"))
        row = (raw.get("tickers") or {}).get(ticker)
        return row if isinstance(row, dict) else None
    except Exception:
        return None


def _merge(current: str, incoming: str) -> str:
    return incoming if SEVERITY.get(incoming, 0) > SEVERITY.get(current, 0) else current


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _requested_exit_quantity(action: dict) -> float | None:
    """Return only deterministic quantity fields.

    Natural-language fallbacks are unsafe for exits: in text such as
    ``残り50株のうち10株を売却`` the first number is inventory, while
    ``1株ずつ計5株売却`` starts with a quantity smaller than the actual order.
    The holding binder/API must persist an explicit requested quantity.
    """
    for key in ("requested_sell_quantity", "quantity"):
        quantity = _positive_number(action.get(key))
        if quantity is not None:
            return quantity
    if action.get("sell_all"):
        return _positive_number(action.get("holding_shares_before"))
    return None


def _load_json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _cash_holding_row(base_dir: Path, key: str) -> dict | None:
    raw = _load_json_object(base_dir / "holdings.json")
    if raw is None:
        return None
    positions = raw.get("holdings") or raw.get("positions") or raw
    if not isinstance(positions, dict):
        return None
    row = positions.get(key)
    return row if isinstance(row, dict) else None


def _requested_buy_quantity(action: dict) -> float | None:
    for key in ("requested_buy_quantity", "quantity"):
        quantity = _positive_number(action.get(key))
        if quantity is not None:
            return quantity
    text = " ".join(str(action.get(key) or "") for key in ("amount_hint", "action"))
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:株|口)", text)
    return _positive_number(match.group(1)) if match else None


def _requested_buy_notional(action: dict, *, currency: str) -> float | None:
    """Return requested cash in the route's native currency."""
    quantity = _requested_buy_quantity(action)
    price = None
    for key in ("limit_price", "decision_price", "price", "current_price"):
        price = _positive_number(action.get(key))
        if price is not None:
            break
    if quantity is not None and price is not None:
        return quantity * price
    if currency == "JPY":
        amount_jpy = _positive_number(action.get("amount_jpy"))
        if amount_jpy is not None:
            return amount_jpy
        estimated = _positive_number(action.get("estimated_notional_jpy"))
        if estimated is not None:
            return estimated
    return None


def evaluate_cash_buying_power(action: dict, *, base_dir: Path) -> dict:
    """Check an explicitly routed cash buy against that wallet only.

    The wife's SBI row is an estimated reconciliation ledger by design.  It is
    shown in NAV, but unless explicitly confirmed it must never be treated as
    fresh buying power.
    """
    action_type = str(action.get("type") or action.get("action_type") or "").lower()
    if action_type not in {"buy", "add", "dca"}:
        return {"required": False, "readiness": "ready", "reasons": []}

    from execution_safety import canonical_broker, canonical_owner

    owner = canonical_owner(action.get("execution_owner") or action.get("owner"))
    broker = canonical_broker(action.get("execution_broker") or action.get("broker"))
    # Legacy actions without a structured route are handled by the existing
    # route/scope gates.  Do not infer a wallet from prose here.
    if not owner or not broker:
        return {"required": False, "readiness": "ready", "reasons": []}

    ticker = str(action.get("ticker") or "").upper()
    currency = str(action.get("currency") or ("JPY" if ticker.endswith(".T") else "USD")).upper()
    required = _requested_buy_notional(action, currency=currency)
    route = ""
    balance = None
    status = "confirmed"
    reconciliation_required = False

    if owner == "husband" and broker == "rakuten" and currency in {"JPY", "USD"}:
        route = "account.json"
        account = _load_json_object(base_dir / "account.json")
        if account is not None:
            raw_balance = account.get("balance") if currency == "JPY" else account.get("usd_balance")
            try:
                parsed_balance = float(raw_balance)
                balance = parsed_balance if parsed_balance >= 0 else None
            except (TypeError, ValueError):
                balance = None
    elif owner == "husband" and broker == "sbi" and currency == "JPY":
        route = "CASH_JPY_SBI"
        row = _cash_holding_row(base_dir, route)
        if row is not None:
            try:
                balance = float(row.get("shares", 0) or 0)
            except (TypeError, ValueError):
                balance = None
            status = str(row.get("balance_status") or "confirmed").lower()
            reconciliation_required = bool(row.get("reconciliation_required", False))
    elif owner == "wife" and broker == "sbi" and currency == "JPY":
        route = "CASH_JPY_SBI_WIFE"
        row = _cash_holding_row(base_dir, route)
        if row is not None:
            try:
                balance = float(row.get("shares", 0) or 0)
            except (TypeError, ValueError):
                balance = None
            status = str(row.get("balance_status") or "estimated").lower()
            reconciliation_required = bool(row.get("reconciliation_required", True))
    else:
        return {
            "required": True,
            "readiness": "blocked",
            "reasons": [{
                "code": "cash_route_unresolved",
                "message": f"{owner}×{broker}×{currency} の買付現金ルートは未定義です",
                "execution_owner": owner,
                "execution_broker": broker,
                "currency": currency,
            }],
        }

    details = {
        "cash_route": route,
        "execution_owner": owner,
        "execution_broker": broker,
        "currency": currency,
        "available_cash": balance,
        "requested_cash": round(required, 2) if required is not None else None,
        "balance_status": status,
        "reconciliation_required": reconciliation_required,
    }
    if balance is None:
        return {
            "required": True,
            "readiness": "blocked",
            "reasons": [{
                "code": "cash_balance_unresolved",
                "message": f"{route or '現金口座'} の買付余力を確認できません",
                **details,
            }],
        }
    if status != "confirmed" or reconciliation_required:
        return {
            "required": True,
            "readiness": "blocked",
            "reasons": [{
                "code": "cash_balance_unconfirmed",
                "message": f"{route} は推定残高のため、新規買付余力には使用しません",
                **details,
            }],
        }
    if required is None:
        return {
            "required": True,
            "readiness": "blocked",
            "reasons": [{
                "code": "cash_notional_unresolved",
                "message": "買付数量と価格から必要現金を確定できません",
                **details,
            }],
        }
    if required > balance:
        return {
            "required": True,
            "readiness": "blocked",
            "reasons": [{
                "code": "cash_balance_insufficient",
                "message": f"{route} の残高{balance:,.2f}{currency}では{required:,.2f}{currency}を買付できません",
                "shortfall": round(required - balance, 2),
                **details,
            }],
        }
    return {"required": True, "readiness": "ready", "reasons": [], **details}


def classify_execution_readiness(
    action: dict,
    *,
    base_dir: Path,
    now: datetime | None = None,
    macro_state: dict | None = None,
) -> dict:
    """Return an additive readiness decision; never mutate ``action``."""
    now = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    readiness = "ready"
    reasons: list[dict] = []
    advisories: list[dict] = []

    def add(level: str, code: str, message: str, **extra) -> None:
        nonlocal readiness
        readiness = _merge(readiness, level)
        row = {"code": code, "message": message}
        row.update(extra)
        reasons.append(row)

    if action.get("non_executable"):
        add("review", "non_executable_candidate", str(action.get("non_executable_reason") or "非実行候補"))
    if action.get("execution_plan_would_filter"):
        add("review", "execution_plan_observe_conflict", "execution planのobserve判定では非実行")
    recent_opposite = action.get("recent_opposite_execution_guard")
    if isinstance(recent_opposite, dict):
        level = str(recent_opposite.get("level") or "review")
        if level in {"blocked", "review"}:
            add(
                level,
                str(recent_opposite.get("code") or "recent_opposite_execution"),
                str(recent_opposite.get("message") or "反対方向の約定履歴を要確認"),
                **{
                    key: value
                    for key, value in recent_opposite.items()
                    if key not in {"level", "code", "message"}
                },
            )
    if action.get("opposite_intent_conflict"):
        conflict_level = (
            "blocked"
            if str(action.get("type") or "").lower() in RISK_INCREASING
            else "review"
        )
        add(
            conflict_level,
            "opposite_intent_conflict",
            str(action.get("opposite_intent_conflict_reason") or "同一分析に反対方向の売買意図が併存"),
        )
    if action.get("cross_scope_opposite_action"):
        add("review", "cross_scope_opposite_action", "異なる口座・運用ティアに反対方向の提案が併存")
    if action.get("cross_owner_opposite_action"):
        add("review", "cross_owner_opposite_action", "別名義に反対方向の注文・約定が存在")
    if action.get("holding_scope_unresolved"):
        add("blocked", "holding_scope_unresolved", "指定された口座・運用ティアに一致する保有を確認できない")
    elif action.get("holding_scope_ambiguous"):
        add("blocked", "holding_scope_ambiguous", "同一銘柄を複数口座で保有しており発注口座を特定できない")

    action_type = str(action.get("type") or "").lower()
    ticker = str(action.get("ticker") or "")
    risk_increasing = action_type in RISK_INCREASING

    if action_type in EXIT_ACTION_TYPES and not (
        action.get("holding_scope_unresolved") or action.get("holding_scope_ambiguous")
    ):
        from execution_safety import evaluate_exit_route_consistency

        route_result = evaluate_exit_route_consistency(action, base_dir=base_dir)
        readiness = _merge(readiness, str(route_result.get("readiness") or "ready"))
        reasons.extend(route_result.get("reasons") or [])

        requested_quantity = _requested_exit_quantity(action)
        available_quantity = _positive_number(action.get("holding_shares_before"))
        if requested_quantity is None or available_quantity is None:
            add(
                "blocked",
                "holding_quantity_unresolved",
                "指定口座の売却数量または保有数量を確認できない",
                requested_quantity=requested_quantity,
                available_quantity=available_quantity,
            )
        elif requested_quantity > available_quantity or action.get("holding_quantity_exceeds_account"):
            add(
                "blocked",
                "holding_quantity_exceeds_account",
                f"指定口座の保有{available_quantity:g}株に対し{requested_quantity:g}株の売却はできません",
                requested_quantity=requested_quantity,
                available_quantity=available_quantity,
                shortfall_quantity=round(requested_quantity - available_quantity, 8),
                execution_account=action.get("execution_account"),
                execution_position_keys=action.get("execution_position_keys") or [],
            )

    funding = evaluate_discretionary_funding(
        action_type,
        plan_state=load_execution_plan_state(base_dir),
    )
    if funding.get("required") and not funding.get("allowed"):
        add(
            "blocked",
            str(funding.get("reason_code") or "discretionary_funding_unresolved"),
            str(funding.get("message") or "裁量投資枠を確認できません"),
            **{
                key: value
                for key, value in funding.items()
                if key not in {"required", "allowed", "reason_code", "message"}
            },
        )

    try:
        cash_result = evaluate_cash_buying_power(action, base_dir=base_dir)
    except Exception as exc:
        cash_result = {
            "readiness": "blocked",
            "reasons": [{
                "code": "cash_buying_power_check_error",
                "message": f"買付余力判定に失敗: {type(exc).__name__}: {str(exc)[:160]}",
            }],
        }
    readiness = _merge(readiness, str(cash_result.get("readiness") or "ready"))
    reasons.extend(cash_result.get("reasons") or [])

    # The once-daily analysis intentionally runs after the US close and before
    # the JPX open.  Those are valid planning windows: keep the action ready,
    # start its TTL at the next opening, and ask the user to confirm the live
    # quote when placing it.  Only a genuinely long closure (next opening more
    # than 24h away), or an unresolved calendar, downgrades readiness.
    market_context = None
    try:
        from execution_safety import economic_direction, market_session_context

        if economic_direction(action_type) in {"buy", "sell", "short", "cover"}:
            market_context = market_session_context(ticker, now)
            # Funds and other non-exchange instruments deliberately do not
            # inherit an equity-session failure merely because no exchange can
            # be inferred from their ticker.
            if market_context.get("exchange") not in {"JPX", "NYSE"}:
                market_context = None
            elif market_context.get("status") == "closed":
                next_open = _parse_local_timestamp(
                    market_context.get("next_market_open"),
                    local_tz=ZoneInfo("Asia/Tokyo"),
                )
                opens_within_24h = bool(
                    next_open is not None
                    and timedelta(0) <= next_open.astimezone(now.tzinfo) - now <= timedelta(hours=24)
                )
                if opens_within_24h:
                    advisories.append({
                        "code": "market_quote_confirmation_required",
                        "message": "次の寄り付き前後に現在値・スプレッドを確認してから発注してください",
                        **market_context,
                    })
                else:
                    next_session = market_context.get("next_session_date") or "次回営業日"
                    add(
                        "review",
                        "market_closed_reprice_required",
                        f"{market_context.get('exchange') or '取引所'} は {market_context.get('local_date')} が休場。"
                        f"{next_session} の朝分析で価格・板を更新してから再提案する",
                        **market_context,
                    )
            elif market_context.get("session_state") == "closed":
                advisories.append({
                    "code": "market_quote_confirmation_required",
                    "message": "市場時間外の分析です。発注時に現在値・スプレッドを確認してください",
                    **market_context,
                })
            elif market_context.get("status") == "unresolved":
                add(
                    "review",
                    "market_session_unresolved",
                    "取引所カレンダーを確認できないため、発注前に市場セッションを再確認する",
                    **market_context,
                )
    except Exception as exc:
        market_context = {
            "status": "unresolved",
            "reason": f"market_session_error:{type(exc).__name__}",
        }
        add(
            "review",
            "market_session_unresolved",
            "取引所カレンダーを確認できないため、発注前に市場セッションを再確認する",
            **market_context,
        )
    if risk_increasing:
        snapshot = portfolio_snapshot_health(base_dir, now=now)
        if snapshot["status"] in {"stale", "unknown"}:
            add(
                "blocked",
                "portfolio_snapshot_stale",
                f"口座・保有スナップショットが72時間超または不明 ({snapshot.get('max_age_hours')}h)",
                **snapshot,
            )
        elif snapshot["status"] == "degraded":
            add(
                "review",
                "portfolio_snapshot_degraded",
                f"口座・保有スナップショットが24時間超 ({snapshot.get('max_age_hours')}h)",
                **snapshot,
            )

        if not ticker.startswith(FUND_PREFIXES):
            tech = _technical_entry(base_dir, ticker)
            if tech is None:
                add("blocked", "technical_data_missing", f"{ticker} のテクニカル基準日を確認できない")
            else:
                status = str(tech.get("freshness_status") or "unknown")
                quality_status = str(tech.get("data_quality_status") or "ok")
                if quality_status == "blocked":
                    add(
                        "blocked",
                        "technical_data_degraded",
                        f"{ticker} の価格系列に未調整の分割・併合候補があるためテクニカル指標を無効化",
                        data_as_of=tech.get("data_as_of"),
                        data_quality_reasons=tech.get("data_quality_reasons") or [],
                    )
                elif status in {"stale", "unknown"}:
                    add("blocked", "technical_data_stale", f"{ticker} の最終足が古い", data_as_of=tech.get("data_as_of"))
                elif status == "degraded":
                    add("review", "technical_data_degraded", f"{ticker} の最終足が1セッション遅延", data_as_of=tech.get("data_as_of"))

        event_result = evaluate_macro_event_gate(
            action,
            macro_state if macro_state is not None else load_macro_event_state(base_dir / "macro_event_state.json"),
            now=now.astimezone(timezone.utc),
        )
        if event_result.get("readiness") != "ready":
            readiness = _merge(readiness, str(event_result.get("readiness")))
            reasons.extend(event_result.get("reasons") or [])

        try:
            from execution_safety import evaluate_nisa_capacity

            nisa_result = evaluate_nisa_capacity(action, base_dir=base_dir, now=now)
        except Exception as exc:
            nisa_result = {
                "readiness": "review",
                "reasons": [{
                    "code": "nisa_capacity_check_error",
                    "message": f"NISA残枠判定に失敗: {type(exc).__name__}: {str(exc)[:160]}",
                }],
            }
        readiness = _merge(readiness, str(nisa_result.get("readiness") or "ready"))
        reasons.extend(nisa_result.get("reasons") or [])

    ticker_upper = ticker.upper()
    is_fund = ticker_upper.startswith(FUND_PREFIXES)
    order_type = str(action.get("order_type") or "").lower()
    urgency = str(action.get("urgency") or "medium").lower()
    spread = action.get("spread_bps")
    try:
        spread = float(spread) if spread is not None else None
    except (TypeError, ValueError):
        spread = None
    if action.get("no_trade_zone"):
        add("blocked", "no_trade_zone", str(action.get("skip_reason") or "推定コストが期待値を上回る"))
    elif order_type == "market" and not is_fund:
        if urgency != "high":
            add("blocked", "market_order_low_urgency", "low/medium urgencyの成行は許可しない")
        if action.get("decision_price") in (None, ""):
            add("blocked", "market_order_price_missing", "成行判断の基準価格がない")
        if spread is None:
            add("review", "market_order_spread_unknown", "成行前にbid/ask spreadの確認が必要")
        elif spread > 30:
            add("blocked", "market_order_spread_too_wide", f"spread {spread:.1f}bps > 30bps のため指値必須", spread_bps=spread)
    elif order_type in {"limit", "stop_limit"}:
        if action.get("limit_price") in (None, ""):
            add("blocked", "limit_price_missing", "指値注文にlimit_priceがない")
        elif spread is not None and spread > 30:
            add(
                "review",
                "limit_order_wide_spread_review",
                f"spread {spread:.1f}bps > 30bps のため指値水準と流動性を再確認",
                spread_bps=spread,
            )
    elif not order_type and not action.get("no_trade_zone"):
        add("review", "order_type_missing", "注文方式の再評価が必要")

    result = {
        "execution_readiness": readiness,
        "execution_block_reasons": reasons,
        "execution_advisories": advisories,
    }
    if market_context is not None:
        result["market_session"] = market_context
        if market_context.get("status") == "closed" and any(
            row.get("code") == "market_closed_reprice_required" for row in reasons
        ):
            result["market_reprice_required"] = True
            result["expiry_deferred_until_reprice"] = True
            result["market_reprice_after"] = market_context.get("next_market_open")
        elif market_context.get("session_state") == "closed" or market_context.get("status") == "closed":
            result["market_quote_confirmation_required"] = True
            result["expiry_starts_at"] = market_context.get("next_market_open")
            result["expiry_ends_at"] = (
                market_context.get("next_market_close")
                or market_context.get("market_close")
            )
            result["market_order_window"] = market_context.get("reason")
    if risk_increasing:
        event_result = evaluate_macro_event_gate(
            action,
            macro_state if macro_state is not None else load_macro_event_state(base_dir / "macro_event_state.json"),
            now=now.astimezone(timezone.utc),
        )
        for key in ("event_context", "required_size_multiplier", "market_order_allowed"):
            if key in event_result:
                result[key] = event_result[key]
        for key in (
            "execution_owner",
            "execution_broker",
            "nisa_capacity_remaining_jpy",
            "nisa_capacity_baseline",
        ):
            if key in nisa_result:
                result[key] = nisa_result[key]
    return result


def apply_execution_readiness(actions: list[dict], *, base_dir: Path, now: datetime | None = None) -> list[dict]:
    macro_state = load_macro_event_state(base_dir / "macro_event_state.json")
    for action in actions:
        if isinstance(action, dict):
            action.update(classify_execution_readiness(action, base_dir=base_dir, now=now, macro_state=macro_state))
    return actions

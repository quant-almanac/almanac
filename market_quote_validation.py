"""Deterministic validation for executable bid/ask snapshots.

A quote is only evidence about execution cost while its exchange is actually
open.  Outside the regular session the book is thin and the feed mixes
composite/last-trade values, so the same checks that catch real data faults
during trading hours instead manufacture them: a 614bps spread on a mega-cap,
or a bid above its ask.  Both were observed on 2026-08-20, when the 06:15 JST
analysis read US quotes captured 1h18m after the NY close and blocked two
otherwise-fine orders on spread/inversion that would not exist in the session
the owner would actually trade in.  When the exchange is closed the quote is
reported as ``session_closed`` and the readiness layer routes it through the
existing "spread unknown" path — confirm before ordering, rather than
asserting a number that is not real.

The analysis pipeline can keep a recommendation when a quote is unavailable,
but it must never derive execution cost or a limit price from an internally
inconsistent quote.  A ``spread_bps`` value without its bid/ask inputs is also
internally unverifiable: spread is derived from those two inputs and must not
survive as an orphaned execution signal.  Actions with neither quote nor
spread continue through the existing ``spread unknown`` path in
:mod:`execution_readiness`; this preserves investment-trust orders, which do
not have an executable bid/ask quote.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Mapping
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
MAX_QUOTE_AGE = timedelta(hours=36)


def _as_positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _parse_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed


def _session_state(action: Mapping[str, object], quote_as_of: object) -> str | None:
    """クオート取得時点で、その銘柄の取引所が開いていたか。

    判定できないとき (取引所不明・カレンダー未取得) は None を返し、
    呼び出し側は従来どおりの検証に落ちる。ここで例外を上げると、
    カレンダーが引けないだけで発注判断全体が止まる。
    """
    when = _parse_timestamp(quote_as_of)
    if when is None:
        return None
    try:
        from execution_safety import market_session_context

        context = market_session_context(action.get("ticker"), when)
    except Exception:
        return None
    state = context.get("session_state")
    return str(state) if state in ("open", "closed") else None


def validate_market_quote(
    action: Mapping[str, object],
    *,
    now: datetime | None = None,
    max_age: timedelta = MAX_QUOTE_AGE,
) -> dict:
    """Validate a supplied quote and return a serialisable diagnostic.

    ``status == "absent"`` means the action did not carry a quote.  It is not
    an error here because the readiness layer already has separate semantics
    for an unknown spread.  Any partial or malformed *supplied* quote is
    ``invalid`` and is therefore fail-closed for executable instructions.
    """
    raw_bid = action.get("quote_bid")
    raw_ask = action.get("quote_ask")
    raw_spread = action.get("spread_bps")
    has_bid = raw_bid not in (None, "")
    has_ask = raw_ask not in (None, "")
    if not has_bid and not has_ask:
        if raw_spread not in (None, ""):
            return {
                "status": "invalid",
                "code": "market_quote_unverified",
                "message": "spread_bps に対応する bid/ask がないため注文用quoteとして検証できない",
                "supplied_spread_bps": raw_spread,
            }
        return {"status": "absent", "code": None, "message": None}

    bid = _as_positive_number(raw_bid)
    ask = _as_positive_number(raw_ask)
    if not has_bid or not has_ask:
        return {
            "status": "invalid",
            "code": "market_quote_incomplete",
            "message": "bid/ask の片方だけがあるため注文用quoteとして使えない",
            "bid": bid,
            "ask": ask,
        }
    if bid is None or ask is None:
        return {
            "status": "invalid",
            "code": "market_quote_nonpositive",
            "message": "bid/ask が正の有限値ではない",
            "bid": bid,
            "ask": ask,
        }
    quote_session = _session_state(action, action.get("quote_as_of"))
    if bid > ask:
        if quote_session == "closed":
            # 時間外は板が薄く、composite/last-trade 混在で交差が普通に起きる。
            # 実市場の異常として扱うと、翌セッションでは存在しない欠陥で
            # 発注が止まる。
            return {
                "status": "session_closed",
                "code": "market_quote_session_closed",
                "message": "取引所が時間外のため、注文用quoteとして検証できない（bid/ask交差）",
                "bid": bid,
                "ask": ask,
                "quote_session_state": "closed",
            }
        return {
            "status": "invalid",
            "code": "market_quote_inverted",
            "message": "bid が ask を上回っており、quote整合性を確認できない",
            "bid": bid,
            "ask": ask,
            "quote_session_state": quote_session,
        }

    quote_as_of = _parse_timestamp(action.get("quote_as_of"))
    if quote_as_of is None:
        return {
            "status": "invalid",
            "code": "market_quote_timestamp_missing",
            "message": "bid/ask の取得時刻がないため注文用quoteとして使えない",
            "bid": bid,
            "ask": ask,
        }
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = current.astimezone(timezone.utc) - quote_as_of.astimezone(timezone.utc)
    if age < -timedelta(minutes=5) or age > max_age:
        return {
            "status": "invalid",
            "code": "market_quote_stale",
            "message": "bid/ask の取得時刻が注文判断に使える範囲外",
            "bid": bid,
            "ask": ask,
            "quote_as_of": quote_as_of.isoformat(),
            "age_hours": round(age.total_seconds() / 3600, 2),
        }

    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 10_000 if mid else None
    if quote_session == "closed":
        # 値そのものは残す(参考として読める)が、spread を執行ゲートには渡さない。
        # 時間外のスプレッドは、実際に発注する次のセッションのコストではない。
        return {
            "status": "session_closed",
            "code": "market_quote_session_closed",
            "message": "取引所が時間外のため、spreadを執行判断に使わない",
            "bid": bid,
            "ask": ask,
            "quote_as_of": quote_as_of.isoformat(),
            "age_hours": round(max(0.0, age.total_seconds() / 3600), 2),
            "observed_spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
            "quote_session_state": "closed",
        }
    return {
        "status": "valid",
        "code": None,
        "message": None,
        "bid": bid,
        "ask": ask,
        "quote_as_of": quote_as_of.isoformat(),
        "age_hours": round(max(0.0, age.total_seconds() / 3600), 2),
        "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
        "quote_session_state": quote_session,
    }

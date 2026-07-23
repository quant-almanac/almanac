"""Append-only outcome measurement for catalyst and sell-side logs.

This is the bridge between the immutable decision logs and the EV/reliability
reports.  It intentionally appends to ``*_outcome_log.jsonl`` instead of
mutating decision rows, preserving the R9 append-only invariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo

from .logs import write_catalyst_outcome, write_sell_outcome

DEFAULT_HORIZONS = (3, 5, 10, 20, 60)
UNMEASURABLE_TICKER_PREFIXES = ("SLIM_", "IFREE_", "NOMURA_", "MNXACT")
# 口座区分サフィックス付きの保有疑似名 (例: AVGO_特定, AVGO_一般, META_特定)。
# yfinance では解決できないため測定対象から除外する。
UNMEASURABLE_TICKER_SUFFIXES = ("_特定", "_一般")
UNMEASURABLE_TICKERS = {
    "CASH_JPY",
    "CASH_USD",
    "CASH_JPY_SBI",
    "CASH_USD_SBI",
    "WIFE_NISA_TSUMITATE",
}


class PriceProvider(Protocol):
    """Minimal market-data interface used by the updater."""

    def price_on_or_after(self, ticker: str, after_date: date) -> float | None:
        """Return the first available close on/after ``after_date``."""


# yfinance では取得できない指数の代替ティッカーマップ
# ^TOPX / TOPIX は上場廃止扱いで取得不可 → NEXT FUNDS TOPIX ETF (1306.T) で代替
_TICKER_ALIASES: dict[str, str] = {
    "^TOPX":  "1306.T",
    "^TOPIX": "1306.T",
    "TOPIX":  "1306.T",
}


def is_unmeasurable_primary_ticker(ticker: str) -> bool:
    """True for pseudo tickers that have no supported price oracle yet."""
    t = str(ticker or "").strip().upper()
    return (
        t in UNMEASURABLE_TICKERS
        or t.startswith(UNMEASURABLE_TICKER_PREFIXES)
        or t.endswith(UNMEASURABLE_TICKER_SUFFIXES)
    )


@dataclass
class YFinancePriceProvider:
    """Small yfinance-backed provider for cron use."""

    lookahead_days: int = 10

    def price_on_or_after(self, ticker: str, after_date: date) -> float | None:
        # 取得不可ティッカーを代替へ正規化
        ticker = _TICKER_ALIASES.get(ticker, ticker)
        try:
            import yfinance as yf

            end = after_date + timedelta(days=self.lookahead_days)
            df = yf.download(
                ticker,
                start=after_date.isoformat(),
                end=end.isoformat(),
                progress=False,
                threads=False,
                auto_adjust=True,
            )
            if df is None or df.empty:
                return None
            close = df["Close"].dropna()
            if close.empty:
                return None
            value = close.iloc[0]
            if hasattr(value, "iloc"):
                value = value.iloc[0]
            price = float(value)
            return price if price > 0 else None
        except Exception:
            return None


def _read_jsonl(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _event_entry_date(value: object, ticker: str) -> date | None:
    """Return the earliest local session date executable after ``value``.

    Disclosure features carry a timezone-aware ``compute_time``. Daily closes
    from before that timestamp must never become the event price: a filing
    computed after the market close can only be acted on from the next session.

    Legacy date-only and naive timestamps retain their historical date
    semantics because their timezone is unknowable. ``price_on_or_after`` then
    handles weekends and exchange holidays.
    """
    if not value:
        return None
    text = str(value)
    try:
        event_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _parse_date(value)
    if event_dt.tzinfo is None:
        return event_dt.date()

    is_jp = _ticker_currency(ticker) == "JPY"
    market_tz = ZoneInfo("Asia/Tokyo" if is_jp else "America/New_York")
    market_close = time(15, 30) if is_jp else time(16, 0)
    local_dt = event_dt.astimezone(market_tz)
    entry_date = local_dt.date()
    if local_dt.timetz().replace(tzinfo=None) >= market_close:
        entry_date += timedelta(days=1)
    return entry_date


def _business_days_after(start: date, n: int) -> date:
    try:
        import numpy as np

        return date.fromisoformat(str(np.busday_offset(start.isoformat(), n, roll="forward")))
    except Exception:
        # Conservative fallback: enough calendar days to usually cover n market days.
        return start + timedelta(days=int(n * 7 / 5) + 2)


def _ticker_currency(ticker: str) -> str:
    t = str(ticker or "").upper()
    if t.endswith(".T") or t.isdigit() or t in {"TOPIX", "^TOPX", "1489.T", "1570.T"}:
        return "JPY"
    return "USD"


def _usdjpy(provider: PriceProvider, when: date, fallback: object = None) -> float | None:
    try:
        value = float(fallback)
        # 1.0 などのプレースホルダ値を除外。JPY/USD レートは現実的に 50 超。
        if value > 50:
            return value
    except (TypeError, ValueError):
        pass
    return provider.price_on_or_after("JPY=X", when)


def _normalize_price(
    price: float,
    *,
    ticker_currency: str,
    target_currency: str,
    usdjpy: float | None,
) -> float | None:
    if ticker_currency == target_currency:
        return price
    if usdjpy is None or usdjpy <= 0:
        return None
    if ticker_currency == "USD" and target_currency == "JPY":
        return price * usdjpy
    if ticker_currency == "JPY" and target_currency == "USD":
        return price / usdjpy
    return None


def _weighted_benchmark_return(
    row: dict,
    *,
    event_date: date,
    measure_date: date,
    provider: PriceProvider,
) -> float | None:
    basket = row.get("benchmark_basket") or []
    weights = row.get("benchmark_weights") or []
    if not basket or len(basket) != len(weights):
        return None
    target_currency = str(
        row.get("benchmark_currency_normalized_to")
        or row.get("primary_ticker_currency")
        or _ticker_currency(str(row.get("primary_ticker") or row.get("ticker") or ""))
    ).upper()
    event_prices = row.get("benchmark_price_at_event") or {}
    if not isinstance(event_prices, dict):
        event_prices = {}

    usdjpy_event = _usdjpy(provider, event_date, row.get("usdjpy_at_event") or row.get("usdjpy_at_recommend"))
    usdjpy_measure = _usdjpy(provider, measure_date, row.get("usdjpy_at_measure"))

    total_weight = 0.0
    total_return = 0.0
    for ticker, raw_weight in zip(basket, weights, strict=False):
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            return None
        ticker = str(ticker)
        event_price_raw = event_prices.get(ticker)
        try:
            event_price = float(event_price_raw)
        except (TypeError, ValueError):
            event_price = provider.price_on_or_after(ticker, event_date)
        measure_price = provider.price_on_or_after(ticker, measure_date)
        if event_price is None or measure_price is None or event_price <= 0:
            return None

        currency = _ticker_currency(ticker)
        norm_event = _normalize_price(
            event_price,
            ticker_currency=currency,
            target_currency=target_currency,
            usdjpy=usdjpy_event,
        )
        norm_measure = _normalize_price(
            measure_price,
            ticker_currency=currency,
            target_currency=target_currency,
            usdjpy=usdjpy_measure,
        )
        if norm_event is None or norm_measure is None or norm_event <= 0:
            return None
        total_weight += weight
        total_return += weight * ((norm_measure - norm_event) / norm_event)
    if total_weight <= 0:
        return None
    return total_return / total_weight


def _latest_generated_hypotheses(rows: Iterable[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in rows:
        if row.get("event_type") != "generated":
            continue
        hypothesis_id = row.get("hypothesis_id")
        if not hypothesis_id:
            continue
        latest[str(hypothesis_id)] = row
    return latest


def _measured_keys(rows: Iterable[dict], id_field: str) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for row in rows:
        ident = row.get(id_field)
        horizon = row.get("horizon_days")
        if ident is None or horizon is None:
            continue
        try:
            out.add((str(ident), int(horizon)))
        except (TypeError, ValueError):
            continue
    return out


def update_catalyst_outcomes(
    *,
    hypothesis_log_path: Path | str,
    outcome_log_path: Path | str,
    today: date | None = None,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    price_provider: PriceProvider | None = None,
    fsync: bool = True,
) -> int:
    """Append missing catalyst outcome rows and return the write count."""
    today = today or date.today()
    provider = price_provider or YFinancePriceProvider()
    hypotheses = _latest_generated_hypotheses(_read_jsonl(hypothesis_log_path))
    measured = _measured_keys(_read_jsonl(outcome_log_path), "hypothesis_id")
    written = 0

    for hypothesis_id, row in hypotheses.items():
        ticker = row.get("primary_ticker") or row.get("ticker")
        event_value = row.get("event_at") or row.get("analysis_date")
        event_date = _event_entry_date(event_value, str(ticker or ""))
        if event_date is None or not ticker:
            continue
        if is_unmeasurable_primary_ticker(str(ticker)):
            continue

        # price_at_event の解決は horizon が due になった最初の時点まで遅延させる。
        # due な horizon がなければプロバイダへの余分な HTTP リクエストを出さない。
        _price_resolved = False   # True = 解決試行済み（結果が None の場合もあり）
        price_at_event_f: float | None = None

        for horizon in horizons:
            horizon = int(horizon)
            if (hypothesis_id, horizon) in measured:
                continue
            measure_date = _business_days_after(event_date, horizon)
            if today < measure_date:
                continue
            # Horizon が due になった最初のタイミングで price_at_event を遅延解決する。
            # due でない hypothesis は一切プロバイダへの HTTP リクエストを出さない。
            if not _price_resolved:
                _price_resolved = True
                _pae_raw = row.get("price_at_event")
                if _pae_raw is None:
                    _pae_raw = provider.price_on_or_after(str(ticker), event_date)
                try:
                    price_at_event_f = float(_pae_raw) if _pae_raw is not None else None
                except (TypeError, ValueError):
                    price_at_event_f = None
            if price_at_event_f is None:
                break  # event price を取得できない → このhypothesisのoutcomeは計算不可
            price_at_measure = provider.price_on_or_after(str(ticker), measure_date)
            benchmark_return = _weighted_benchmark_return(
                row,
                event_date=event_date,
                measure_date=measure_date,
                provider=provider,
            )
            if price_at_measure is None or benchmark_return is None:
                continue
            usdjpy_event = _usdjpy(provider, event_date, row.get("usdjpy_at_event")) or 0.0
            usdjpy_measure = _usdjpy(provider, measure_date, row.get("usdjpy_at_measure")) or 0.0
            write_catalyst_outcome(
                outcome_log_path,
                hypothesis_id=hypothesis_id,
                horizon_days=horizon,
                reference_event_at=str(row.get("event_at") or row.get("analysis_date")),
                price_at_event=price_at_event_f,
                price_at_measure=float(price_at_measure),
                benchmark_basket=list(row.get("benchmark_basket") or []),
                benchmark_weights=list(row.get("benchmark_weights") or []),
                benchmark_currency_normalized_to=str(
                    row.get("benchmark_currency_normalized_to")
                    or _ticker_currency(str(ticker))
                ),
                benchmark_return_pct=benchmark_return,
                primary_ticker_currency=_ticker_currency(str(ticker)),
                usdjpy_at_event=usdjpy_event,
                usdjpy_at_measure=usdjpy_measure,
                fsync=fsync,
            )
            measured.add((hypothesis_id, horizon))
            written += 1
    return written


def update_sell_outcomes(
    *,
    sell_decision_log_path: Path | str,
    sell_outcome_log_path: Path | str,
    today: date | None = None,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    price_provider: PriceProvider | None = None,
    fsync: bool = True,
) -> int:
    """Append missing sell counterfactual outcome rows."""
    today = today or date.today()
    provider = price_provider or YFinancePriceProvider()
    decisions = {
        str(row["sell_decision_id"]): row
        for row in _read_jsonl(sell_decision_log_path)
        if row.get("sell_decision_id")
    }
    measured = _measured_keys(_read_jsonl(sell_outcome_log_path), "sell_decision_id")
    written = 0

    for sell_decision_id, row in decisions.items():
        event_date = _parse_date(row.get("recommended_at"))
        ticker = row.get("ticker")
        price_at_recommend = row.get("price_at_recommend")
        if event_date is None or not ticker or price_at_recommend is None:
            continue
        try:
            price_at_recommend_f = float(price_at_recommend)
        except (TypeError, ValueError):
            continue
        if price_at_recommend_f <= 0:
            # write_sell_outcome rejects zero; one bad legacy row (e.g. a 0.0
            # price recorded before validation existed) must skip itself, not
            # abort the whole catch-up batch.
            continue
        for horizon in horizons:
            horizon = int(horizon)
            if (sell_decision_id, horizon) in measured:
                continue
            measure_date = _business_days_after(event_date, horizon)
            if today < measure_date:
                continue
            counterfactual_price = provider.price_on_or_after(str(ticker), measure_date)
            benchmark_return = _weighted_benchmark_return(
                row,
                event_date=event_date,
                measure_date=measure_date,
                provider=provider,
            )
            if counterfactual_price is None or benchmark_return is None:
                continue
            usdjpy_event = _usdjpy(provider, event_date, row.get("usdjpy_at_recommend")) or 0.0
            usdjpy_measure = _usdjpy(provider, measure_date, row.get("usdjpy_at_measure")) or 0.0
            write_sell_outcome(
                sell_outcome_log_path,
                sell_decision_id=sell_decision_id,
                horizon_days=horizon,
                price_at_recommend=price_at_recommend_f,
                counterfactual_price=float(counterfactual_price),
                benchmark_return_pct=benchmark_return,
                benchmark_basket=list(row.get("benchmark_basket") or []),
                benchmark_weights=list(row.get("benchmark_weights") or []),
                benchmark_currency_normalized_to=str(
                    row.get("benchmark_currency_normalized_to")
                    or _ticker_currency(str(ticker))
                ),
                primary_ticker_currency=_ticker_currency(str(ticker)),
                usdjpy_at_recommend=usdjpy_event,
                usdjpy_at_measure=usdjpy_measure,
                fsync=fsync,
            )
            measured.add((sell_decision_id, horizon))
            written += 1
    return written


def update_all_outcomes(
    *,
    root: Path | str = ".",
    today: date | None = None,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    price_provider: PriceProvider | None = None,
    fsync: bool = True,
) -> dict[str, int]:
    """Update catalyst and sell-side outcome logs under ``root``."""
    root = Path(root)
    provider = price_provider or YFinancePriceProvider()
    catalyst = update_catalyst_outcomes(
        hypothesis_log_path=root / "catalyst_hypothesis_log.jsonl",
        outcome_log_path=root / "catalyst_outcome_log.jsonl",
        today=today,
        horizons=horizons,
        price_provider=provider,
        fsync=fsync,
    )
    sell = update_sell_outcomes(
        sell_decision_log_path=root / "sell_decision_log.jsonl",
        sell_outcome_log_path=root / "sell_outcome_log.jsonl",
        today=today,
        horizons=horizons,
        price_provider=provider,
        fsync=fsync,
    )
    return {"catalyst": catalyst, "sell": sell}

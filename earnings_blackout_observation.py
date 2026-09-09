"""S3 shadow-only earnings observations. Never supplies trading gate values.

The immutable input is frozen after self-heal. Legacy consumers still perform
their own reads; their actual results are recorded, not replayed at run end.
No promotion or trading-policy change is implemented here.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
SCHEMA_VERSION = 1
FILENAME = "earnings_blackout_observation.jsonl"
_CURRENT = ContextVar("earnings_shadow_observer", default=None)
_NON_EARNINGS = frozenset({"etf", "fund", "mutual_fund", "investment_trust",
                           "money_market_fund", "cash"})
_STOCK = frozenset({"stock", "equity", "common_stock"})


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class EarningsBlackoutState:
    resolved: bool
    today: date
    classifications: Mapping[str, str]
    events: Mapping[str, date]
    unknown_reasons: Mapping[str, str]
    input_hash: str
    failure_reason: str | None = None
    generated_at: str | None = None

    def window(self, days: int) -> Mapping[str, frozenset[str]]:
        if isinstance(days, bool) or not isinstance(days, int) or days < 0:
            raise ValueError("invalid window")
        buckets = {key: set() for key in
                   ("blackout", "cleared", "non_earnings", "unknown", "not_covered")}
        for ticker, classification in self.classifications.items():
            if classification != "dated":
                buckets[classification].add(ticker)
                continue
            event = self.events[ticker]
            cursor, lag = self.today, 0
            while cursor < event and lag <= days:
                cursor += timedelta(days=1)
                lag += cursor.weekday() < 5
            buckets["blackout" if event >= self.today and lag <= days else "cleared"].add(ticker)
        return MappingProxyType({key: frozenset(value) for key, value in buckets.items()})


def build_state(*, holdings, snapshot, registry, non_earnings, today, evidence):
    """Pure classification; missing metadata is never evidence of a stock."""
    if isinstance(holdings, dict):
        holdings = holdings.get("positions", holdings)
        if isinstance(holdings, dict):
            holdings = list(holdings.values())
    if not isinstance(holdings, list):
        raise ValueError("invalid holdings")
    from instrument_metadata import canonical_ticker

    classifications, events, reasons, types = {}, {}, {}, {}
    for row in holdings:
        if not isinstance(row, dict) or not isinstance(row.get("ticker"), str) or not row["ticker"].strip():
            raise ValueError("invalid holding row")
        ticker = canonical_ticker(row["ticker"])
        types.setdefault(ticker, set())
        for raw in (row.get("asset_type"), row.get("asset_class")):
            if isinstance(raw, str):
                types[ticker].add(raw.lower())
        if row.get("investment_type") == "cash":
            types[ticker].add("cash")
    rows = {} if snapshot is None else {
        canonical_ticker(row["ticker"]): row
        for row in snapshot.get("suggestions", []) + snapshot.get("skipped", [])
    }
    for ticker, kinds in types.items():
        registered = registry.get(ticker, {}).get("asset_class")
        if registered:
            kinds.add(registered)
        if ticker in non_earnings:
            kinds.add("fund")
        is_stock, is_other = bool(kinds & _STOCK), bool(kinds & _NON_EARNINGS)
        reason = None
        if is_stock and is_other:
            reason = "classification_conflict"
        elif is_other:
            classifications[ticker] = "non_earnings"
        elif not is_stock:
            reason = "classification_missing"
        elif snapshot is None:
            reason = "snapshot_unresolved"
        elif ticker not in rows:
            classifications[ticker] = "not_covered"
        else:
            row = rows[ticker]
            try:
                events[ticker] = date.fromisoformat(str(row.get("earnings_date") or row.get("earnings")))
                classifications[ticker] = "dated"
            except (ValueError, TypeError):
                reason = "earnings_date_missing_or_invalid"
        if reason:
            classifications[ticker] = "unknown"
            reasons[ticker] = reason
    return EarningsBlackoutState(
        snapshot is not None, today, MappingProxyType(classifications),
        MappingProxyType(events), MappingProxyType(reasons),
        _hash({"evidence": evidence, "holdings": holdings, "snapshot": snapshot,
               "registry": registry, "non_earnings": sorted(non_earnings), "today": today.isoformat()}),
        None if snapshot is not None else "snapshot_validation_failed",
        generated_at=snapshot.get("generated_at") if snapshot else None,
    )


def freeze_inputs(manager, *, now=None):
    """Validate once, with before/after source identities to detect torn reads.

    Source bytes and inode/mtime/ctime are checked around S2's internal reads;
    concurrent changes reject this observation instead of mixing generations.
    This does not change the legacy consumer's inputs or its locking policy.
    """
    from instrument_metadata import BROAD_EXECUTION_ALLOWLIST
    from pseudo_tickers import NON_EARNINGS_TICKERS

    now = now or datetime.now(JST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=JST)
    paths = (manager.HOLDINGS, manager.OUTPUT, manager.EARNINGS_OVERRIDES)

    def read(path):
        stat = path.stat()
        return ((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns), path.read_bytes())

    before = [read(path) for path in paths]
    snapshot = manager._read_and_validate_snapshot(now=now)
    after = [read(path) for path in paths]
    if before != after:
        raise ValueError("inputs_changed_during_validation")
    if snapshot is not None and snapshot != json.loads(before[1][1]):
        raise ValueError("validated_snapshot_mismatch")
    return build_state(
        holdings=json.loads(before[0][1]), snapshot=snapshot,
        registry=BROAD_EXECUTION_ALLOWLIST, non_earnings=NON_EARNINGS_TICKERS,
        today=now.astimezone(JST).date(),
        evidence=[hashlib.sha256(item[1]).hexdigest() for item in before],
    )


class Observer:
    def __init__(self):
        self.state = None
        self.error = None
        self.reads = []
        self.candidates = {}
        self.lock = threading.Lock()
        self.closed = False

    def freeze(self, manager):
        try:
            self.state = freeze_inputs(manager)
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)

    def capture(self, consumer, days, values, resolved=None):
        with self.lock:
            if not self.closed:
                self.reads.append({"consumer": consumer, "window": days,
                                   "legacy": sorted(values), "legacy_resolved": resolved})

    def capture_candidates(self, stage, rows):
        from instrument_metadata import canonical_ticker

        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError("candidate collection is not a list")
        tickers = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid candidate row")
            ticker = row.get("ticker")
            if isinstance(ticker, str) and ticker.strip():
                tickers.add(canonical_ticker(ticker))
        with self.lock:
            if not self.closed:
                self.candidates.setdefault(stage, set()).update(tickers)

    def record(self, analysis_id, candidates=(), *, now=None):
        from instrument_metadata import canonical_ticker

        now = now or datetime.now(JST)
        with self.lock:
            self.closed = True
            reads = [dict(row) for row in self.reads]
            candidate_stages = {key: sorted(values) for key, values in self.candidates.items()}
        state = self.state
        windows = {}
        if state:
            for days in {5, 7} | {row["window"] for row in reads}:
                windows[str(days)] = {key: sorted(values) for key, values in state.window(days).items()}
            for row in reads:
                shadow = set(windows[str(row["window"])]["blackout"])
                row["legacy_only"] = sorted(set(row["legacy"]) - shadow)
                row["shadow_only"] = sorted(shadow - set(row["legacy"]))
        universe = set(state.classifications) if state else set()
        all_candidates = {canonical_ticker(ticker) for ticker in candidates if isinstance(ticker, str) and ticker.strip()}
        for values in candidate_stages.values():
            all_candidates.update(values)
        outside = sorted(all_candidates - universe)
        return {
            "schema_version": SCHEMA_VERSION, "analysis_id": analysis_id,
            "observed_date_jst": now.astimezone(JST).date().isoformat(),
            "resolved": bool(state and state.resolved and not self.error),
            "failure_reason": self.error or (state.failure_reason if state else "not_frozen"),
            "input_hash": state.input_hash if state else None,
            "input_date_jst": state.today.isoformat() if state else None,
            "generated_at": state.generated_at if state else None,
            "query_hash": _hash({"input_hash": state.input_hash if state else None,
                                 "windows": sorted(windows)}),
            "unknown_reasons": dict(state.unknown_reasons) if state else {},
            "windows": windows,
            "window_counts": {days: {key: len(values) for key, values in groups.items()}
                              for days, groups in windows.items()},
            "consumer_reads": reads,
            "candidate_stages": candidate_stages,
            "out_of_universe_candidates": outside,
            "out_of_universe_classification": "not_covered",
        }


@contextmanager
def observation_scope():
    token = _CURRENT.set(Observer())
    try:
        yield
    finally:
        _CURRENT.reset(token)


def freeze_current(manager):
    observer = _CURRENT.get()
    if observer is not None:
        observer.freeze(manager)


def capture(consumer, days, values, resolved=None):
    observer = _CURRENT.get()
    if observer is not None:
        try:
            observer.capture(consumer, days, values, resolved)
        except Exception as exc:
            observer.error = "capture_failed: " + type(exc).__name__


def capture_candidates(stage, rows):
    observer = _CURRENT.get()
    if observer is not None:
        try:
            observer.capture_candidates(stage, rows)
        except Exception as exc:
            observer.error = "candidate_capture_failed: " + type(exc).__name__


def finish_synthesis(base_dir, analysis_id, synthesis):
    # Extraction also belongs inside the non-blocking observer boundary.
    # Never iterate arbitrary model output in run_analysis just for telemetry.
    try:
        for key in ("priority_actions", "_filtered_actions", "order_intent_deferred_actions"):
            capture_candidates(key, synthesis.get(key))
        return finish_current(base_dir, analysis_id)
    except Exception as exc:
        return {"recorded": False, "resolved": False, "error": type(exc).__name__ + ": " + str(exc)}


def summarize(records):
    valid_dates = set()
    failed = 0
    for row in records:
        if row.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported observation schema")
        observed = date.fromisoformat(row["observed_date_jst"])
        if row.get("resolved") is True:
            if observed.weekday() < 5:
                valid_dates.add(observed.isoformat())
        else:
            failed += 1
    return {"distinct_valid_weekdays": len(valid_dates), "failed_input_runs": failed,
            "review_ready": len(valid_dates) >= 10, "automatic_promotion": False}


def append_observation(path: Path, record):
    """Serialized check+append, durable flush. Corrupt history is not ignored."""
    from utils import process_lock

    if not isinstance(record.get("analysis_id"), str) or not record["analysis_id"].strip():
        raise ValueError("analysis_id required")
    lock_name = "earnings_observation_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    with process_lock(lock_name, timeout=2):
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        summary = summarize(rows + [record])
        if any(row["analysis_id"] == record["analysis_id"] for row in rows):
            return {"recorded": True, "duplicate": True, **summarize(rows)}
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return {"recorded": True, "duplicate": False, **summary}


def finish_current(base_dir, analysis_id, candidates=()):
    observer = _CURRENT.get()
    try:
        if observer is None:
            raise ValueError("observation context missing")
        record = observer.record(analysis_id, candidates)
        result = append_observation(Path(base_dir) / FILENAME, record)
        return {**result, "resolved": record["resolved"], "error": record["failure_reason"]}
    except Exception as exc:
        return {"recorded": False, "resolved": False, "error": type(exc).__name__ + ": " + str(exc)}

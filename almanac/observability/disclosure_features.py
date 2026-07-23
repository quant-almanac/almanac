"""Append-only store + schema for LLM-derived public-disclosure features.

Phase 0 of the ALMANAC public-disclosure feature pipeline plan.

Each row is one disclosure / news item that a cheap model (DeepSeek V4 Flash)
read and turned into a numeric, evidence-backed "何が変わったか" memo. The row
captures *what changed* as verifiable features plus the point-in-time
provenance needed to prove no look-ahead at validation time.

Two hard Phase-0 invariants are enforced here, in code:

1. **observe_only is always True.** A raw extraction never carries a decision
   mandate. Promotion to a decision input happens later by *joining* a
   certification record (``feature_certifications.jsonl``), never by flipping a
   bit on the stored feature. :func:`make_feature` rejects ``observe_only=False``
   so an unvalidated feature physically cannot claim to be tradeable.
2. **publish_time ≤ ingest_time ≤ compute_time.** The no-look-ahead proof: a
   feature computed *before* the disclosure was public would be impossible, so
   we reject any row that violates the ordering.

Storage is append-only JSONL at ``data/disclosure_features.jsonl`` via
:func:`almanac.observability.append_only_log.append_jsonl_safe`. Idempotency is
on ``(source_event_id, model_id, prompt_version, feature_schema_version)``:
re-running the *same* extractor over the *same* disclosure writes no duplicate,
but re-extracting with a new prompt/model version is a legitimately new row
(the validation harness must know which extractor produced which value).

MVP uses JSONL; migrate to SQLite (plan) only if Phase-1 queries get heavy.
"""

from __future__ import annotations

import fcntl
import hashlib
import math
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .append_only_log import append_jsonl_safe
from .ids import compute_source_event_id, new_row_id

__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "MVP_CORE_FEATURES",
    "AI_CONTEXT_FEATURES",
    "DETERMINISTIC_FEATURES",
    "VALID_SOURCES",
    "VALID_DISCLOSURE_TYPES",
    "DisclosureFeature",
    "make_feature",
    "append_feature",
    "feature_exists",
    "read_features",
    "query_features",
    "default_store_path",
    "placebo_hash_score",
]

FEATURE_SCHEMA_VERSION = "0.5.0"
"""Bump when the feature set or their semantics change. Stored on every row so
the validation harness can compare only like-for-like extractions."""

# MVP-core features are certified first; AI-context features are logged in
# observe_only and may be null when their point-in-time inputs are unavailable.
MVP_CORE_FEATURES: tuple[str, ...] = (
    "directional_score",       # signed [-1, 1]
    "directional_confidence",  # [0, 1]
    "catalyst_specificity",    # [0, 1]
    "contradiction_count",     # int >= 0
)
AI_CONTEXT_FEATURES: tuple[str, ...] = (
    "expectation_gap",          # signed [-1, 1] vs consensus (PIT consensus)
    "narrative_delta",          # [0, 1] tone shift vs prior filing
    "risk_emergence",           # [0, 1] newly prominent risk
    "guidance_credibility",     # [0, 1]
    "second_order_impact",      # list[{ticker, sign}]
    "crowding_hype_score",      # [0, 1]
    "non_obvious_negative",     # [0, 1]
    "price_reaction_divergence",  # signed [-1, 1] feature-vs-realized-move
)
DETERMINISTIC_FEATURES: tuple[str, ...] = (
    "guidance_revision_pct",  # operating-profit revision, unbounded signed ratio
    "monthly_yoy_pct",        # monthly sales YoY change, signed ratio
    "insider_cluster_score",  # distinct open-market buyers in the lookback window
    "activist_flag",          # target-company stake filing by a known activist
    "dilution_flag",          # deterministic JP equity-dilution event
    "dilution_pct",           # explicit dilution ratio when safely parseable
    "going_concern_flag",     # deterministic going-concern / audit anomaly
    "buyback_flag",           # deterministic JP self-share-repurchase event (自己株式取得)
    "buyback_ratio_pct",      # repurchase ratio vs shares outstanding, when safely parseable
    "placebo_hash_score",     # stable meaningless control in [-1, 1]
)

VALID_SOURCES = frozenset({"edgar", "edinet", "tdnet", "news"})
VALID_DISCLOSURE_TYPES = frozenset(
    {
        "earnings", "guidance", "mna", "shelf", "insider", "stake",
        "monthly_sales", "dilution", "buyback", "audit", "other",
    }
)

# [0, 1] unit-interval features.
_UNIT_FEATURES = frozenset(
    {
        "directional_confidence",
        "catalyst_specificity",
        "narrative_delta",
        "risk_emergence",
        "guidance_credibility",
        "crowding_hype_score",
        "non_obvious_negative",
    }
)
# [-1, 1] signed features.
_SIGNED_FEATURES = frozenset(
    {"directional_score", "expectation_gap", "price_reaction_divergence"}
)


def default_store_path() -> Path:
    """Repo-root ``data/disclosure_features.jsonl`` (parents[2] from here)."""
    return Path(__file__).resolve().parents[2] / "data" / "disclosure_features.jsonl"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DisclosureFeature:
    """One stored disclosure-feature row. Use :func:`make_feature` to build."""

    # identity
    feature_id: str
    source_event_id: str
    native_doc_id: str | None
    # point-in-time provenance (the no-look-ahead proof)
    publish_time: str
    ingest_time: str
    compute_time: str
    availability_lag_min: int
    # source
    source: str
    source_url: str | None
    raw_text_sha256: str | None
    raw_archive_ref: str | None
    market: str
    language: str | None
    disclosure_type: str
    # ticker mapping
    ticker: str
    ticker_resolution_method: str | None
    ticker_resolution_confidence: float | None
    # extractor versioning
    model_id: str
    prompt_version: str
    feature_schema_version: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    # MVP-core features
    directional_score: float | None
    directional_confidence: float | None
    catalyst_specificity: float | None
    contradiction_count: int | None
    # AI-context features (nullable; null when inputs not point-in-time available)
    expectation_gap: float | None
    narrative_delta: float | None
    risk_emergence: float | None
    guidance_credibility: float | None
    second_order_impact: list[dict[str, Any]]
    crowding_hype_score: float | None
    non_obvious_negative: float | None
    price_reaction_divergence: float | None
    # deterministic public-document features (nullable)
    guidance_revision_pct: float | None
    monthly_yoy_pct: float | None
    insider_cluster_score: float | None
    activist_flag: bool | None
    dilution_flag: bool | None
    dilution_pct: float | None
    going_concern_flag: bool | None
    buyback_flag: bool | None
    buyback_ratio_pct: float | None
    placebo_hash_score: float
    # evidence + control
    evidence: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    observe_only: bool = True

    def to_row(self) -> dict[str, Any]:
        """Return a plain JSON-serializable dict for the JSONL store."""
        return asdict(self)

    def dedup_key(self) -> tuple[str, str, str, str]:
        """Idempotency key: same extractor over same event = same key."""
        return (
            self.source_event_id,
            self.model_id,
            self.prompt_version,
            self.feature_schema_version,
        )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _to_utc(value: Any) -> datetime:
    """Parse a datetime or ISO string to a UTC-aware datetime.

    Naive inputs are assumed UTC so PIT comparisons never raise on a
    naive/aware mix — the ordering is what matters, not the absolute zone.
    """
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(value: Any) -> str:
    """Normalize a datetime/str timestamp to an ISO string for storage."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _check_range(name: str, value: Any) -> None:
    if value is None:
        return
    if name in _UNIT_FEATURES:
        if not (0.0 <= float(value) <= 1.0):
            raise ValueError(f"{name} must be in [0, 1], got {value}")
    elif name in _SIGNED_FEATURES:
        if not (-1.0 <= float(value) <= 1.0):
            raise ValueError(f"{name} must be in [-1, 1], got {value}")


def placebo_hash_score(source_event_id: str) -> float:
    """Stable, semantically meaningless control feature for harness calibration."""
    digest = hashlib.sha256(source_event_id.encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    return unit * 2.0 - 1.0


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def make_feature(
    *,
    source: str,
    ticker: str,
    publish_time: Any,
    compute_time: Any,
    disclosure_type: str = "other",
    market: str = "US",
    native_doc_id: str | None = None,
    source_url: str | None = None,
    ingest_time: Any | None = None,
    availability_lag_min: int = 0,
    language: str | None = None,
    raw_text_sha256: str | None = None,
    raw_archive_ref: str | None = None,
    ticker_resolution_method: str | None = None,
    ticker_resolution_confidence: float | None = None,
    model_id: str,
    prompt_version: str,
    feature_schema_version: str = FEATURE_SCHEMA_VERSION,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    features: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    summary: str = "",
    observe_only: bool = True,
    feature_id: str | None = None,
    source_event_id: str | None = None,
) -> DisclosureFeature:
    """Build a validated :class:`DisclosureFeature`.

    Fills derived ids (``feature_id`` via UUID, ``source_event_id`` via
    :func:`compute_source_event_id`), defaults ``ingest_time`` to now, validates
    the source/market/disclosure-type enums, the PIT ordering
    (``publish ≤ ingest ≤ compute``), the feature value ranges, and the Phase-0
    ``observe_only`` invariant.

    ``features`` is a flat dict of any subset of :data:`MVP_CORE_FEATURES`,
    :data:`AI_CONTEXT_FEATURES`, and :data:`DETERMINISTIC_FEATURES`; absent
    features are stored as ``None``.

    Raises
    ------
    ValueError
        On any enum/range/PIT/observe_only violation, surfaced at construction
        so a bad row never reaches disk.
    """
    if observe_only is not True:
        raise ValueError(
            "Phase 0: observe_only must be True. Promotion to a decision input "
            "happens by joining a certification record, never by flipping this "
            "flag on a stored feature."
        )

    src = (source or "").strip().lower()
    if src not in VALID_SOURCES:
        raise ValueError(f"source must be one of {sorted(VALID_SOURCES)}, got {source!r}")
    if disclosure_type not in VALID_DISCLOSURE_TYPES:
        raise ValueError(
            f"disclosure_type must be one of {sorted(VALID_DISCLOSURE_TYPES)}, "
            f"got {disclosure_type!r}"
        )
    if market not in {"US", "JP"}:
        raise ValueError(f"market must be 'US' or 'JP', got {market!r}")
    if not ticker:
        raise ValueError("ticker must be non-empty")

    if ingest_time is None:
        ingest_time = datetime.now(timezone.utc)

    pub_dt, ing_dt, com_dt = _to_utc(publish_time), _to_utc(ingest_time), _to_utc(compute_time)
    if not (pub_dt <= ing_dt <= com_dt):
        raise ValueError(
            "PIT ordering violated: require publish_time <= ingest_time <= "
            f"compute_time, got publish={pub_dt.isoformat()}, "
            f"ingest={ing_dt.isoformat()}, compute={com_dt.isoformat()}"
        )

    sid = source_event_id or compute_source_event_id(
        src, native_doc_id=native_doc_id, source_url=source_url
    )

    feats = dict(features or {})
    unknown = (
        set(feats)
        - set(MVP_CORE_FEATURES)
        - set(AI_CONTEXT_FEATURES)
        - set(DETERMINISTIC_FEATURES)
    )
    if unknown:
        raise ValueError(f"unknown feature names: {sorted(unknown)}")
    for name in _UNIT_FEATURES | _SIGNED_FEATURES:
        _check_range(name, feats.get(name))
    for name in (
        "guidance_revision_pct",
        "monthly_yoy_pct",
        "insider_cluster_score",
        "dilution_pct",
        "buyback_ratio_pct",
    ):
        value = feats.get(name)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric, got {value!r}") from exc
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            raise ValueError(f"{name} must be finite, got {value!r}")
        if name == "insider_cluster_score" and numeric < 0:
            raise ValueError("insider_cluster_score must be non-negative")
        if name == "dilution_pct" and not (0.0 <= numeric <= 5.0):
            raise ValueError("dilution_pct must be in [0, 5]")
        if name == "buyback_ratio_pct" and not (0.0 <= numeric <= 30.0):
            raise ValueError("buyback_ratio_pct must be in [0, 30]")
    activist_flag = feats.get("activist_flag")
    if activist_flag is not None and not isinstance(activist_flag, bool):
        raise ValueError("activist_flag must be bool or None")
    dilution_flag = feats.get("dilution_flag")
    if dilution_flag is not None and not isinstance(dilution_flag, bool):
        raise ValueError("dilution_flag must be bool or None")
    going_concern_flag = feats.get("going_concern_flag")
    if going_concern_flag is not None and not isinstance(going_concern_flag, bool):
        raise ValueError("going_concern_flag must be bool or None")
    buyback_flag = feats.get("buyback_flag")
    if buyback_flag is not None and not isinstance(buyback_flag, bool):
        raise ValueError("buyback_flag must be bool or None")
    derived_placebo = placebo_hash_score(sid)
    supplied_placebo = feats.get("placebo_hash_score")
    if supplied_placebo is not None and not math.isclose(
        float(supplied_placebo), derived_placebo, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError("placebo_hash_score is derived from source_event_id and cannot be overridden")
    cc = feats.get("contradiction_count")
    if cc is not None and (int(cc) != cc or cc < 0):
        raise ValueError(f"contradiction_count must be a non-negative int, got {cc}")
    soi = feats.get("second_order_impact") or []
    if not isinstance(soi, list):
        raise ValueError("second_order_impact must be a list of {ticker, sign}")
    for _e in soi:
        if (not isinstance(_e, dict) or not _e.get("ticker")
                or _e.get("sign") not in (-1, 0, 1)):
            raise ValueError(
                "second_order_impact items must be {ticker, sign in {-1,0,1}}, "
                f"got {_e!r}"
            )

    if ticker_resolution_confidence is not None and not (
        0.0 <= float(ticker_resolution_confidence) <= 1.0
    ):
        raise ValueError("ticker_resolution_confidence must be in [0, 1]")

    return DisclosureFeature(
        feature_id=feature_id or new_row_id(),
        source_event_id=sid,
        native_doc_id=native_doc_id,
        publish_time=_iso(publish_time),
        ingest_time=_iso(ingest_time),
        compute_time=_iso(compute_time),
        availability_lag_min=int(availability_lag_min),
        source=src,
        source_url=source_url,
        raw_text_sha256=raw_text_sha256,
        raw_archive_ref=raw_archive_ref,
        market=market,
        language=language,
        disclosure_type=disclosure_type,
        ticker=ticker,
        ticker_resolution_method=ticker_resolution_method,
        ticker_resolution_confidence=ticker_resolution_confidence,
        model_id=model_id,
        prompt_version=prompt_version,
        feature_schema_version=feature_schema_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        directional_score=feats.get("directional_score"),
        directional_confidence=feats.get("directional_confidence"),
        catalyst_specificity=feats.get("catalyst_specificity"),
        contradiction_count=feats.get("contradiction_count"),
        expectation_gap=feats.get("expectation_gap"),
        narrative_delta=feats.get("narrative_delta"),
        risk_emergence=feats.get("risk_emergence"),
        guidance_credibility=feats.get("guidance_credibility"),
        second_order_impact=list(soi),
        crowding_hype_score=feats.get("crowding_hype_score"),
        non_obvious_negative=feats.get("non_obvious_negative"),
        price_reaction_divergence=feats.get("price_reaction_divergence"),
        guidance_revision_pct=feats.get("guidance_revision_pct"),
        monthly_yoy_pct=feats.get("monthly_yoy_pct"),
        insider_cluster_score=feats.get("insider_cluster_score"),
        activist_flag=activist_flag,
        dilution_flag=dilution_flag,
        dilution_pct=feats.get("dilution_pct"),
        going_concern_flag=going_concern_flag,
        buyback_flag=buyback_flag,
        buyback_ratio_pct=feats.get("buyback_ratio_pct"),
        placebo_hash_score=derived_placebo,
        evidence=list(evidence or []),
        summary=summary,
        observe_only=True,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def read_features(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read all stored feature rows (oldest first). Missing file → empty list."""
    import json

    p = Path(path) if path is not None else default_store_path()
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # One corrupt/partial line (migration, manual edit, crash mid-write)
            # must not break every reader (API / catalyst / feature_exists).
            continue
    return rows


def _existing_dedup_keys(path: Path) -> set[tuple[str, str, str, str]]:
    keys: set[tuple[str, str, str, str]] = set()
    for row in read_features(path):
        keys.add(
            (
                row.get("source_event_id"),
                row.get("model_id"),
                row.get("prompt_version"),
                row.get("feature_schema_version"),
            )
        )
    return keys


@contextmanager
def _store_lock(path: Path) -> Iterator[None]:
    """Exclusive lock over a store's check-then-append critical section.

    Uses a sidecar ``<store>.lock`` file rather than the data file itself:
    :func:`append_jsonl_safe` already flocks the data file, and taking a second
    flock on the same file from one process (via a different fd) can deadlock.
    A separate lock file lets ``append_feature`` make its *dedup check + write*
    atomic across concurrent ingest processes without nesting locks.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def append_feature(
    feature: DisclosureFeature,
    *,
    path: Path | str | None = None,
    fsync: bool = True,
) -> dict[str, Any]:
    """Append ``feature`` to the store unless an identical extraction exists.

    Idempotent on :meth:`DisclosureFeature.dedup_key`. Re-running the same
    extractor over the same disclosure is a no-op (``duplicate=True``); a new
    prompt/model/schema version writes a fresh row. The dedup check and the
    write happen under a single :func:`_store_lock`, so two concurrent ingest
    processes cannot both pass the check and double-write the same row.

    Returns
    -------
    dict
        ``{"written": bool, "duplicate": bool, "source_event_id": str,
        "feature_id": str}``.
    """
    p = Path(path) if path is not None else default_store_path()
    with _store_lock(p):
        if feature.dedup_key() in _existing_dedup_keys(p):
            return {
                "written": False,
                "duplicate": True,
                "source_event_id": feature.source_event_id,
                "feature_id": feature.feature_id,
            }
        append_jsonl_safe(p, feature.to_row(), fsync=fsync)
    return {
        "written": True,
        "duplicate": False,
        "source_event_id": feature.source_event_id,
        "feature_id": feature.feature_id,
    }


def feature_exists(
    source_event_id: str,
    *,
    model_id: str,
    prompt_version: str,
    feature_schema_version: str = FEATURE_SCHEMA_VERSION,
    path: Path | str | None = None,
) -> bool:
    """True if an identical extraction already exists in the store.

    Lets an ingest orchestrator skip re-calling the LLM (and re-paying) for a
    disclosure it already extracted with the same extractor version. Matches the
    same key :meth:`DisclosureFeature.dedup_key` uses.
    """
    key = (source_event_id, model_id, prompt_version, feature_schema_version)
    p = Path(path) if path is not None else default_store_path()
    return key in _existing_dedup_keys(p)


def query_features(
    *,
    ticker: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Filter stored rows by ticker / source / publish-date window.

    ``date_from`` / ``date_to`` are inclusive ISO-date(-time) bounds compared
    against ``publish_time``.
    """
    rows = read_features(path)
    out: list[dict[str, Any]] = []
    for row in rows:
        if ticker is not None and row.get("ticker") != ticker:
            continue
        if source is not None and row.get("source") != source.strip().lower():
            continue
        pub = row.get("publish_time") or ""
        if date_from is not None and pub < date_from:
            continue
        if date_to is not None and pub > date_to:
            continue
        out.append(row)
    return out

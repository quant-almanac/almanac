"""Tests for almanac.observability.disclosure_features.

Covers the Phase-0 store contract:

- ``make_feature`` validates enums, feature ranges, the PIT ordering
  (publish ≤ ingest ≤ compute), and the observe_only invariant.
- ``source_event_id`` prefers the native doc id.
- ``append_feature`` is idempotent on (source_event_id, model, prompt, schema);
  a new extractor version writes a new row.
- ``query_features`` filters by ticker / source / publish-date window.
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.disclosure_features import (  # noqa: E402
    FEATURE_SCHEMA_VERSION,
    DisclosureFeature,
    append_feature,
    make_feature,
    placebo_hash_score,
    query_features,
    read_features,
)

_PUB = "2026-06-01T05:00:00+00:00"
_ING = "2026-06-01T05:10:00+00:00"
_COM = "2026-06-01T05:11:00+00:00"


def _feature(**overrides) -> DisclosureFeature:
    kwargs = dict(
        source="edgar",
        ticker="AAPL",
        publish_time=_PUB,
        ingest_time=_ING,
        compute_time=_COM,
        disclosure_type="earnings",
        market="US",
        native_doc_id="0000320193-26-000010",
        source_url="https://sec.gov/x",
        model_id="deepseek-chat",
        prompt_version="p1",
        features={
            "directional_score": 0.6,
            "directional_confidence": 0.8,
            "catalyst_specificity": 0.7,
            "contradiction_count": 1,
        },
        summary="raised FY operating guidance",
    )
    kwargs.update(overrides)
    return make_feature(**kwargs)


# ---------------------------------------------------------------------------
# make_feature — happy path + ids
# ---------------------------------------------------------------------------


def test_make_feature_uses_native_doc_id_for_source_event_id() -> None:
    f = _feature()
    assert f.source_event_id == "edgar:0000320193-26-000010"
    assert f.observe_only is True
    assert f.feature_schema_version == FEATURE_SCHEMA_VERSION
    assert f.placebo_hash_score == placebo_hash_score(f.source_event_id)


def test_placebo_is_stable_and_cannot_be_overridden() -> None:
    a = _feature()
    b = _feature()
    assert a.placebo_hash_score == b.placebo_hash_score
    with pytest.raises(ValueError, match="cannot be overridden"):
        _feature(features={"placebo_hash_score": 0.123})


def test_make_feature_url_fallback_when_no_native_id() -> None:
    f = _feature(native_doc_id=None, source="news", disclosure_type="other")
    assert f.source_event_id.startswith("news:url:")


# ---------------------------------------------------------------------------
# make_feature — invariants
# ---------------------------------------------------------------------------


def test_observe_only_false_rejected() -> None:
    with pytest.raises(ValueError, match="observe_only must be True"):
        _feature(observe_only=False)


def test_pit_ordering_enforced() -> None:
    # compute_time before publish_time → impossible, must reject.
    with pytest.raises(ValueError, match="PIT ordering violated"):
        _feature(compute_time="2026-05-30T00:00:00+00:00")


@pytest.mark.parametrize(
    "feats",
    [
        {"directional_score": 2.0},      # signed out of [-1, 1]
        {"catalyst_specificity": 1.5},   # unit out of [0, 1]
        {"contradiction_count": -1},     # negative count
        {"expectation_gap": -2.0},       # signed out of range
    ],
)
def test_feature_range_validation(feats: dict) -> None:
    with pytest.raises(ValueError):
        _feature(features=feats)


def test_unknown_feature_name_rejected() -> None:
    with pytest.raises(ValueError, match="unknown feature names"):
        _feature(features={"made_up_score": 0.5})


@pytest.mark.parametrize(
    "bad",
    [
        {"source": "twitter"},
        {"disclosure_type": "rumor"},
        {"market": "EU"},
    ],
)
def test_enum_validation(bad: dict) -> None:
    with pytest.raises(ValueError):
        _feature(**bad)


def test_ai_context_features_nullable() -> None:
    f = _feature(features={"directional_score": 0.1})
    assert f.expectation_gap is None
    assert f.narrative_delta is None
    assert f.second_order_impact == []


# ---------------------------------------------------------------------------
# append_feature — idempotency
# ---------------------------------------------------------------------------


def test_append_then_duplicate_is_noop(tmp_path: Path) -> None:
    store = tmp_path / "disclosure_features.jsonl"
    f = _feature()

    r1 = append_feature(f, path=store, fsync=False)
    assert r1["written"] is True and r1["duplicate"] is False

    # Same extractor over the same event → no duplicate row.
    r2 = append_feature(_feature(), path=store, fsync=False)
    assert r2["written"] is False and r2["duplicate"] is True

    assert len(read_features(store)) == 1


def test_new_prompt_version_writes_new_row(tmp_path: Path) -> None:
    store = tmp_path / "disclosure_features.jsonl"
    append_feature(_feature(prompt_version="p1"), path=store, fsync=False)
    r = append_feature(_feature(prompt_version="p2"), path=store, fsync=False)
    assert r["written"] is True
    assert len(read_features(store)) == 2


def test_second_order_impact_sign_constrained() -> None:
    with pytest.raises(ValueError, match=r"sign in"):
        _feature(features={"second_order_impact": [{"ticker": "NVDA", "sign": 99}]})


def test_second_order_impact_valid_sign_accepted() -> None:
    f = _feature(features={"second_order_impact": [{"ticker": "NVDA", "sign": -1}]})
    assert f.second_order_impact == [{"ticker": "NVDA", "sign": -1}]


def test_read_features_skips_corrupt_lines(tmp_path: Path) -> None:
    """A corrupt/partial JSONL line must not break every reader (R-round P2)."""
    store = tmp_path / "f.jsonl"
    append_feature(_feature(native_doc_id="a1"), path=store, fsync=False)
    with store.open("a", encoding="utf-8") as fh:
        fh.write("{ this is not valid json\n")
    append_feature(_feature(native_doc_id="a2"), path=store, fsync=False)
    rows = read_features(store)
    assert len(rows) == 2   # both valid rows returned; corrupt line skipped


# ---------------------------------------------------------------------------
# append_feature — concurrency (P2: dedup check + write must be atomic)
# ---------------------------------------------------------------------------


def _concurrent_same_key_worker(store_str: str) -> None:
    """Build the SAME extraction (same dedup key) and try to append it."""
    from almanac.observability.disclosure_features import append_feature, make_feature

    f = make_feature(
        source="edgar",
        ticker="AAPL",
        publish_time=_PUB,
        ingest_time=_ING,
        compute_time=_COM,
        disclosure_type="earnings",
        market="US",
        native_doc_id="race-doc-1",
        model_id="deepseek-chat",
        prompt_version="p1",
        features={"directional_score": 0.5},
    )
    append_feature(f, path=store_str, fsync=False)


def test_concurrent_same_key_writes_exactly_one_row(tmp_path: Path) -> None:
    """Many processes appending the same dedup key must yield exactly one row."""
    store = tmp_path / "race.jsonl"
    n = 8
    with multiprocessing.Pool(processes=n) as pool:
        pool.map(_concurrent_same_key_worker, [str(store)] * n)
    assert len(read_features(store)) == 1


# ---------------------------------------------------------------------------
# query_features
# ---------------------------------------------------------------------------


def test_query_filters(tmp_path: Path) -> None:
    store = tmp_path / "disclosure_features.jsonl"
    append_feature(_feature(ticker="AAPL", native_doc_id="a1"), path=store, fsync=False)
    append_feature(
        _feature(ticker="7203.T", market="JP", source="tdnet", native_doc_id="t1"),
        path=store,
        fsync=False,
    )

    assert len(query_features(ticker="AAPL", path=store)) == 1
    assert len(query_features(source="tdnet", path=store)) == 1
    assert len(query_features(date_from="2026-06-01", date_to="2026-06-02", path=store)) == 2
    assert len(query_features(date_from="2026-07-01", path=store)) == 0

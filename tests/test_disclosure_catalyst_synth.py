"""Tests for synthesize_from_disclosure_features + the observe_only guarantee.

The safety-critical property: a disclosure-derived hypothesis is observe_only,
so it is logged and outcome-measured but can NEVER reach the Opus prompt
(compact_for_opus) or the decision top (run().top). This is the in-code
enforcement the plan requires.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability import catalyst_layer as cl  # noqa: E402
from almanac.observability.catalyst_layer import (  # noqa: E402
    CatalystOutput,
    _evidence_sufficiency_check,
    compact_for_opus,
    dedupe_by_hypothesis_id,
    synthesize_from_disclosure_features,
)
from almanac.observability.disclosure_features import (  # noqa: E402
    append_feature,
    make_feature,
)

_PUB = "2026-06-01T00:00:00+00:00"
_ING = "2026-06-01T00:05:00+00:00"
_COM = "2026-06-01T00:06:00+00:00"


def _row(**feature_over) -> dict:
    feats = {"directional_score": 0.6, "directional_confidence": 0.8,
             "catalyst_specificity": 0.7, "crowding_hype_score": 0.2}
    feats.update(feature_over.pop("features", {}))
    kwargs = dict(
        source="edgar", ticker="AAPL", publish_time=_PUB, ingest_time=_ING,
        compute_time=_COM, disclosure_type="earnings", market="US",
        native_doc_id="acc-1", model_id="deepseek-chat", prompt_version="p1",
        summary="raised FY guidance above consensus", features=feats,
    )
    kwargs.update(feature_over)
    return make_feature(**kwargs).to_row()


# ---------------------------------------------------------------------------
# synthesizer basics
# ---------------------------------------------------------------------------


def test_synth_builds_observe_only_hypothesis_passing_esg() -> None:
    hyps = synthesize_from_disclosure_features(
        [_row()], analysis_id="a", analysis_date="2026-06-01")
    assert len(hyps) == 1
    h = hyps[0]
    assert h.observe_only is True
    assert h.hypothesis_type == "disclosure_catalyst"
    assert h.primary_source_agent == "disclosure_feature"
    assert h.source_event_id == "edgar:acc-1"
    # Evidence Sufficiency Gate fields present → passes (empty missing list).
    assert h.horizon_days > 0 and h.invalidates_if
    assert _evidence_sufficiency_check(h) == []


def test_synth_direction_maps_to_action_type() -> None:
    up = synthesize_from_disclosure_features(
        [_row(features={"directional_score": 0.4})], analysis_id="a", analysis_date="d")
    down = synthesize_from_disclosure_features(
        [_row(native_doc_id="acc-2", features={"directional_score": -0.4})],
        analysis_id="a", analysis_date="d")
    assert up[0].action_type == "buy"
    assert down[0].action_type == "trim"


@pytest.mark.parametrize("ds", [None, 0])
def test_synth_skips_non_actionable_direction(ds) -> None:
    rows = [_row(features={"directional_score": ds})]
    assert synthesize_from_disclosure_features(rows, analysis_id="a", analysis_date="d") == []


# ---------------------------------------------------------------------------
# HARD GUARANTEE: observe_only never reaches Opus / decision top
# ---------------------------------------------------------------------------


def test_observe_only_excluded_from_compact_for_opus() -> None:
    hyps = synthesize_from_disclosure_features(
        [_row()], analysis_id="a", analysis_date="d")
    out = CatalystOutput(
        as_of="x", n_hypotheses_total=1, n_hypotheses_top=1,
        top=hyps, by_type={"disclosure_catalyst": 1}, all_hypotheses=hyps,
    )
    # The hypothesis has score > 0, so the ONLY reason it is excluded is
    # observe_only — compact_for_opus must return empty.
    assert hyps[0].catalyst_score > 0
    assert compact_for_opus(out) == ""


def test_run_logs_disclosure_but_excludes_from_top(tmp_path: Path) -> None:
    store = tmp_path / "feat.jsonl"
    append_feature(make_feature(
        source="edgar", ticker="AAPL", publish_time=_PUB, ingest_time=_ING,
        compute_time=_COM, disclosure_type="earnings", market="US",
        native_doc_id="acc-1", model_id="deepseek-chat", prompt_version="p1",
        summary="raised guidance", features={"directional_score": 0.6,
        "directional_confidence": 0.8, "catalyst_specificity": 0.7},
    ), path=store, fsync=False)

    log = tmp_path / "catalyst_hypothesis_log.jsonl"
    out = cl.run(
        disclosure_features_path=store,
        catalyst_log_path=log,
        analysis_id="run-1",
        analysis_date="2026-06-01",
        write_log=True,
    )

    # observe_only → not in decision top
    assert all(h.hypothesis_type != "disclosure_catalyst" for h in out.top)

    # ...but it IS written to the log (so outcome_updater can measure it)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    disc = [r for r in rows if r.get("hypothesis_type") == "disclosure_catalyst"]
    assert len(disc) == 1
    assert disc[0]["observe_only"] is True


# ---------------------------------------------------------------------------
# Review fixes: event_at origin, dedupe preservation, malformed-row resilience
# ---------------------------------------------------------------------------


def test_event_at_set_from_feature_compute_time() -> None:
    """Forward-outcome origin must be the feature's compute_time, not run date."""
    h = synthesize_from_disclosure_features(
        [_row()], analysis_id="a", analysis_date="d")[0]
    assert h.event_at == _COM


def test_run_logs_event_at_as_compute_time(tmp_path: Path) -> None:
    store = tmp_path / "feat.jsonl"
    append_feature(make_feature(
        source="edgar", ticker="AAPL", publish_time=_PUB, ingest_time=_ING,
        compute_time=_COM, disclosure_type="earnings", market="US",
        native_doc_id="acc-1", model_id="deepseek-chat", prompt_version="p1",
        summary="raised guidance", features={"directional_score": 0.6},
    ), path=store, fsync=False)
    log = tmp_path / "catalyst_hypothesis_log.jsonl"
    cl.run(disclosure_features_path=store, catalyst_log_path=log,
           analysis_id="run-1", analysis_date="2026-06-01", write_log=True)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    disc = [r for r in rows if r.get("hypothesis_type") == "disclosure_catalyst"][0]
    # event_at is the feature compute_time (origin), NOT the catalyst run "now".
    assert disc["event_at"] == _COM


def test_dedupe_preserves_invalidates_if_and_event_at_passes_esg() -> None:
    """Duplicate rows from the same extractor version collapse safely."""
    rows = [_row(), _row()]  # same source event + extractor version → same id
    hyps = synthesize_from_disclosure_features(rows, analysis_id="a", analysis_date="d")
    deduped = dedupe_by_hypothesis_id(hyps)
    assert len(deduped) == 1
    h = deduped[0]
    assert h.invalidates_if != ""          # P1-a: was dropped before
    assert h.event_at == _COM              # P1-b: preserved through dedupe
    assert _evidence_sufficiency_check(h) == []  # passes ESG (not dropped)


def test_different_extractor_versions_do_not_dedupe() -> None:
    """Each extractor version needs its own forward outcome measurement."""
    rows = [_row(), _row(model_id="other-model")]
    hyps = synthesize_from_disclosure_features(rows, analysis_id="a", analysis_date="d")
    assert len(dedupe_by_hypothesis_id(hyps)) == 2
    assert hyps[0].hypothesis_id != hyps[1].hypothesis_id


def test_malformed_rows_skip_not_crash() -> None:
    """A non-numeric directional_score skips that row; numeric strings coerce."""
    good = _row()
    nonnum = {**good, "directional_score": "high", "source_event_id": "edgar:x1"}
    numstr = {**good, "directional_score": "0.5", "source_event_id": "edgar:x2"}
    hyps = synthesize_from_disclosure_features(
        [nonnum, numstr, good], analysis_id="a", analysis_date="d")
    sids = {h.source_event_id for h in hyps}
    assert "edgar:x1" not in sids   # non-numeric → skipped, no crash
    assert "edgar:x2" in sids       # numeric string → coerced, processed
    assert "edgar:acc-1" in sids    # valid row → processed

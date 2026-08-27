"""Stage 2C: claim provenance の tagged union (claim_provenance.py)。

背景: 「根拠には source_url が必須」という一律ルールは GARCH・IV rank・
内部集計のような URL を持たない内部指標に合わない。型ごとに必須項目が
異なる tagged union (official_web/market_provider/local_artifact/derived)
にする。unverified な claim は confidence・urgency を引き上げる根拠に
使ってはならない、という契約を検証する。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import claim_provenance as cp  # noqa: E402


# ---------------------------------------------------------------------------
# official_web
# ---------------------------------------------------------------------------


def test_official_web_requires_url():
    claim = cp.build_claim_provenance("c1", source_type="official_web", source_ref="not-a-url")
    assert claim.verified is False
    assert "URL" in claim.unverified_reason


def test_official_web_valid_url_is_verified():
    claim = cp.build_claim_provenance("c1", source_type="official_web", source_ref="https://disclosure.edinet-fsa.go.jp/x")
    assert claim.verified is True
    assert claim.unverified_reason is None


# ---------------------------------------------------------------------------
# market_provider
# ---------------------------------------------------------------------------


def test_market_provider_requires_provider_and_symbol_format():
    claim = cp.build_claim_provenance("c2", source_type="market_provider", source_ref="yfinance",
                                        observation_date="2026-07-27")
    assert claim.verified is False  # "yfinance" だけで ":" が無い


def test_market_provider_requires_observation_timestamp():
    claim = cp.build_claim_provenance("c2", source_type="market_provider", source_ref="yfinance:AVGO")
    assert claim.verified is False
    assert "observation" in claim.unverified_reason.lower()


def test_market_provider_valid():
    claim = cp.build_claim_provenance("c2", source_type="market_provider", source_ref="yfinance:AVGO",
                                        observation_date="2026-07-27")
    assert claim.verified is True


# ---------------------------------------------------------------------------
# local_artifact
# ---------------------------------------------------------------------------


def test_local_artifact_requires_path_hash_and_as_of():
    assert cp.build_claim_provenance("c3", source_type="local_artifact").verified is False
    assert cp.build_claim_provenance(
        "c3", source_type="local_artifact", source_ref="technical_state.json",
    ).verified is False  # hash が無い
    assert cp.build_claim_provenance(
        "c3", source_type="local_artifact", source_ref="technical_state.json", artifact_hash="abc123",
    ).verified is False  # as_of が無い


def test_local_artifact_valid():
    claim = cp.build_claim_provenance(
        "c3", source_type="local_artifact", source_ref="technical_state.json",
        artifact_hash="abc123", observation_date="2026-07-27",
    )
    assert claim.verified is True


# ---------------------------------------------------------------------------
# derived
# ---------------------------------------------------------------------------


def test_derived_requires_input_claims_and_calculation_version():
    assert cp.build_claim_provenance("c4", source_type="derived").verified is False
    assert cp.build_claim_provenance(
        "c4", source_type="derived", derived_from_claim_ids=["c1", "c2"],
    ).verified is False  # calculation_version が無い


def test_derived_does_not_require_a_url():
    """本題: derived は URL を要求しない (GARCH・IV rank 等の内部指標を想定)。"""
    claim = cp.build_claim_provenance(
        "garch_vol:AVGO", source_type="derived",
        derived_from_claim_ids=["technical_state:AVGO"],
        calculation_version="gjr_garch_v1",
    )
    assert claim.verified is True
    assert claim.source_ref is None


def test_unknown_source_type_is_unverified():
    claim = cp.build_claim_provenance("c5", source_type="nonsense")
    assert claim.verified is False
    assert "未知の source_type" in claim.unverified_reason


# ---------------------------------------------------------------------------
# can_raise_confidence — 唯一の判定関数
# ---------------------------------------------------------------------------


def test_unverified_claim_cannot_raise_confidence():
    claim = cp.build_claim_provenance("c6", source_type="official_web", source_ref="not-a-url")
    assert cp.can_raise_confidence(claim) is False


def test_verified_claim_can_raise_confidence():
    claim = cp.build_claim_provenance("c6", source_type="official_web", source_ref="https://example.com/x")
    assert cp.can_raise_confidence(claim) is True


def test_build_never_raises_even_when_invalid():
    """本題: 型の必須項目を満たさなくても例外を投げない。呼び出し側が
    「削除」か「unverified 表示」かを選べるようにする契約。"""
    claim = cp.build_claim_provenance("c7", source_type="derived")  # 必須項目ゼロ
    assert isinstance(claim, cp.ClaimProvenance)
    assert claim.verified is False


# ---------------------------------------------------------------------------
# claims_from_bl_views — Stage 0C 連携
# ---------------------------------------------------------------------------


def test_claims_from_bl_views_tags_tier_derived_lineage():
    bl_views_root = {
        "as_of": "2026-07-27 06:00",
        "independent_count": 0,
        "views": {
            "AVGO": {"mean_view": 0.05, "evidence_lineage_ids": ["long", "medium", "short"]},
        },
    }
    claims = cp.claims_from_bl_views(bl_views_root)
    assert len(claims) == 1
    c = claims[0]
    assert c.claim_id == "bl_view:AVGO"
    assert c.source_type == "derived"
    assert c.derived_from_claim_ids == ("tier:long:AVGO", "tier:medium:AVGO", "tier:short:AVGO")
    assert c.verified is True
    assert c.evidence_lineage_id == "long+medium+short"


def test_claims_from_bl_views_marks_missing_lineage_unverified():
    bl_views_root = {
        "independent_count": 0,
        "views": {"XLF": {"mean_view": 0.01, "evidence_lineage_ids": []}},
    }
    claims = cp.claims_from_bl_views(bl_views_root)
    assert claims[0].verified is False  # derived_from_claim_ids が空


def test_claims_from_bl_views_independent_source_is_market_provider():
    """independent_count > 0 の view (bl_alpha_sources 由来) は
    derived でなく market_provider として扱う (実際に外部プロバイダから
    取得された値のため)。"""
    bl_views_root = {
        "as_of": "2026-07-27",
        "independent_count": 1,
        "views": {
            "AVGO": {"mean_view": 0.04, "source": "analyst_consensus", "evidence_lineage_ids": []},
        },
    }
    claims = cp.claims_from_bl_views(bl_views_root)
    assert claims[0].source_type == "market_provider"
    assert claims[0].verified is True


def test_claims_from_bl_views_empty_root_returns_empty_list():
    assert cp.claims_from_bl_views({}) == []
    assert cp.claims_from_bl_views({"views": {}}) == []


def test_snapshot_claims_and_action_claim_preserve_parent_quality(tmp_path):
    import analysis_snapshot as snapshot

    now = datetime(2026, 7, 28, 9, 0)
    for name, payload in {
        # fx_rate_usdjpy_as_of が無いと fx の provenance が "unknown" になり
        # (レビューで指摘・修正済み: last_updated への fallback は fail-open
        # だったため撤去した)、このテストの「全カテゴリ fresh」という前提が
        # 崩れる。last_updated だけでなく FX 固有の as_of も明示する。
        "account.json": {"last_updated": now.isoformat(), "fx_rate_usdjpy_as_of": now.timestamp()},
        "technical_state.json": {"cached_at": now.isoformat()},
        "macro_event_state.json": {"refreshed_at": now.isoformat()},
        "news_signal_candidates.json": {"generated_at": now.isoformat()},
        "short_candidates.json": {"generated_at": now.isoformat()},
        "margin_long_candidates.json": {"generated_at": now.isoformat()},
        "long_term_screen_results.json": {"as_of": now.isoformat()},
        "holdings.json": {},
    }.items():
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    base = snapshot.build_base_snapshot_from_data(
        {"positions": [], "cash_info": {"fx_rate_usdjpy": 150}},
        base_dir=tmp_path,
        now=now,
    )
    enriched = snapshot.build_enriched_snapshot(
        base,
        options_by_ticker_raw={"AVGO": {"iv_rank": 45, "as_of": now.isoformat()}},
        now=now,
    )
    claims = cp.claims_from_decision_snapshot(
        enriched, decision_snapshot_id="snap-1", ticker="AVGO", now=now,
    )
    action_claim = cp.build_action_claim(
        claim_id="snap-1:action:1:AVGO",
        input_claims=[
            c for c in claims
            if c.claim_id.endswith((":holdings", ":cash", ":prices", ":fx", ":options:AVGO"))
        ],
        calculation_version="test-v1",
        evidence_lineage_id="analysis:snap-1",
        now=now,
    )
    assert action_claim.verified is True
    assert cp.claim_to_dict(action_claim)["derived_from_claim_ids"]


def test_action_claim_does_not_launder_unknown_parent():
    unknown = cp.build_claim_provenance(
        "snapshot:prices",
        source_type="local_artifact",
        source_ref="technical_state.json",
        artifact_hash="missing",
        freshness_status="unknown",
    )
    action_claim = cp.build_action_claim(
        claim_id="action:1",
        input_claims=[unknown],
        calculation_version="test-v1",
        evidence_lineage_id="analysis:test",
    )
    assert action_claim.verified is False
    assert "unverified_or_stale_parents" in action_claim.unverified_reason

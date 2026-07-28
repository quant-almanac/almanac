"""claim_provenance.py — Stage 2C: claim単位の出所タグ付け (tagged union)

背景: 「根拠には source_url が必須」という一律ルールは、GARCH ボラティリティ・
IV rank・内部集計のような「そもそも URL を持たない内部指標」に適用できない。
一方で、これらの内部指標も「何を根拠に算出したか」を追跡できないと
Stage 0C (evidence anti-circularity) が防いだのと同じ種類の問題
(自己参照した数値をあたかも外部検証済みであるかのように見せる) を再発しうる。

本モジュールは source_url 一律必須の代わりに、claim の性質ごとに必須項目が
異なる tagged union を定義する:

  - official_web:      公式Web (EDINET/TDNET/IR等) — URL必須
  - market_provider:   相場データプロバイダ (yfinance等) — provider名・symbol・
                        observation timestamp 必須
  - local_artifact:    ローカル成果物ファイル (technical_state.json 等) —
                        artifact path・hash・source_as_of 必須
  - derived:           他 claim からの計算派生 (GARCH・IV rank・BLビュー等) —
                        URL不要。派生元 claim ID と計算バージョンが必須

契約: 型ごとの必須項目を満たさない claim は verified=False のまま返す
(例外を投げて捨てない — 呼び出し側が「削除」か「unverified 表示」かを選べる
ようにする)。verified=False の claim は confidence・urgency を引き上げる根拠
として使ってはならない (can_raise_confidence() が唯一の判定関数)。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Optional

SOURCE_TYPES = ("official_web", "market_provider", "local_artifact", "derived")


@dataclass(frozen=True)
class ClaimProvenance:
    claim_id: str
    source_type: str  # SOURCE_TYPES のいずれか
    source_ref: Optional[str]
    published_at: Optional[str]
    observation_date: Optional[str]
    retrieved_at: str
    freshness_status: str  # "fresh" | "degraded" | "stale" | "unknown"
    derived_from_claim_ids: tuple[str, ...]
    calculation_version: Optional[str]
    artifact_hash: Optional[str]
    evidence_lineage_id: Optional[str]
    verified: bool
    unverified_reason: Optional[str]


def _missing_reason(source_type: str, *, source_ref, derived_from_claim_ids, calculation_version, artifact_hash,
                     observation_date, published_at) -> Optional[str]:
    """型ごとの必須項目チェック。満たしていれば None、欠けていれば理由文字列。"""
    if source_type == "official_web":
        if not source_ref or not str(source_ref).startswith(("http://", "https://")):
            return "official_web には有効な URL (source_ref) が必要"
        return None
    if source_type == "market_provider":
        # provider名・symbol は source_ref に "provider:symbol" 形式で載せる規約。
        if not source_ref or ":" not in str(source_ref):
            return "market_provider には 'provider:symbol' 形式の source_ref が必要"
        if not observation_date and not published_at:
            return "market_provider には observation_date または published_at (観測時刻) が必要"
        return None
    if source_type == "local_artifact":
        if not source_ref:
            return "local_artifact には artifact path (source_ref) が必要"
        if not artifact_hash:
            return "local_artifact には artifact_hash が必要"
        if not observation_date and not published_at:
            return "local_artifact には source_as_of (observation_date/published_at) が必要"
        return None
    if source_type == "derived":
        if not derived_from_claim_ids:
            return "derived には derived_from_claim_ids (派生元 claim) が最低1件必要"
        if not calculation_version:
            return "derived には calculation_version が必要"
        return None
    return f"未知の source_type: {source_type!r}"


def build_claim_provenance(
    claim_id: str,
    *,
    source_type: str,
    source_ref: Optional[str] = None,
    published_at: Optional[str] = None,
    observation_date: Optional[str] = None,
    freshness_status: str = "unknown",
    derived_from_claim_ids: Optional[list[str]] = None,
    calculation_version: Optional[str] = None,
    artifact_hash: Optional[str] = None,
    evidence_lineage_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ClaimProvenance:
    """ClaimProvenance を構築する。

    型ごとの必須項目を満たさない場合も例外は投げない — verified=False で
    返し、unverified_reason に理由を残す。呼び出し側は「この claim を
    削除する」か「unverified 表示のまま保持する」かを選べる。どちらを選んでも
    can_raise_confidence() は False を返すため、確信度・urgency の根拠には
    使われない。
    """
    now = now or datetime.now()
    derived_tuple = tuple(derived_from_claim_ids or [])
    reason = _missing_reason(
        source_type,
        source_ref=source_ref,
        derived_from_claim_ids=derived_tuple,
        calculation_version=calculation_version,
        artifact_hash=artifact_hash,
        observation_date=observation_date,
        published_at=published_at,
    )
    return ClaimProvenance(
        claim_id=claim_id,
        source_type=source_type,
        source_ref=source_ref,
        published_at=published_at,
        observation_date=observation_date,
        retrieved_at=now.isoformat(),
        freshness_status=freshness_status,
        derived_from_claim_ids=derived_tuple,
        calculation_version=calculation_version,
        artifact_hash=artifact_hash,
        evidence_lineage_id=evidence_lineage_id,
        verified=reason is None,
        unverified_reason=reason,
    )


def can_raise_confidence(claim: ClaimProvenance) -> bool:
    """この claim を根拠に confidence・urgency を引き上げてよいかどうかの
    唯一の判定関数。呼び出し側はこの関数を経由すること — verified フィールドを
    直接見て個別に判断すると、将来 verified の意味が拡張されたときに
    整合性が崩れる。"""
    return claim.verified


def claim_to_dict(claim: ClaimProvenance) -> dict:
    """JSON output helper with tuples normalized to lists."""
    value = asdict(claim)
    value["derived_from_claim_ids"] = list(claim.derived_from_claim_ids)
    return value


def claims_from_decision_snapshot(
    enriched,
    *,
    decision_snapshot_id: str,
    ticker: str | None = None,
    now: Optional[datetime] = None,
) -> list[ClaimProvenance]:
    """Build provenance claims from the exact frozen analysis inputs.

    The function intentionally uses duck typing so ``claim_provenance`` does
    not import ``analysis_snapshot`` and create a dependency cycle.
    """
    now = now or datetime.now()
    claims: list[ClaimProvenance] = []
    base = getattr(enriched, "base", None)
    if base is not None:
        for category in ("holdings", "cash", "prices", "fx", "macro", "news", "screening"):
            row = getattr(base, category, None)
            if row is None:
                continue
            claims.append(build_claim_provenance(
                f"{decision_snapshot_id}:{category}",
                source_type="local_artifact",
                source_ref=getattr(row, "source", None),
                observation_date=getattr(row, "source_as_of", None),
                freshness_status=getattr(row, "freshness_status", "unknown"),
                artifact_hash=(
                    getattr(row, "payload_hash", None)
                    or getattr(row, "artifact_hash", None)
                ),
                evidence_lineage_id=f"snapshot:{decision_snapshot_id}:{category}",
                now=now,
            ))
    option_rows = getattr(enriched, "options_by_ticker", {}) or {}
    if ticker and ticker in option_rows:
        row = option_rows[ticker]
        claims.append(build_claim_provenance(
            f"{decision_snapshot_id}:options:{ticker}",
            source_type="market_provider",
            source_ref=f"options_fetcher:{ticker}",
            observation_date=getattr(row, "source_as_of", None),
            freshness_status=getattr(row, "freshness_status", "unknown"),
            artifact_hash=getattr(row, "artifact_hash", None),
            evidence_lineage_id=f"snapshot:{decision_snapshot_id}:options:{ticker}",
            now=now,
        ))
    return claims


def build_action_claim(
    *,
    claim_id: str,
    input_claims: list[ClaimProvenance],
    calculation_version: str,
    evidence_lineage_id: str,
    now: Optional[datetime] = None,
) -> ClaimProvenance:
    """Create a derived action claim and recursively validate its parents.

    ``build_claim_provenance(..., source_type="derived")`` can only check that
    parent IDs exist.  This helper additionally prevents a stale/unverified
    parent from being laundered into a verified derived claim.
    """
    usable = [
        claim for claim in input_claims
        if claim.freshness_status in {"fresh", "degraded"}
    ]
    derived = build_claim_provenance(
        claim_id,
        source_type="derived",
        derived_from_claim_ids=[claim.claim_id for claim in usable],
        calculation_version=calculation_version,
        evidence_lineage_id=evidence_lineage_id,
        freshness_status=(
            "fresh"
            if usable and all(c.freshness_status == "fresh" for c in usable)
            else ("degraded" if usable else "unknown")
        ),
        now=now,
    )
    bad_parents = [
        claim.claim_id for claim in input_claims
        if not claim.verified or claim.freshness_status in {"stale", "unknown"}
    ]
    if bad_parents:
        return replace(
            derived,
            verified=False,
            unverified_reason="unverified_or_stale_parents:" + ",".join(bad_parents),
        )
    return derived


# ---------------------------------------------------------------------------
# Stage 0C 連携: bl_views.json の evidence_lineage_ids を claim provenance 化する
# ---------------------------------------------------------------------------


def claims_from_bl_views(bl_views_root: dict, *, now: Optional[datetime] = None) -> list[ClaimProvenance]:
    """bl_views.json (Stage 0C で evidence_lineage_ids を追加済み) から
    ticker 毎の ClaimProvenance を作る。

    各 view は tier LLM (long/medium/short) からの派生値であり、それ自体は
    URL を持たない内部指標 — source_type="derived" とし、
    derived_from_claim_ids には寄与した lineage (tier名) を入れる。
    calculation_version は _extract_bl_views() の実装バージョンを表す固定文字列。
    """
    now = now or datetime.now()
    views = (bl_views_root or {}).get("views") or {}
    independent_count = (bl_views_root or {}).get("independent_count", 0) or 0
    claims: list[ClaimProvenance] = []
    for ticker, v in views.items():
        if not isinstance(v, dict):
            continue
        lineages = v.get("evidence_lineage_ids") or []
        source = v.get("source")  # bl_alpha_sources 由来 (independent) の場合のみ存在
        if source and independent_count:
            # BL_USE_INDEPENDENT_ALPHA 経由の真に独立な alpha source。
            claims.append(build_claim_provenance(
                f"bl_view:{ticker}",
                source_type="market_provider",
                source_ref=f"{source}:{ticker}",
                observation_date=bl_views_root.get("as_of"),
                freshness_status="fresh",
                now=now,
            ))
            continue
        claims.append(build_claim_provenance(
            f"bl_view:{ticker}",
            source_type="derived",
            derived_from_claim_ids=[f"tier:{t}:{ticker}" for t in lineages],
            calculation_version="extract_bl_views_v1",
            evidence_lineage_id="+".join(lineages) if lineages else None,
            freshness_status="fresh" if lineages else "unknown",
            now=now,
        ))
    return claims

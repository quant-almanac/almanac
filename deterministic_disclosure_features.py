"""Extract public-document features that do not require an LLM."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jp_guidance_parser import PARSER_VERSION as GUIDANCE_VERSION
from jp_guidance_parser import parse_guidance_revision_pct
from jp_monthly_sales_parser import PARSER_VERSION as MONTHLY_VERSION
from jp_monthly_sales_parser import parse_monthly_yoy_pct
from jp_dilution_parser import (
    PARSER_VERSION as DILUTION_VERSION,
    parse_dilution_event,
    parse_going_concern_flag,
)
from jp_buyback_parser import (
    PARSER_VERSION as BUYBACK_VERSION,
    parse_buyback_ratio_pct,
)
from almanac.observability.disclosure_features import append_feature, make_feature

DETERMINISTIC_MODEL_ID = "deterministic"


def extract_deterministic_values(item: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    text = f"{item.get('title', '')}\n{item.get('body', '')}"
    features: dict[str, Any] = {}
    versions: list[str] = []

    if item.get("disclosure_type") == "guidance" or "業績予想" in text or "予想の修正" in text:
        value = parse_guidance_revision_pct(text)
        if value is not None:
            features["guidance_revision_pct"] = value
            versions.append(GUIDANCE_VERSION)

    if item.get("disclosure_type") == "monthly_sales" or "月次" in text:
        value = parse_monthly_yoy_pct(text)
        if value is not None:
            features["monthly_yoy_pct"] = value
            versions.append(MONTHLY_VERSION)

    if item.get("insider_cluster_score") is not None:
        features["insider_cluster_score"] = float(item["insider_cluster_score"])
        versions.append("insider-cluster-1.0")

    if item.get("activist_flag") is True:
        features["activist_flag"] = True
        versions.append("activist-match-1.0")

    dilution_flag, dilution_pct = parse_dilution_event(text)
    if dilution_flag:
        features["dilution_flag"] = True
        features["dilution_pct"] = dilution_pct
        versions.append(DILUTION_VERSION)

    if item.get("disclosure_type") == "buyback" or "自己株式" in text or "自社株" in text:
        buyback_ratio = parse_buyback_ratio_pct(text)
        if buyback_ratio is not None:
            features["buyback_flag"] = True
            features["buyback_ratio_pct"] = buyback_ratio
            versions.append(BUYBACK_VERSION)

    if parse_going_concern_flag(str(item.get("title") or "")):
        features["going_concern_flag"] = True
        versions.append(DILUTION_VERSION)

    return features, versions


def append_deterministic_feature(
    item: dict[str, Any],
    *,
    store_path: Path | str | None = None,
    fsync: bool = True,
) -> dict[str, Any] | None:
    """Append one deterministic ``observe_only`` row, or return ``None``."""

    features, versions = extract_deterministic_values(item)
    if not features:
        return None
    ingest_time = item.get("ingest_time") or datetime.now(timezone.utc)
    feature = make_feature(
        source=item["source"],
        ticker=item["ticker"],
        publish_time=item["publish_time"],
        ingest_time=ingest_time,
        compute_time=datetime.now(timezone.utc),
        disclosure_type=item.get("disclosure_type", "other"),
        market=item.get("market", "US"),
        native_doc_id=item.get("native_doc_id"),
        source_url=item.get("source_url"),
        language=item.get("language"),
        ticker_resolution_method=item.get("ticker_resolution_method"),
        ticker_resolution_confidence=item.get("ticker_resolution_confidence"),
        model_id=DETERMINISTIC_MODEL_ID,
        prompt_version="+".join(sorted(versions)),
        features=features,
        evidence=[],
        summary=item.get("title", "")[:500],
        observe_only=True,
    )
    return append_feature(feature, path=store_path, fsync=fsync)

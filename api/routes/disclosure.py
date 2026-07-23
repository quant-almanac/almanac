"""
GET /api/disclosure-features

Read-only view of the Phase-0 public-disclosure feature store
(``data/disclosure_features.jsonl``). These are LLM-extracted, evidence-backed
"何が変わったか" features that are **observe_only** — surfaced for reference only
and NEVER used for trade decisions until the Phase-1 validation harness certifies
them. The response is explicit about that status so the UI can badge it
未検証 / 参考のみ.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from almanac.observability.disclosure_features import read_features  # noqa: E402

# Fields surfaced to the UI. We deliberately omit raw archive refs / hashes —
# the panel shows the human-readable signal + provenance, not storage internals.
_CORE = (
    "directional_score", "directional_confidence", "catalyst_specificity",
    "contradiction_count",
)
_CONTEXT = (
    "expectation_gap", "narrative_delta", "risk_emergence", "guidance_credibility",
    "second_order_impact", "crowding_hype_score", "non_obvious_negative",
    "price_reaction_divergence",
    "guidance_revision_pct", "monthly_yoy_pct", "insider_cluster_score",
    "activist_flag",
)


def _shape(row: dict) -> dict:
    """Project a stored feature row to the UI-facing shape."""
    return {
        "ticker": row.get("ticker"),
        "market": row.get("market"),
        "source": row.get("source"),
        "disclosure_type": row.get("disclosure_type"),
        "publish_time": row.get("publish_time"),
        "summary": row.get("summary", ""),
        "core": {k: row.get(k) for k in _CORE},
        "context": {k: row.get(k) for k in _CONTEXT},
        "evidence": row.get("evidence", []),
        "source_url": row.get("source_url"),
        "model_id": row.get("model_id"),
        "prompt_version": row.get("prompt_version"),
        # Phase 0: nothing is certified yet, so everything is reference-only.
        "observe_only": bool(row.get("observe_only", True)),
        "status": "unvalidated",
    }


def build_disclosure_response(rows: list, *, ticker: str | None = None,
                              limit: int = 100) -> dict:
    """Pure builder for the endpoint payload (kept HTTP-free for testing).

    Newest first by ``publish_time``; optional ticker filter; capped at ``limit``.
    The top-level fields make the observe_only contract unmistakable to the UI.
    """
    items = rows
    if ticker:
        items = [r for r in items if r.get("ticker") == ticker]
    items = sorted(items, key=lambda r: r.get("publish_time") or "", reverse=True)
    items = [_shape(r) for r in items[: max(0, limit)]]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "observe_only": True,
        "status_note": "未検証 / 参考のみ — 売買判断・サイズ決定には使用していません",
        "features": items,
    }


@router.get("/api/disclosure-features")
async def get_disclosure_features(ticker: str | None = None, limit: int = 100):
    rows = read_features()
    if not isinstance(rows, list):
        rows = []
    return build_disclosure_response(rows, ticker=ticker, limit=limit)

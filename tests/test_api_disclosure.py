"""Tests for the read-only /api/disclosure-features payload builder.

Verifies the observe_only contract is unmistakable in the response, plus
newest-first ordering, ticker filtering, limit, and the UI projection.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.routes.disclosure import build_disclosure_response  # noqa: E402

_ROWS = [
    {"ticker": "AAPL", "market": "US", "source": "edgar", "disclosure_type": "earnings",
     "publish_time": "2026-06-01T00:00:00+00:00", "summary": "older",
     "observe_only": True, "directional_score": 0.6, "directional_confidence": 0.8,
     "catalyst_specificity": 0.7, "contradiction_count": 1, "evidence": [],
     "source_url": "https://x", "model_id": "deepseek-chat", "prompt_version": "p1"},
    {"ticker": "7203.T", "market": "JP", "source": "tdnet", "disclosure_type": "guidance",
     "publish_time": "2026-06-02T00:00:00+00:00", "summary": "newer",
     "observe_only": True, "directional_score": -0.3, "directional_confidence": 0.5,
     "catalyst_specificity": 0.4, "contradiction_count": 0, "evidence": [],
     "source_url": "https://y", "model_id": "deepseek-chat", "prompt_version": "p1"},
]


def test_response_is_explicitly_observe_only() -> None:
    resp = build_disclosure_response(_ROWS)
    assert resp["observe_only"] is True
    assert "参考のみ" in resp["status_note"]
    assert resp["count"] == 2
    assert all(f["observe_only"] is True for f in resp["features"])
    assert all(f["status"] == "unvalidated" for f in resp["features"])


def test_newest_first_ordering() -> None:
    resp = build_disclosure_response(_ROWS)
    assert resp["features"][0]["ticker"] == "7203.T"  # 06-02 before 06-01


def test_shape_splits_core_and_context() -> None:
    f = build_disclosure_response(_ROWS)["features"][0]
    assert "directional_score" in f["core"]
    assert "expectation_gap" in f["context"]
    assert f["source_url"] == "https://y"


def test_ticker_filter_and_limit() -> None:
    assert build_disclosure_response(_ROWS, ticker="AAPL")["count"] == 1
    assert build_disclosure_response(_ROWS, limit=1)["count"] == 1
    assert build_disclosure_response([], )["count"] == 0

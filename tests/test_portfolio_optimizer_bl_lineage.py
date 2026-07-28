"""Stage 0C: Black-Litterman must not consume circular tier-LLM views."""

import json

from portfolio_optimizer import _load_independent_bl_views


def test_only_independent_views_are_loaded(tmp_path):
    path = tmp_path / "bl_views.json"
    path.write_text(
        json.dumps({
            "views": {
                "AVGO": {"mean_view": 0.04, "is_independent": False},
                "XLF": {"mean_view": 0.02, "is_independent": True},
                "META": {"mean_view": 0.03},
            }
        }),
        encoding="utf-8",
    )

    assert _load_independent_bl_views(path) == {
        "XLF": {"mean_view": 0.02, "is_independent": True}
    }


def test_invalid_payload_fails_closed(tmp_path):
    path = tmp_path / "bl_views.json"
    path.write_text("{broken", encoding="utf-8")
    assert _load_independent_bl_views(path) == {}

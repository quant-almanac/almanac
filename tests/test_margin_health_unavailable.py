"""margin_health must not claim "safe" when margin data was never obtained.

analyst.data_gatherer sets ``margin = {}`` when margin_manager.get_summary()
raises, and the analyst tiers then did ``margin.get("margin_status", "safe")``
-- telling the model (and the returned ``margin_health``) that margin was safe
with maintenance_ratio None / collateral 0 / no positions, i.e. a fabricated
all-clear built from missing data. Same class as the loss-guard None->False
collapse fixed earlier: unknown must stay unknown (2026-09 review, P3).
"""
import analyst


def test_margin_long_no_candidates_reports_unavailable_not_safe_when_margin_missing():
    data = {"margin": {}, "screening": {"margin_long_candidates": []}}

    result = analyst._analyze_margin_long(data)

    assert result["margin_health"] == "unavailable"


def test_margin_long_keeps_the_real_status_when_margin_data_exists():
    data = {
        "margin": {"margin_status": "warning", "maintenance_ratio": 140.0},
        "screening": {"margin_long_candidates": []},
    }

    result = analyst._analyze_margin_long(data)

    assert result["margin_health"] == "warning"


def test_margin_long_error_path_reports_unavailable_when_margin_missing(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(analyst, "call_tier_analysis", _boom)
    data = {
        "margin": {},
        "screening": {"margin_long_candidates": [{"ticker": "7203.T", "score": 120}]},
        "cash_info": {},
    }

    result = analyst._analyze_margin_long(data)

    assert result["margin_health"] == "unavailable"


def test_short_selling_prompt_says_unavailable_not_safe_when_margin_missing(monkeypatch):
    captured = {}

    def _capture(system, prompt, **kwargs):
        captured["prompt"] = prompt
        return {"margin_health": "unavailable"}

    monkeypatch.setattr(analyst, "call_tier_analysis", _capture)
    data = {
        "margin": {},
        "screening": {"short_candidates": [{"ticker": "TSLA"}]},
        "news": {},
        "earnings": {},
    }

    analyst._analyze_short_selling(data)

    assert '"status": "unavailable"' in captured["prompt"]
    assert '"status": "safe"' not in captured["prompt"]

from __future__ import annotations

from api.routes import scenario


def test_scenario_indicator_api_hides_unusable_technical_numbers(monkeypatch):
    fresh = {
        "price": 100.0,
        "rsi": 55.0,
        "data_quality_status": "ok",
        "freshness_status": "fresh",
        "data_as_of": "2026-08-31",
    }
    unresolved = {
        **fresh,
        "price": 7.0,
        "rsi": 10.0,
        "rebuild_unresolved": True,
    }

    payloads = {
        "technical_state.json": {
            "cached_at": "2026-08-31T06:00:00+09:00",
            "tickers": {"OK": fresh, "OLD": unresolved},
        },
    }
    monkeypatch.setattr(scenario, "_load", lambda name, default=None: payloads.get(name, default or {}))

    out = scenario._get_indicators()["technical"]["tickers"]
    assert out["OK"]["rsi"] == 55.0
    assert out["OLD"] == {
        "usable": False,
        "reason": "rebuild_unresolved",
        "data_as_of": "2026-08-31",
    }
    assert "price" not in out["OLD"]
    assert "rsi" not in out["OLD"]


def test_scenario_indicator_api_rejects_missing_quality_contract(monkeypatch):
    payloads = {
        "technical_state.json": {"tickers": {"NAKED": {"rsi": 10.0, "price": 1.0}}},
    }
    monkeypatch.setattr(scenario, "_load", lambda name, default=None: payloads.get(name, default or {}))

    row = scenario._get_indicators()["technical"]["tickers"]["NAKED"]
    assert row["usable"] is False
    assert row["reason"] == "data_quality_unknown"
    assert "rsi" not in row

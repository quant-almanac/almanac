from datetime import date

import nav_recorder
from api.routes import performance


def _stub_twr(*, confirmed: bool, excess: float | None = 2.5, days: int = 365, **_kwargs):
    return {
        "confirmed": confirmed,
        "excess_return_pct": excess,
        "period_days_actual": days,
        "twr_pct": 5.0,
        "benchmark_twr_pct": 2.5,
        "error": None,
    }


def _stub_dd(*, confirmed: bool, dd: float | None = -10.0, days: int = 365, **_kwargs):
    return {"confirmed": confirmed, "dd_pct": dd, "period_days_actual": days, "error": None}


def test_objective_status_is_pending_with_insufficient_history(monkeypatch):
    monkeypatch.setattr(nav_recorder, "modified_dietz_twr", lambda **kwargs: _stub_twr(confirmed=False, days=47, **kwargs))
    monkeypatch.setattr(nav_recorder, "compute_max_drawdown", lambda **kwargs: _stub_dd(confirmed=False, days=47, **kwargs))

    result = performance._build_objective_status(today=date(2026, 7, 11))

    assert result["judgment"] == "pending"
    assert result["clean_days"] == 47
    assert result["required_days"] == 365


def test_objective_status_is_met_only_when_both_thresholds_hold(monkeypatch):
    monkeypatch.setattr(nav_recorder, "modified_dietz_twr", lambda **kwargs: _stub_twr(confirmed=True, excess=2.0, **kwargs))
    monkeypatch.setattr(nav_recorder, "compute_max_drawdown", lambda **kwargs: _stub_dd(confirmed=True, dd=-15.0, **kwargs))

    result = performance._build_objective_status(today=date(2026, 7, 11))

    assert result["judgment"] == "met"


def test_objective_status_is_not_met_when_a_confirmed_threshold_fails(monkeypatch):
    monkeypatch.setattr(nav_recorder, "modified_dietz_twr", lambda **kwargs: _stub_twr(confirmed=True, excess=1.9, **kwargs))
    monkeypatch.setattr(nav_recorder, "compute_max_drawdown", lambda **kwargs: _stub_dd(confirmed=True, dd=-16.0, **kwargs))

    result = performance._build_objective_status(today=date(2026, 7, 11))

    assert result["judgment"] == "not_met"


def test_objective_status_handles_missing_nav_without_raising(monkeypatch):
    monkeypatch.setattr(nav_recorder, "modified_dietz_twr", lambda **kwargs: _stub_twr(confirmed=False, excess=None, days=0, **kwargs))
    monkeypatch.setattr(nav_recorder, "compute_max_drawdown", lambda **kwargs: _stub_dd(confirmed=False, dd=None, days=0, **kwargs))

    result = performance._build_objective_status(today=date(2026, 7, 11))

    assert result["judgment"] == "pending"
    assert result["clean_days"] == 0
    assert result["max_dd_12m"]["dd_pct"] is None

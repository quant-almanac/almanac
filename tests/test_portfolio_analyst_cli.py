import pytest


def test_portfolio_analyst_main_writes_success_heartbeat(monkeypatch):
    import portfolio_analyst

    calls = []
    monkeypatch.setattr(
        portfolio_analyst,
        "run_analysis",
        lambda force=False: {
            "as_of": "2026-06-28 10:30",
            "synthesis": {"priority_actions": [{"ticker": "AAPL"}]},
        },
    )
    monkeypatch.setattr(portfolio_analyst, "send_to_telegram", lambda result: calls.append(("telegram", result)))
    monkeypatch.setattr(portfolio_analyst, "heartbeat", lambda *args, **kwargs: calls.append(("heartbeat", args, kwargs)))

    assert portfolio_analyst.main(["--force", "--telegram"]) == 0

    assert calls[0][0] == "telegram"
    assert calls[1] == (
        "heartbeat",
        ("portfolio_analyst", "ok", None),
        {"extra": {"as_of": "2026-06-28 10:30", "priority_actions": 1}},
    )


def test_portfolio_analyst_main_writes_error_heartbeat(monkeypatch):
    import portfolio_analyst

    calls = []

    def _raise(force=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(portfolio_analyst, "run_analysis", _raise)
    monkeypatch.setattr(portfolio_analyst, "heartbeat", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(RuntimeError, match="boom"):
        portfolio_analyst.main(["--force"])

    assert calls == [
        (("portfolio_analyst", "error", "boom"), {}),
    ]

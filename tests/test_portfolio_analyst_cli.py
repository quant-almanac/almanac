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


def test_portfolio_analyst_main_surfaces_a_telegram_failure(monkeypatch):
    """Codex レビュー: Telegram失敗の戻り値を無視していた。

    再現: 送信Falseでも終了コード0・heartbeat=ok だった。分析は動いたが
    Telegramが来ない、を watchdog が検知できない直接原因だった。
    """
    import portfolio_analyst

    calls = []
    monkeypatch.setattr(
        portfolio_analyst,
        "run_analysis",
        lambda force=False: {
            "as_of": "2026-08-24 06:21",
            "synthesis": {"priority_actions": [{"ticker": "VT"}]},
        },
    )
    monkeypatch.setattr(portfolio_analyst, "send_to_telegram", lambda result: False)
    monkeypatch.setattr(portfolio_analyst, "heartbeat", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert portfolio_analyst.main(["--force", "--telegram"]) == 2

    ((script, status, error), kwargs) = calls[0]
    assert script == "portfolio_analyst"
    # 分析自体は成功しているので "error" (重大) ではなく "warn"。
    assert status == "warn"
    assert error and "Telegram" in error
    assert kwargs["extra"]["priority_actions"] == 1


def test_portfolio_analyst_main_treats_a_legacy_none_return_as_success(monkeypatch):
    """None を返す簡易テストダブル・レガシーラッパーとの互換性を壊さない。"""
    import portfolio_analyst

    calls = []
    monkeypatch.setattr(
        portfolio_analyst,
        "run_analysis",
        lambda force=False: {"as_of": "2026-08-24 06:21", "synthesis": {"priority_actions": []}},
    )
    monkeypatch.setattr(portfolio_analyst, "send_to_telegram", lambda result: None)
    monkeypatch.setattr(portfolio_analyst, "heartbeat", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert portfolio_analyst.main(["--force", "--telegram"]) == 0
    assert calls[0][0][1] == "ok"


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

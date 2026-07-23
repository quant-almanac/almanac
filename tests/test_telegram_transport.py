import pytest
import requests

import alert


class _Response:
    def __init__(self, *, payload=None, error=None):
        self._payload = payload if payload is not None else {"ok": True}
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


def test_send_telegram_returns_true_only_after_bot_api_success(monkeypatch):
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(alert, "TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(alert.requests, "post", lambda *args, **kwargs: _Response())

    assert alert.send_telegram("safe") is True


def test_send_telegram_propagates_http_rejection(monkeypatch):
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(alert, "TELEGRAM_CHAT_ID", "chat")
    error = requests.HTTPError("400 Bad Request: can't parse entities")
    monkeypatch.setattr(alert.requests, "post", lambda *args, **kwargs: _Response(error=error))

    with pytest.raises(requests.HTTPError, match="can't parse entities"):
        alert.send_telegram("leverage1.0x<cap1.1x")


def test_send_telegram_without_credentials_is_explicit_failure(monkeypatch):
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(alert, "TELEGRAM_CHAT_ID", "")

    assert alert.send_telegram("message") is False

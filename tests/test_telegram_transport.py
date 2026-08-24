import pytest
import requests

import alert


class _Response:
    def __init__(self, *, payload=None, error=None, status_code=None):
        self._payload = payload if payload is not None else {"ok": True}
        self._error = error
        if status_code is not None:
            self.status_code = status_code

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff delays instead of actually waiting for them."""
    slept: list[float] = []
    monkeypatch.setattr(alert.time, "sleep", slept.append)
    return slept


def _post_sequence(*responses):
    """Return a requests.post double that yields each response/exception once."""
    calls = {"n": 0}

    def _post(*args, **kwargs):
        item = responses[calls["n"]]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    _post.calls = calls
    return _post


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


def _with_credentials(monkeypatch):
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(alert, "TELEGRAM_CHAT_ID", "chat")


def test_transient_dns_failure_is_retried_and_succeeds(monkeypatch, no_sleep):
    """The 2026-08-05 outage: DNS failed once and dropped the whole briefing."""
    _with_credentials(monkeypatch)
    dns_error = requests.ConnectionError(
        "Failed to resolve 'api.telegram.org' (nodename nor servname provided)"
    )
    post = _post_sequence(dns_error, _Response())
    monkeypatch.setattr(alert.requests, "post", post)

    assert alert.send_telegram("briefing") is True
    assert post.calls["n"] == 2
    assert no_sleep == [alert.TELEGRAM_RETRY_BASE_SEC]


def test_connection_error_propagates_after_exhausting_attempts(monkeypatch, no_sleep):
    _with_credentials(monkeypatch)
    post = _post_sequence(*[requests.ConnectionError("dns down")] * alert.TELEGRAM_MAX_ATTEMPTS)
    monkeypatch.setattr(alert.requests, "post", post)

    with pytest.raises(requests.ConnectionError, match="dns down"):
        alert.send_telegram("briefing")
    assert post.calls["n"] == alert.TELEGRAM_MAX_ATTEMPTS
    # Backoff is only paid between attempts, never after the last one.
    assert len(no_sleep) == alert.TELEGRAM_MAX_ATTEMPTS - 1


def test_timeout_is_retried(monkeypatch, no_sleep):
    _with_credentials(monkeypatch)
    post = _post_sequence(requests.Timeout("read timed out"), _Response())
    monkeypatch.setattr(alert.requests, "post", post)

    assert alert.send_telegram("briefing") is True
    assert post.calls["n"] == 2


def test_server_error_is_retried(monkeypatch, no_sleep):
    _with_credentials(monkeypatch)
    post = _post_sequence(
        _Response(status_code=502, error=requests.HTTPError("502 Bad Gateway")),
        _Response(status_code=200),
    )
    monkeypatch.setattr(alert.requests, "post", post)

    assert alert.send_telegram("briefing") is True
    assert post.calls["n"] == 2


def test_rate_limit_honors_retry_after_hint(monkeypatch, no_sleep):
    _with_credentials(monkeypatch)
    post = _post_sequence(
        _Response(status_code=429, payload={"ok": False, "parameters": {"retry_after": 7}}),
        _Response(status_code=200),
    )
    monkeypatch.setattr(alert.requests, "post", post)

    assert alert.send_telegram("briefing") is True
    # Telegram's own hint wins over the exponential backoff.
    assert no_sleep == [7.0]


def test_client_error_is_not_retried(monkeypatch, no_sleep):
    """Malformed HTML is permanent -- retrying only delays the real error."""
    _with_credentials(monkeypatch)
    error = requests.HTTPError("400 Bad Request: can't parse entities")
    post = _post_sequence(_Response(status_code=400, error=error))
    monkeypatch.setattr(alert.requests, "post", post)

    with pytest.raises(requests.HTTPError, match="can't parse entities"):
        alert.send_telegram("leverage1.0x<cap1.1x")
    assert post.calls["n"] == 1
    assert no_sleep == []


def test_bot_api_rejection_is_not_retried(monkeypatch, no_sleep):
    _with_credentials(monkeypatch)
    post = _post_sequence(_Response(status_code=200, payload={"ok": False}))
    monkeypatch.setattr(alert.requests, "post", post)

    with pytest.raises(RuntimeError, match="rejected message"):
        alert.send_telegram("message")
    assert post.calls["n"] == 1


def test_a_connection_timeout_does_not_leak_the_token_when_retries_exhaust(monkeypatch, no_sleep):
    """Codex レビュー: Bot トークンが平文でログファイルに残っていた。

    requests/urllib3 は接続エラーの文字列表現に完全な URL (トークンを
    含むパス) を埋め込む。cron はこの関数の print/例外文字列を
    ログファイルへリダイレクトするので、伏せずに print・re-raise すると
    ディスクに平文で残る (実測: alert_log.txt / ai_analysis_log.txt から
    検出)。
    """
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "SECRET_TOKEN_VALUE")
    monkeypatch.setattr(alert, "TELEGRAM_CHAT_ID", "chat")
    real_error = requests.ConnectionError(
        "HTTPSConnectionPool(host='api.telegram.org', port=443): "
        "Max retries exceeded with url: /botSECRET_TOKEN_VALUE/sendMessage"
    )
    post = _post_sequence(*[real_error] * alert.TELEGRAM_MAX_ATTEMPTS)
    monkeypatch.setattr(alert.requests, "post", post)

    with pytest.raises(requests.ConnectionError) as excinfo:
        alert.send_telegram("briefing")

    assert "SECRET_TOKEN_VALUE" not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


def test_an_http_error_does_not_leak_the_token(monkeypatch):
    """HTTPError.args[0] は "... for url: https://.../bot<TOKEN>/..." を含む。"""
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "SECRET_TOKEN_VALUE")
    monkeypatch.setattr(alert, "TELEGRAM_CHAT_ID", "chat")
    error = requests.HTTPError(
        "404 Client Error: Not Found for url: "
        "https://api.telegram.org/botSECRET_TOKEN_VALUE/sendMessage"
    )
    post = _post_sequence(_Response(status_code=404, error=error))
    monkeypatch.setattr(alert.requests, "post", post)

    with pytest.raises(requests.HTTPError) as excinfo:
        alert.send_telegram("message")

    assert "SECRET_TOKEN_VALUE" not in str(excinfo.value)


def test_the_retry_warning_print_does_not_leak_the_token(monkeypatch, no_sleep, capsys):
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "SECRET_TOKEN_VALUE")
    monkeypatch.setattr(alert, "TELEGRAM_CHAT_ID", "chat")
    dns_error = requests.ConnectionError(
        "Max retries exceeded with url: /botSECRET_TOKEN_VALUE/sendMessage"
    )
    post = _post_sequence(dns_error, _Response())
    monkeypatch.setattr(alert.requests, "post", post)

    assert alert.send_telegram("briefing") is True

    captured = capsys.readouterr()
    assert "SECRET_TOKEN_VALUE" not in captured.out


def test_redaction_preserves_the_exception_type_and_traceback(monkeypatch):
    """既存の呼出元は例外の型で分岐する (requests.HTTPError vs ConnectionError
    vs RuntimeError)。伏字化のために型を変えてはならない。"""
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "")  # 伏字化は無効化
    error = requests.ConnectionError("dns down")
    post = _post_sequence(*[error] * alert.TELEGRAM_MAX_ATTEMPTS)
    monkeypatch.setattr(alert.requests, "post", post)
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "unused")
    monkeypatch.setattr(alert, "TELEGRAM_CHAT_ID", "chat")

    with pytest.raises(requests.ConnectionError, match="dns down"):
        alert.send_telegram("briefing")


def test_no_token_configured_does_not_redact_or_crash(monkeypatch, no_sleep):
    """TELEGRAM_TOKEN が空のときに伏字化ロジック自体が例外を起こさないこと。"""
    monkeypatch.setattr(alert, "TELEGRAM_TOKEN", "")
    assert alert._redact_telegram_token("plain text") == "plain text"

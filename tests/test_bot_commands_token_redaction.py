"""bot_commands.py の Telegram トークン保護。

send_telegram (送信経路) は round 2 で保護したが、getUpdates (受信経路、
main() の while ループがポーリングに使う) は別の requests 呼び出しで、
個別に保護しないと例外の文字列表現 (URL に token を含む) が
main() の `print(f"エラー: {e}")` でそのまま漏れる
(Codex レビュー 2026-08-24 で再現: fake_token_visible=True,
redacted_marker_visible=False)。

bot_commands.py はどの LaunchAgent からも呼ばれていない (telegram-bot
LaunchAgent は telegram_bot.py の方を起動する) が、将来の呼出元がこの
print/traceback を無防備にログへ流すことを防ぐため、他の送信経路と
同じ保護をここにも確認しておく。
"""
import os

# bot_commands はモジュール読み込み時に os.environ[...] を直接引く
# (デフォルト無し)。実環境の値を上書きしないよう setdefault で最小限のみ補う。
os.environ.setdefault("TELEGRAM_TOKEN", "test-placeholder-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test-placeholder-chat")

import pytest
import requests

import bot_commands


def test_send_telegram_does_not_leak_the_token_on_connection_error(monkeypatch):
    monkeypatch.setattr(bot_commands, "TELEGRAM_TOKEN", "SECRET_TOKEN_VALUE")
    monkeypatch.setattr(bot_commands, "TELEGRAM_CHAT_ID", "chat")
    error = requests.ConnectionError(
        "Max retries exceeded with url: /botSECRET_TOKEN_VALUE/sendMessage"
    )

    def _post(*args, **kwargs):
        raise error

    monkeypatch.setattr(bot_commands.requests, "post", _post)

    with pytest.raises(requests.ConnectionError) as excinfo:
        bot_commands.send_telegram("message")

    assert "SECRET_TOKEN_VALUE" not in str(excinfo.value)


def test_get_updates_does_not_leak_the_token_on_connection_error(monkeypatch):
    monkeypatch.setattr(bot_commands, "TELEGRAM_TOKEN", "SECRET_TOKEN_VALUE")
    monkeypatch.setattr(bot_commands, "TELEGRAM_CHAT_ID", "chat")
    error = requests.ConnectionError(
        "Max retries exceeded with url: /botSECRET_TOKEN_VALUE/getUpdates"
    )

    def _get(*args, **kwargs):
        raise error

    monkeypatch.setattr(bot_commands.requests, "get", _get)

    with pytest.raises(requests.ConnectionError) as excinfo:
        bot_commands.get_updates()

    assert "SECRET_TOKEN_VALUE" not in str(excinfo.value)


def test_get_updates_return_value_contract_is_unchanged(monkeypatch):
    """保護の追加が正常系の戻り値契約 (res.json() をそのまま返す) を壊していないこと。"""
    monkeypatch.setattr(bot_commands, "TELEGRAM_TOKEN", "SECRET_TOKEN_VALUE")
    monkeypatch.setattr(bot_commands, "TELEGRAM_CHAT_ID", "chat")

    class _Response:
        def json(self):
            return {"ok": True, "result": []}

    monkeypatch.setattr(bot_commands.requests, "get", lambda *a, **k: _Response())

    assert bot_commands.get_updates() == {"ok": True, "result": []}

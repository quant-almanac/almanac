import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import ipo_watch


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _web_search_stub(query: str) -> list[dict]:
    return [
        {
            "headline": "Nova Robotics debuts on Nasdaq after large IPO",
            "snippet": f"Ticker NVRB began trading after a $3B IPO. query={query}",
            "url": "https://example.com/nvrb-ipo",
        }
    ]


def _extract_nvrb(search_results: list[dict]) -> list[dict]:
    assert search_results
    assert any("NVRB" in row["snippet"] for row in search_results)
    return [
        {
            "company": "Nova Robotics",
            "ticker": "NVRB",
            "exchange": "NASDAQ",
            "ipo_date": "2026-06-18",
            "size_or_rank": "$3B IPO",
            "confidence": 0.91,
        }
    ]


def test_ipo_watch_records_universe_missing_ticker_and_sends_telegram(tmp_path: Path) -> None:
    tickers = tmp_path / "tickers.json"
    state = tmp_path / "data" / "ipo_watch_state.json"
    _write_json(tickers, {"all": ["AAPL", "MSFT"]})
    sent: list[str] = []

    result = ipo_watch.run_watch(
        base_dir=tmp_path,
        tickers_path=tickers,
        state_path=state,
        web_search_fn=_web_search_stub,
        extract_fn=_extract_nvrb,
        telegram_sender=sent.append,
        now=datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert [row["ticker"] for row in result["new_candidates"]] == ["NVRB"]
    # ALMANAC: telegram disabled — ai_analysis only (telegram_sender no longer invoked)
    assert sent == []
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["candidates"][0]["status"] == "universe_missing"
    assert saved["candidates"][0]["onboarding_path"] == "download_tickers.py:NEW_LISTINGS"
    assert saved["candidates"][0]["notified_at"] == "2026-06-18T00:00:00+00:00"
    assert json.loads(tickers.read_text(encoding="utf-8")) == {"all": ["AAPL", "MSFT"]}


def test_ipo_watch_skips_existing_universe_ticker(tmp_path: Path) -> None:
    tickers = tmp_path / "tickers.json"
    state = tmp_path / "data" / "ipo_watch_state.json"
    _write_json(tickers, {"all": ["AAPL", "NVRB"]})
    sent: list[str] = []

    result = ipo_watch.run_watch(
        base_dir=tmp_path,
        tickers_path=tickers,
        state_path=state,
        web_search_fn=_web_search_stub,
        extract_fn=_extract_nvrb,
        telegram_sender=sent.append,
    )

    assert result["new_candidates"] == []
    assert result["skipped_existing"] == ["NVRB"]
    assert sent == []
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["candidates"] == []


def test_ipo_watch_dedups_state_and_does_not_renotify(tmp_path: Path) -> None:
    tickers = tmp_path / "tickers.json"
    state = tmp_path / "data" / "ipo_watch_state.json"
    _write_json(tickers, {"all": ["AAPL"]})
    sent: list[str] = []

    first = ipo_watch.run_watch(
        base_dir=tmp_path,
        tickers_path=tickers,
        state_path=state,
        web_search_fn=_web_search_stub,
        extract_fn=_extract_nvrb,
        telegram_sender=sent.append,
    )
    second = ipo_watch.run_watch(
        base_dir=tmp_path,
        tickers_path=tickers,
        state_path=state,
        web_search_fn=_web_search_stub,
        extract_fn=_extract_nvrb,
        telegram_sender=sent.append,
    )

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert len(saved["candidates"]) == 1
    assert first["new_candidates"][0]["ticker"] == "NVRB"
    assert second["new_candidates"] == []
    assert second["skipped_dedup"] == ["NVRB"]
    # ALMANAC: telegram disabled — ai_analysis only (telegram_sender no longer invoked)
    assert len(sent) == 0


def test_ipo_watch_uses_atomic_write_for_state(tmp_path: Path, monkeypatch) -> None:
    tickers = tmp_path / "tickers.json"
    state = tmp_path / "data" / "ipo_watch_state.json"
    _write_json(tickers, {"all": ["AAPL"]})
    calls: list[Path] = []
    original = ipo_watch.atomic_write_json

    def _spy(path, data, **kwargs):
        calls.append(Path(path))
        return original(path, data, **kwargs)

    monkeypatch.setattr(ipo_watch, "atomic_write_json", _spy)

    ipo_watch.run_watch(
        base_dir=tmp_path,
        tickers_path=tickers,
        state_path=state,
        web_search_fn=_web_search_stub,
        extract_fn=_extract_nvrb,
        telegram_sender=lambda msg: None,
    )

    assert calls == [state]
    assert state.exists()
    assert not list(state.parent.glob("*.tmp"))


def test_ipo_watch_extractor_logs_llm_usage(monkeypatch) -> None:
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        type="tool_use",
                        name="extract_ipo_listings",
                        input={
                            "listings": [
                                {
                                    "company": "Nova Robotics",
                                    "ticker": "NVRB",
                                    "exchange": "NASDAQ",
                                    "ipo_date": "2026-06-18",
                                    "confidence": 0.91,
                                }
                            ]
                        },
                    )
                ],
                stop_reason="tool_use",
                usage=types.SimpleNamespace(input_tokens=456, output_tokens=78),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(ipo_watch, "anthropic", fake_anthropic)
    monkeypatch.setattr(ipo_watch, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    listings = ipo_watch.extract_listings_with_claude(
        [
            {
                "headline": "Nova Robotics debuts on Nasdaq",
                "snippet": "Ticker NVRB began trading after a $3B IPO.",
                "url": "https://example.com/nvrb-ipo",
            }
        ]
    )

    assert listings[0]["ticker"] == "NVRB"
    assert rows, "IPO extraction should be included in logs/llm_calls.jsonl accounting"
    row = rows[-1]
    assert row["role"] == "ipo_watch_extractor"
    assert row["model"] == ipo_watch.MODEL_ID
    assert row["status"] == "ok"
    assert row["input_tokens"] == 456
    assert row["output_tokens"] == 78

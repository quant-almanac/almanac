import json

from analyst import data_gatherer as dg


def test_load_screen_candidates_merges_jp_only_output(tmp_path, monkeypatch):
    (tmp_path / "screen_results.json").write_text(
        json.dumps({
            "timestamp": "2026-05-20 09:00",
            "total_screened": 2,
            "candidates": [
                {"ticker": "AAPL", "ai_signal": "WATCH", "ai_confidence": 40, "score": 30}
            ],
        }),
        encoding="utf-8",
    )
    (tmp_path / "screen_results_jp.json").write_text(
        json.dumps({
            "timestamp": "2026-05-20 15:30",
            "total_screened": 220,
            "candidates": [
                {"ticker": "8306.T", "ai_signal": "WATCH", "ai_confidence": 45, "score": 50}
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(dg, "BASE_DIR", tmp_path)

    candidates, sources = dg._load_screen_candidates()

    by_ticker = {c["ticker"]: c for c in candidates}
    assert set(by_ticker) == {"AAPL", "8306.T"}
    assert by_ticker["8306.T"]["screen_source"] == "jp_only"
    assert by_ticker["8306.T"]["screen_timestamp"] == "2026-05-20 15:30"
    assert {s["source"] for s in sources} == {"all_market", "jp_only"}


def test_load_screen_candidates_keeps_jp_only_longer_than_us_morning(tmp_path, monkeypatch):
    (tmp_path / "screen_results_morning.json").write_text(
        json.dumps({
            "timestamp": "2026-05-21 06:00",
            "total_screened": 1,
            "candidates": [
                {"ticker": "ADI", "ai_signal": "WATCH", "ai_confidence": 60, "score": 40}
            ],
        }),
        encoding="utf-8",
    )
    (tmp_path / "screen_results_jp.json").write_text(
        json.dumps({
            "timestamp": "2026-05-20 15:30",
            "total_screened": 220,
            "candidates": [
                {"ticker": "4208.T", "ai_signal": "WATCH", "ai_confidence": 42, "score": 118}
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(dg, "BASE_DIR", tmp_path)

    candidates, sources = dg._load_screen_candidates()

    by_ticker = {c["ticker"]: c for c in candidates}
    by_source = {s["source"]: s for s in sources}
    assert "4208.T" in by_ticker
    assert by_source["jp_only"]["included"] is True
    assert by_source["jp_only"]["max_age_hours"] == 24

import json

from analyst.data_gatherer import _load_short_candidate_source


def _write(path, *, generated_at, ticker):
    path.write_text(json.dumps({
        "generated_at": generated_at,
        "candidates": [{"ticker": ticker}],
    }), encoding="utf-8")


def test_newer_morning_short_scan_wins(tmp_path):
    _write(tmp_path / "short_candidates.json", generated_at="2026-08-13T18:30:00+09:00", ticker="OLD")
    _write(tmp_path / "short_candidates_morning.json", generated_at="2026-08-14T06:02:00+09:00", ticker="MORNING")

    payload, filename, label = _load_short_candidate_source(tmp_path)

    assert payload["candidates"][0]["ticker"] == "MORNING"
    assert filename == "short_candidates_morning.json"
    assert label == "morning"


def test_newer_regular_short_scan_wins(tmp_path):
    _write(tmp_path / "short_candidates_morning.json", generated_at="2026-08-14T06:02:00+09:00", ticker="MORNING")
    _write(tmp_path / "short_candidates.json", generated_at="2026-08-14T18:30:00+09:00", ticker="EVENING")

    payload, filename, label = _load_short_candidate_source(tmp_path)

    assert payload["candidates"][0]["ticker"] == "EVENING"
    assert filename == "short_candidates.json"
    assert label == "regular"

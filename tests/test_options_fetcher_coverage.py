import json

import options_fetcher as options


def test_option_coverage_preserves_requested_denominator(monkeypatch, tmp_path):
    monkeypatch.setattr(options, "CACHE_DIR", tmp_path)
    (tmp_path / "SPY.json").write_text(
        json.dumps({"error": "rate limited", "fetched_at": "2026-07-31T06:15:00"}),
        encoding="utf-8",
    )
    (tmp_path / "1489.T.json").write_text(
        json.dumps({"error": "no_options", "fetched_at": "2026-07-31T06:15:00"}),
        encoding="utf-8",
    )
    coverage = options.get_option_signal_coverage(
        ["SPY", "1489.T", "AVGO"],
        signals={},
    )
    assert coverage["SPY"]["status"] == "error"
    assert coverage["1489.T"]["status"] == "not_applicable"
    assert coverage["AVGO"]["status"] == "error"


def test_option_coverage_marks_successful_signal_available():
    coverage = options.get_option_signal_coverage(
        ["SPY"],
        signals={"SPY": {"atm_iv": 0.2}},
    )
    assert coverage == {"SPY": {"status": "available", "error": None}}

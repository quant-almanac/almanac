"""Buy blackout uses dated events rather than scan-time countdowns."""
import json
from datetime import date

import analyst


def test_blackout_recomputes_both_date_fields_and_drops_past_events(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    (tmp_path / "earnings_hedge_suggestions.json").write_text(json.dumps({
        "suggestions": [
            {"ticker": "SYNTH_A", "earnings_date": "2026-09-09", "business_days": 8},
            {"ticker": "SYNTH_B", "earnings_date": "2026-09-04", "business_days": 1},
        ],
        "skipped": [
            {"ticker": "SYNTH_C", "earnings": "2026-09-14", "bdays": 20},
            {"ticker": "SYNTH_D", "earnings": "2026-09-15", "bdays": 1},
            {"ticker": "SYNTH_E", "bdays": 1},
        ],
    }))
    assert analyst._load_earnings_blackout(today=date(2026, 9, 7)) == {"SYNTH_A", "SYNTH_C"}

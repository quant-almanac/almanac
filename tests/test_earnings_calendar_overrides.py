from datetime import date
import json

import earnings_proximity_manager as earnings


def test_meta_official_override_precedes_yfinance(monkeypatch, tmp_path):
    path = tmp_path / "earnings_calendar_overrides.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "overrides": {
            "META": {
                "earnings_date": "2026-07-29",
                "source": "issuer",
                "verified_at": "2026-07-24",
                "valid_until": "2026-07-30",
            },
        },
    }), encoding="utf-8")
    monkeypatch.setattr(earnings, "EARNINGS_OVERRIDES", path)

    result = earnings._official_earnings_override("META", today=date(2026, 7, 24))

    assert result["date"] == date(2026, 7, 29)
    assert result["source"] == "issuer"


def test_expired_override_falls_back_to_yfinance(monkeypatch, tmp_path):
    path = tmp_path / "earnings_calendar_overrides.json"
    path.write_text(json.dumps({
        "overrides": {
            "META": {
                "earnings_date": "2026-07-29",
                "source": "issuer",
                "valid_until": "2026-07-30",
            },
        },
    }), encoding="utf-8")
    monkeypatch.setattr(earnings, "EARNINGS_OVERRIDES", path)

    assert earnings._official_earnings_override("META", today=date(2026, 8, 1)) is None


def test_snapshot_requires_current_schema_and_matching_override(monkeypatch, tmp_path):
    override_path = tmp_path / "earnings_calendar_overrides.json"
    output_path = tmp_path / "earnings_hedge_suggestions.json"
    override_path.write_text(json.dumps({
        "overrides": {
            "META": {
                "earnings_date": "2026-07-29",
                "source": "issuer",
                "valid_until": "2026-07-30",
            },
        },
    }), encoding="utf-8")
    output_path.write_text(json.dumps({
        "schema_version": 2,
        "generated_at": "2026-07-24 05:30:00",
        "suggestions": [],
        "skipped": [{
            "ticker": "META",
            "earnings": "2026-07-29",
            "earnings_source": "issuer",
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(earnings, "EARNINGS_OVERRIDES", override_path)
    monkeypatch.setattr(earnings, "OUTPUT", output_path)

    assert earnings.snapshot_is_current(today=date(2026, 7, 24)) is True

    data = json.loads(output_path.read_text(encoding="utf-8"))
    data["skipped"][0]["earnings"] = "2026-07-30"
    output_path.write_text(json.dumps(data), encoding="utf-8")

    assert earnings.snapshot_is_current(today=date(2026, 7, 24)) is False

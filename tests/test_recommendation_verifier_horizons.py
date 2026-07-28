import json
from datetime import datetime, timedelta, timezone

import pandas as pd

import recommendation_verifier as verifier


def _market_frame(start: datetime, periods: int = 90) -> pd.DataFrame:
    index = pd.bdate_range(start=start.date(), periods=periods)
    columns = pd.MultiIndex.from_product([["Close"], ["AVGO", "SPY"]])
    values = [
        [100.0 + i, 400.0 + i]
        for i in range(periods)
    ]
    return pd.DataFrame(values, index=index, columns=columns)


def test_verified_5d_row_is_revisited_for_20d_and_60d(tmp_path, monkeypatch):
    as_of = datetime.now(timezone.utc) - timedelta(days=120)
    original_5d = {"price": 999.0, "outcome_pct": 1.23}
    entries = [{
        "analysis_id": "analysis-1",
        "ticker": "AVGO",
        "type": "buy",
        "tier": "long",
        "urgency": "normal",
        "as_of": as_of.isoformat(),
        "price_at_rec": 100.0,
        "verified": True,
        "verified_at": "2026-01-01T00:00:00+00:00",
        "horizons": {"5d": original_5d},
    }]
    log_path = tmp_path / "recommendations.json"
    log_path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(verifier, "LOG_PATH", log_path)
    monkeypatch.setattr(
        verifier.yf,
        "download",
        lambda *args, **kwargs: _market_frame(as_of),
    )

    result = verifier.verify_recommendations()
    saved = json.loads(log_path.read_text(encoding="utf-8"))[0]

    assert result["total_newly_verified"] == 0
    assert saved["horizons"]["5d"] == original_5d
    assert set(saved["horizons"]) == {"5d", "20d", "60d"}
    assert saved["verified_at"] == "2026-01-01T00:00:00+00:00"
    assert "horizons_updated_at" in saved


def test_complete_horizon_row_does_not_download_or_rewrite(tmp_path, monkeypatch):
    as_of = datetime.now(timezone.utc) - timedelta(days=120)
    entries = [{
        "analysis_id": "analysis-2",
        "ticker": "AVGO",
        "type": "buy",
        "tier": "long",
        "urgency": "normal",
        "as_of": as_of.isoformat(),
        "price_at_rec": 100.0,
        "verified": True,
        "horizons": {
            "5d": {"price": 105.0, "outcome_pct": 5.0},
            "20d": {"price": 120.0, "outcome_pct": 20.0},
            "60d": {"price": 160.0, "outcome_pct": 60.0},
        },
        "benchmark_horizons": {"5d": 1.0, "20d": 2.0, "60d": 3.0},
    }]
    log_path = tmp_path / "recommendations.json"
    original = json.dumps(entries, sort_keys=True)
    log_path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(verifier, "LOG_PATH", log_path)

    def unexpected_download(*args, **kwargs):
        raise AssertionError("complete rows must not fetch market data")

    monkeypatch.setattr(verifier.yf, "download", unexpected_download)
    verifier.verify_recommendations()
    saved = json.loads(log_path.read_text(encoding="utf-8"))
    assert json.dumps(saved, sort_keys=True) == original

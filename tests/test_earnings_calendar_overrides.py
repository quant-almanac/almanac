from datetime import date, datetime
import json
from zoneinfo import ZoneInfo

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
    holdings = [{"ticker": "META", "shares": 1.0, "currency": "USD"}]
    output_path.write_text(json.dumps({
        "schema_version": earnings.OUTPUT_SCHEMA_VERSION,
        "generated_at": "2026-07-24 05:30:00",
        "holdings_scanned": 1,
        "holdings_snapshot_sha256": earnings._holdings_snapshot_sha256(holdings),
        "portfolio_jpy": 30_000_000.0,
        "portfolio_jpy_source": "formal_analysis",
        "portfolio_jpy_as_of": "2026-07-24T05:00:00+09:00",
        "usd_jpy": 150.0,
        "fx_rate_source": "cache",
        "fx_rate_usdjpy_as_of": "2026-07-24T05:00:00+09:00",
        "suggestions": [],
        "skipped": [{
            "ticker": "META",
            "earnings": "2026-07-29",
            "earnings_source": "issuer",
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(earnings, "EARNINGS_OVERRIDES", override_path)
    monkeypatch.setattr(earnings, "OUTPUT", output_path)
    monkeypatch.setattr(earnings, "_load_holdings", lambda: holdings)

    # S2: snapshot_is_current は消費時点での NAV/FX 実年齢と generated_at の
    # 未来拒否も検証するため、実時計の「今日」ではなく、この fixture の
    # タイムスタンプ（2026-07-24 05:00/05:30 JST）に近い固定 now を注入する
    # （fix_plan_v2.md の P7 対応方針: 固定 now の注入に統一）。
    fixed_now = datetime(2026, 7, 24, 5, 35, tzinfo=ZoneInfo("Asia/Tokyo"))

    assert earnings.snapshot_is_current(today=date(2026, 7, 24), now=fixed_now) is True

    data = json.loads(output_path.read_text(encoding="utf-8"))
    data["skipped"][0]["earnings"] = "2026-07-30"
    output_path.write_text(json.dumps(data), encoding="utf-8")

    assert earnings.snapshot_is_current(today=date(2026, 7, 24), now=fixed_now) is False

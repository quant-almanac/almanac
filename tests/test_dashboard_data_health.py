import json
from datetime import datetime, timedelta, timezone

from api.routes.dashboard import _build_data_health, _extract_timestamp


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_extract_timestamp_prefers_explicit_state_timestamp():
    value, source = _extract_timestamp({
        "cached_at": "2026-05-25T08:00:00",
        "updated_at": "2026-05-24T08:00:00",
    })

    assert value == "2026-05-25T08:00:00"
    assert source == "cached_at"


def test_dashboard_data_health_reports_stale_and_missing_sources(tmp_path):
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=72)).isoformat()

    _write_json(tmp_path / "guard_state.json", {"updated_at": fresh})
    _write_json(tmp_path / "regime_state.json", {"updated": old})
    _write_json(tmp_path / "ai_portfolio_analysis.json", {"as_of": fresh})
    _write_json(tmp_path / "scenario_state.json", {"evaluated_at": fresh})
    _write_json(tmp_path / "vix_state.json", {"cached_at": fresh})
    _write_json(tmp_path / "technical_state.json", {"cached_at": fresh})
    _write_json(tmp_path / "macro_state.json", {"cached_at": fresh})
    # news_sentiment_summary.json intentionally missing

    health = _build_data_health(base_dir=tmp_path)

    assert health["ok"] is False
    assert "regime" in health["stale_sources"]
    assert "news_sentiment" in health["missing_sources"]
    assert health["sources"]["guard"]["stale"] is False
    assert health["sources"]["regime"]["stale"] is True
    assert health["sources"]["news_sentiment"]["exists"] is False


def test_dashboard_data_health_falls_back_to_mtime(tmp_path):
    now = datetime.now(timezone.utc)
    for filename in [
        "guard_state.json",
        "regime_state.json",
        "ai_portfolio_analysis.json",
        "scenario_state.json",
        "vix_state.json",
        "technical_state.json",
        "macro_state.json",
        "news_sentiment_summary.json",
    ]:
        _write_json(tmp_path / filename, {"value": 1})

    health = _build_data_health(base_dir=tmp_path)

    assert health["ok"] is True
    assert health["sources"]["guard"]["timestamp_source"] == "mtime"
    assert health["sources"]["guard"]["age_hours"] is not None
    assert health["sources"]["guard"]["age_hours"] < 1


def test_dashboard_data_health_applies_weekend_grace_to_weekday_cron_sources(tmp_path):
    jst = timezone(timedelta(hours=9))
    monday_morning = datetime(2026, 5, 25, 7, 30, tzinfo=jst)
    friday_evening = datetime(2026, 5, 22, 18, 5, tzinfo=jst).isoformat()
    fresh = (monday_morning - timedelta(hours=1)).isoformat()

    _write_json(tmp_path / "guard_state.json", {"updated_at": fresh})
    _write_json(tmp_path / "regime_state.json", {"updated": fresh})
    _write_json(tmp_path / "ai_portfolio_analysis.json", {"as_of": fresh})
    _write_json(tmp_path / "scenario_state.json", {"evaluated_at": friday_evening})
    _write_json(tmp_path / "vix_state.json", {"cached_at": fresh})
    _write_json(tmp_path / "technical_state.json", {"cached_at": friday_evening})
    _write_json(tmp_path / "macro_state.json", {"cached_at": fresh})
    _write_json(tmp_path / "news_sentiment_summary.json", {"as_of": fresh})

    health = _build_data_health(base_dir=tmp_path, now=monday_morning)

    assert health["sources"]["scenario"]["age_hours"] > 60
    assert health["sources"]["scenario"]["stale_after_hours"] == 72
    assert health["sources"]["scenario"]["stale"] is False
    assert health["sources"]["technical"]["stale"] is False


def test_dashboard_data_health_expires_weekday_cron_sources_after_monday_first_run(tmp_path):
    jst = timezone(timedelta(hours=9))
    monday_after_first_run = datetime(2026, 5, 25, 10, 0, tzinfo=jst)
    friday_evening = datetime(2026, 5, 22, 18, 5, tzinfo=jst).isoformat()
    fresh = (monday_after_first_run - timedelta(hours=1)).isoformat()

    _write_json(tmp_path / "guard_state.json", {"updated_at": fresh})
    _write_json(tmp_path / "regime_state.json", {"updated": fresh})
    _write_json(tmp_path / "ai_portfolio_analysis.json", {"as_of": fresh})
    _write_json(tmp_path / "scenario_state.json", {"evaluated_at": friday_evening})
    _write_json(tmp_path / "vix_state.json", {"cached_at": fresh})
    _write_json(tmp_path / "technical_state.json", {"cached_at": friday_evening})
    _write_json(tmp_path / "macro_state.json", {"cached_at": fresh})
    _write_json(tmp_path / "news_sentiment_summary.json", {"as_of": fresh})

    health = _build_data_health(base_dir=tmp_path, now=monday_after_first_run)

    assert health["sources"]["scenario"]["stale_after_hours"] == 24
    assert health["sources"]["scenario"]["stale"] is True
    assert health["sources"]["technical"]["stale"] is True


def test_weekend_grace_decision_is_independent_of_host_timezone(tmp_path, monkeypatch):
    """週末猶予の判定はホストの実行環境タイムゾーンに依存してはならない。

    以前は bare .astimezone() (引数無し) でホスト OS のローカルタイムゾーン
    へ変換しており、開発機 (JST 設定) では「月曜 10:00 JST」のまま
    weekday()/hour を判定できていたが、UTC 設定のホストでは同じ injected
    now が「月曜 01:00 UTC」へ変換され、hour<9 の条件が誤って真になり
    週末猶予が発動していた (レビューが導入した CI 分離で初めて実行され、
    assert 72 == 24 として実測)。
    """
    jst = timezone(timedelta(hours=9))
    monday_after_first_run = datetime(2026, 5, 25, 10, 0, tzinfo=jst)
    friday_evening = datetime(2026, 5, 22, 18, 5, tzinfo=jst).isoformat()
    fresh = (monday_after_first_run - timedelta(hours=1)).isoformat()

    _write_json(tmp_path / "guard_state.json", {"updated_at": fresh})
    _write_json(tmp_path / "regime_state.json", {"updated": fresh})
    _write_json(tmp_path / "ai_portfolio_analysis.json", {"as_of": fresh})
    _write_json(tmp_path / "scenario_state.json", {"evaluated_at": friday_evening})
    _write_json(tmp_path / "vix_state.json", {"cached_at": fresh})
    _write_json(tmp_path / "technical_state.json", {"cached_at": friday_evening})
    _write_json(tmp_path / "macro_state.json", {"cached_at": fresh})
    _write_json(tmp_path / "news_sentiment_summary.json", {"as_of": fresh})

    import time as _time

    # ホストのローカルタイムゾーンを UTC (CI ランナーの実際の設定) へ
    # 固定して、結果が JST 設定の開発機と一致することを確認する。
    original_tz = __import__("os").environ.get("TZ")
    __import__("os").environ["TZ"] = "UTC"
    _time.tzset()
    try:
        health = _build_data_health(base_dir=tmp_path, now=monday_after_first_run)
    finally:
        if original_tz is None:
            __import__("os").environ.pop("TZ", None)
        else:
            __import__("os").environ["TZ"] = original_tz
        _time.tzset()

    assert health["sources"]["scenario"]["stale_after_hours"] == 24, (
        "ホストが UTC のとき、JST 基準の週末猶予判定が崩れている")
    assert health["sources"]["scenario"]["stale"] is True

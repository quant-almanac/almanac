from datetime import date, datetime, timezone
import json

import technical_signals as ts
from analyst import _ensure_technical_state_fresh


def test_session_lag_and_freshness_contract_without_wall_clock():
    assert ts._session_lag("XLF", date(2026, 7, 13), expected=date(2026, 7, 13)) == 0
    assert ts._session_lag("XLF", date(2026, 7, 10), expected=date(2026, 7, 13)) == 1
    assert ts._session_lag("XLF", date(2026, 7, 9), expected=date(2026, 7, 13)) == 2
    assert ts._freshness_status(0) == "fresh"
    assert ts._freshness_status(1) == "degraded"
    assert ts._freshness_status(2) == "stale"


def test_force_refresh_bypasses_fresh_wrapper_cache(monkeypatch, tmp_path):
    cached = {"cached_at": "2999-01-01T00:00:00+00:00", "tickers": {"OLD": {}}}
    computed = {"cached_at": "2026-07-14T00:00:00+00:00", "tickers": {"NEW": {}}}
    calls = []
    monkeypatch.setattr(ts, "CACHE_FILE", tmp_path / "technical_state.json")
    monkeypatch.setattr(ts, "load_json", lambda path, default: cached)
    monkeypatch.setattr(ts, "compute_technical_state", lambda: calls.append("compute") or computed)
    monkeypatch.setattr(ts, "atomic_write_json", lambda path, value: calls.append((path, value)))

    assert ts.get_technical_context() is cached
    assert calls == []
    assert ts.get_technical_context(force=True) is computed
    assert calls[0] == "compute"


def test_screener_candidates_are_included_in_technical_universe(monkeypatch, tmp_path):
    (tmp_path / "holdings.json").write_text("{}", encoding="utf-8")
    (tmp_path / "scenario_playbook.json").write_text("{}", encoding="utf-8")
    (tmp_path / "margin_long_candidates.json").write_text(json.dumps({
        "candidates": [{"ticker": "MDB"}, {"ticker": "RCL"}],
    }), encoding="utf-8")
    (tmp_path / "short_candidates.json").write_text(json.dumps({
        "candidates": [{"ticker": "CVNA"}],
    }), encoding="utf-8")
    monkeypatch.setattr(ts, "BASE_DIR", tmp_path)

    universe = ts._build_ticker_universe()

    assert {"MDB", "RCL", "CVNA"} <= set(universe)


def test_analysis_refreshes_when_current_universe_outgrows_cache(monkeypatch, tmp_path):
    path = tmp_path / "technical_state.json"
    path.write_text(json.dumps({
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "tickers": {"SPY": {}},
        "source_health": {"max_lag_sessions": 0, "missing_count": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(ts, "_build_ticker_universe", lambda: ["SPY", "MDB"])
    calls = []
    assert _ensure_technical_state_fresh(
        base_dir=tmp_path,
        max_age_hours=4,
        refresher=lambda: calls.append("refresh"),
    ) is True
    assert calls == ["refresh"]


def test_analysis_refreshes_legacy_cache_without_quality_schema(monkeypatch, tmp_path):
    path = tmp_path / "technical_state.json"
    path.write_text(json.dumps({
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "tickers": {"SPY": {}},
        "source_health": {"max_lag_sessions": 0, "missing_count": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(ts, "_build_ticker_universe", lambda: ["SPY"])
    calls = []

    assert _ensure_technical_state_fresh(
        base_dir=tmp_path,
        max_age_hours=4,
        refresher=lambda: calls.append("refresh"),
    ) is True
    assert calls == ["refresh"]


def test_analysis_reuses_fresh_complete_cache_with_quality_schema(monkeypatch, tmp_path):
    path = tmp_path / "technical_state.json"
    path.write_text(json.dumps({
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "tickers": {"SPY": {"data_quality_status": "ok"}},
        "source_health": {
            "max_lag_sessions": 0,
            "missing_count": 0,
            "data_quality_counts": {"ok": 1},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(ts, "_build_ticker_universe", lambda: ["SPY"])
    calls = []

    assert _ensure_technical_state_fresh(
        base_dir=tmp_path,
        max_age_hours=4,
        refresher=lambda: calls.append("refresh"),
    ) is False
    assert calls == []


# ---------------------------------------------------------------------------
# 実行内テクニカル補完が鮮度判定を汚染しないこと。
#
# 強制再計算は「ユニバースに居るのに行が無い銘柄」があるだけで走り、しかも
# 無警告なので、実運用のベースライン比較では検出できない。ここが唯一の
# 判定手段になる。
# ---------------------------------------------------------------------------

def _cache(path, tickers, *, universe_extra=()):
    path.write_text(json.dumps({
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "tickers": tickers,
        "source_health": {
            "max_lag_sessions": 0,
            "missing_count": 0,
            "data_quality_counts": {"ok": len(tickers)},
        },
    }), encoding="utf-8")


def test_a_topped_up_row_does_not_trigger_a_forced_rebuild(monkeypatch, tmp_path):
    """topup 行はユニバースの真部分集合ではなく上乗せ。

    universe_is_complete は requested ⊆ cached の包含判定なので余分な行は
    無害だが、quality_schema_current は cached_rows の**全行**を見る。
    topup 行が data_quality_status を欠くと、そこで恒久的な強制再計算になる。
    """
    path = tmp_path / "technical_state.json"
    _cache(path, {
        "SPY": {"data_quality_status": "ok"},
        "MDB": {"data_quality_status": "ok",
                "coverage_source": "topup", "coverage_added_at": "2026-08-24T00:00:00+00:00"},
    })
    monkeypatch.setattr(ts, "_build_ticker_universe", lambda: ["SPY"])
    calls = []

    assert _ensure_technical_state_fresh(
        base_dir=tmp_path, max_age_hours=4,
        refresher=lambda: calls.append("refresh"),
    ) is False
    assert calls == []


def test_an_uncached_registry_entry_forces_one_rebuild_then_self_heals(monkeypatch, tmp_path):
    """レジストリ登録が解決不能だった場合の最悪ケースの上限。

    ユニバースに入って行が無い間は強制再計算が走るが、compute_technical_state
    の追い出しフックがレジストリから外すので自己修復する。

    2026-08-24 のレビューで、1回の欠落だけでは追い出さない仕様に変更した
    (一時的な yfinance 障害と本当の上場廃止を区別するため。
    proposed_ticker_registry.MISSED_REBUILDS_BEFORE_EVICTION 連続で
    欠けて初めて追い出される)。ここでの「最悪ケースの上限」はその回数分の
    強制再計算に伸びる —— それでも有限で、いずれ自己修復することに変わりはない。
    """
    import proposed_ticker_registry

    path = tmp_path / "technical_state.json"
    _cache(path, {"SPY": {"data_quality_status": "ok"}})
    proposed_ticker_registry.record(["ZZQQXX"], resolved={"ZZQQXX"}, base_dir=tmp_path)
    monkeypatch.setattr(ts, "_build_ticker_universe", lambda: ["SPY", "ZZQQXX"])

    calls = []
    assert _ensure_technical_state_fresh(
        base_dir=tmp_path, max_age_hours=4,
        refresher=lambda: calls.append("refresh"),
    ) is True
    assert calls == ["refresh"]

    # 再計算が連続して ZZQQXX を解決できなければ追い出される = ユニバースから消える。
    for _ in range(proposed_ticker_registry.MISSED_REBUILDS_BEFORE_EVICTION):
        proposed_ticker_registry.reconcile_rebuild(
resolved=[], missing=["ZZQQXX"], base_dir=tmp_path, rebuild_coverage=0.99)
    assert proposed_ticker_registry.load_registered(tmp_path) == {}

    monkeypatch.setattr(ts, "_build_ticker_universe", lambda: ["SPY"])
    calls.clear()
    assert _ensure_technical_state_fresh(
        base_dir=tmp_path, max_age_hours=4,
        refresher=lambda: calls.append("refresh"),
    ) is False
    assert calls == []

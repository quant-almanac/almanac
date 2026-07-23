import json

import pytest


def test_run_full_screen_releases_market_data_handles_before_result_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    import insider_restrictions
    import screener
    import utils

    events: list[str] = []
    output_path = tmp_path / "screen_results.json"

    monkeypatch.setattr(screener, "RESULTS_FILE", str(output_path))
    monkeypatch.setattr(screener, "_get_current_regime", lambda: "B_中立")
    monkeypatch.setattr(
        screener,
        "get_market_meta",
        lambda: {"sp500": "上", "nikkei": "上"},
    )
    monkeypatch.setattr(screener, "load_tickers", lambda: ["AAA"])
    monkeypatch.setattr(insider_restrictions, "filter_allowed_tickers", lambda tickers: tickers)
    monkeypatch.setattr(screener, "_bulk_download", lambda tickers: {})
    monkeypatch.setattr(screener, "screen_ticker", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        screener,
        "_release_market_data_handles",
        lambda: events.append("release"),
        raising=False,
    )

    def _write(path, payload):
        events.append("write")
        output_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(utils, "atomic_write_json", _write)

    screener.run_full_screen()

    assert events == ["release", "write"]


def test_run_full_screen_writes_rejection_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    import insider_restrictions
    import screener
    import utils

    output_path = tmp_path / "screen_results.json"

    monkeypatch.setattr(screener, "RESULTS_FILE", str(output_path))
    monkeypatch.setattr(screener, "_get_current_regime", lambda: "B_中立")
    monkeypatch.setattr(screener, "get_market_meta", lambda: {"sp500": "上", "nikkei": "上"})
    monkeypatch.setattr(screener, "load_tickers", lambda: ["AAA"])
    monkeypatch.setattr(insider_restrictions, "filter_allowed_tickers", lambda tickers: tickers)
    monkeypatch.setattr(screener, "_bulk_download", lambda tickers: {})
    monkeypatch.setattr(screener, "screen_ticker", lambda *args, **kwargs: None)
    monkeypatch.setattr(screener, "_release_market_data_handles", lambda: None, raising=False)
    monkeypatch.setattr(utils, "atomic_write_json", lambda path, payload: output_path.write_text(json.dumps(payload)))

    screener.run_full_screen()

    data = json.loads(output_path.read_text())
    assert data["diagnostics"]["download_success_count"] == 0
    assert data["diagnostics"]["download_requested_count"] == 1
    assert data["diagnostics"]["rejection_summary"] == {"no_hist": 1}


def test_run_full_screen_keeps_pre_earnings_momentum_bucket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    import insider_restrictions
    import screener
    import screening_helpers
    import utils

    output_path = tmp_path / "screen_results.json"
    candidate = {
        "ticker": "7203.T",
        "strategy": "決算前モメンタム",
        "priority": 1,
        "price": 3000.0,
        "change_pct": 2.0,
        "gap_pct": 0.0,
        "rsi": 62.0,
        "volume_ratio": 1.0,
        "mom_5d": 3.0,
        "mom_1m": 4.0,
        "mom_3m": 8.0,
        "ma50_dev": 5.0,
        "new_52w_high": False,
        "atr_pct": 3.0,
        "atr": 90.0,
        "stop_loss_atr": 2820.0,
        "reason": "決算前テスト",
        "score": 80.0,
        "is_japan": True,
    }

    monkeypatch.setattr(screener, "RESULTS_FILE", str(output_path))
    monkeypatch.setattr(screener, "_get_current_regime", lambda: "B_中立")
    monkeypatch.setattr(screener, "get_market_meta", lambda: {"sp500": "上", "nikkei": "上"})
    monkeypatch.setattr(screener, "load_tickers", lambda: ["7203.T"])
    monkeypatch.setattr(insider_restrictions, "filter_allowed_tickers", lambda tickers: tickers)
    monkeypatch.setattr(screener, "_bulk_download", lambda tickers: {"7203.T": object()})
    monkeypatch.setattr(screener, "screen_ticker", lambda *args, **kwargs: dict(candidate))
    monkeypatch.setattr(screener, "save_signal_history", lambda candidates: None)
    monkeypatch.setattr(screener, "_release_market_data_handles", lambda: None, raising=False)
    monkeypatch.setattr(screening_helpers, "days_to_next_earnings", lambda ticker: 4)
    monkeypatch.setattr(screening_helpers, "get_historical_win_rate", lambda *args, **kwargs: 0.5)
    monkeypatch.setattr(screening_helpers, "calc_composite_score", lambda **kwargs: 70.0)
    monkeypatch.setattr(screening_helpers, "get_regime_confidence", lambda: 1.0)
    monkeypatch.setattr(utils, "atomic_write_json", lambda path, payload: output_path.write_text(json.dumps(payload)))

    selected, _, _ = screener.run_full_screen()
    data = json.loads(output_path.read_text())

    assert [c["ticker"] for c in selected] == ["7203.T"]
    assert data["strategy_counts"]["決算前モメンタム"] == 1
    assert data["all_candidates"]["決算前モメンタム"][0]["ticker"] == "7203.T"
    assert data["diagnostics"]["unbucketed_strategy_counts"] == {}


def test_release_market_data_handles_resets_yfinance_session(
    monkeypatch: pytest.MonkeyPatch,
):
    import screener
    import utils

    calls: list[str] = []
    monkeypatch.setattr(utils, "reset_yfinance_session", lambda: calls.append("reset"))

    screener._release_market_data_handles()

    assert calls == ["reset"]

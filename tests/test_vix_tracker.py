import numpy as np
import pandas as pd

import vix_tracker


# ── _series_to_history: 純粋関数の変換ロジック ──────────────────────

def test_series_to_history_sorts_ascending_and_rounds():
    index = pd.DatetimeIndex(["2026-08-03", "2026-08-01", "2026-08-02"])
    series = pd.Series([15.001, 14.567, 16.5], index=index)
    assert vix_tracker._series_to_history(series) == [
        {"date": "2026-08-01", "close": 14.57},
        {"date": "2026-08-02", "close": 16.5},
        {"date": "2026-08-03", "close": 15.0},
    ]


def test_series_to_history_excludes_nan():
    index = pd.date_range("2026-08-01", periods=3, freq="D")
    series = pd.Series([14.9, np.nan, 15.3], index=index)
    result = vix_tracker._series_to_history(series)
    assert [row["date"] for row in result] == ["2026-08-01", "2026-08-03"]


def test_series_to_history_none_and_empty_return_empty_list_not_synthesized():
    assert vix_tracker._series_to_history(None) == []
    assert vix_tracker._series_to_history(pd.Series([], dtype=float)) == []


def test_series_to_history_single_point():
    index = pd.DatetimeIndex(["2026-08-05"])
    series = pd.Series([13.2], index=index)
    assert vix_tracker._series_to_history(series) == [{"date": "2026-08-05", "close": 13.2}]


def test_series_to_history_malformed_series_fails_closed():
    # インデックスに strftime が無いオブジェクト(例: 通常の RangeIndex)でも
    # 例外を飲み込んで空配列を返す。合成データは作らない。
    series = pd.Series([1.0, 2.0])
    assert vix_tracker._series_to_history(series) == []


# ── _fetch_all: 実際の yfinance バッチ取得形状での配線確認 ──────────────

def _install_fake_download(monkeypatch, *, vix_dates, vix_values, extra_tickers=None):
    import yfinance as yf

    # group_by="ticker" の実際の yfinance 戻り値は2階層 MultiIndex 列
    # (ticker, field)。フラットな tuple キー列とは別物なので明示的に組む。
    columns = {(vix_tracker.VIX_TICKER, "Close"): vix_values}
    for ticker, values in (extra_tickers or {}).items():
        columns[(ticker, "Close")] = values
    batch_df = pd.DataFrame(columns, index=pd.DatetimeIndex(vix_dates))
    spy_3mo_df = pd.DataFrame({"Close": pd.Series([], dtype=float)})

    def fake_download(*args, **kwargs):
        if kwargs.get("group_by") == "ticker":
            return batch_df
        return spy_3mo_df

    monkeypatch.setattr(yf, "download", fake_download)
    return batch_df


def test_fetch_all_wires_vix_history_from_already_fetched_batch_close(monkeypatch):
    dates = pd.bdate_range("2026-07-01", periods=10)
    values = [15.0 + i * 0.1 for i in range(10)]
    _install_fake_download(monkeypatch, vix_dates=dates, vix_values=values)

    result = vix_tracker._fetch_all()

    assert result is not None
    history = result["vix"]["history_1mo"]
    assert len(history) == 10
    assert history == sorted(history, key=lambda row: row["date"])
    assert history[0]["date"] == dates[0].strftime("%Y-%m-%d")
    assert history[-1]["close"] == round(values[-1], 2)
    # スカラー指標(level/change_1d/5d)は既存どおり動く — 履歴追加が既存契約を壊さない
    assert result["vix"]["level"] == round(values[-1], 2)


def test_fetch_all_wires_history_for_oil_10y_dxy_too(monkeypatch):
    """VIXは「市場の鼓動」の一例 — 同じバッチ取得済みの原油・10年債・DXYも
    再取得なしで history_1mo が付くことを確認する。"""
    dates = pd.bdate_range("2026-07-01", periods=10)
    vix_values = [15.0 + i * 0.1 for i in range(10)]
    oil_values = [78.0 + i * 0.05 for i in range(10)]
    tnx_values = [4.5 + i * 0.01 for i in range(10)]
    dxy_values = [99.0 + i * 0.02 for i in range(10)]
    _install_fake_download(
        monkeypatch, vix_dates=dates, vix_values=vix_values,
        extra_tickers={
            vix_tracker.OIL_TICKER: oil_values,
            vix_tracker.TNX_TICKER: tnx_values,
            vix_tracker.DXY_TICKER: dxy_values,
        },
    )

    result = vix_tracker._fetch_all()

    assert result is not None
    oil_hist = result["oil"]["history_1mo"]
    assert len(oil_hist) == 10
    assert oil_hist[-1]["close"] == round(oil_values[-1], 2)

    tnx_hist = result["yields"]["us_10y_history_1mo"]
    assert len(tnx_hist) == 10
    assert tnx_hist[-1]["close"] == round(tnx_values[-1], 2)
    # 利回りの1日/5日変化は絶対差(pt) — 比ではない
    assert result["yields"]["us_10y_change_1d_pt"] == round(tnx_values[-1] - tnx_values[-2], 2)

    dxy_hist = result["dxy"]["history_1mo"]
    assert len(dxy_hist) == 10
    assert dxy_hist[-1]["close"] == round(dxy_values[-1], 2)


def test_fetch_all_vix_history_empty_when_no_data(monkeypatch):
    import yfinance as yf
    monkeypatch.setattr(yf, "download", lambda *a, **k: None)

    result = vix_tracker._fetch_all()

    assert result is not None
    assert result["vix"]["history_1mo"] == []
    assert result["vix"]["level"] is None


# ── get_vix_context: キャッシュ経路の互換性 ──────────────────────────

def test_get_vix_context_fresh_cache_without_history_key_does_not_crash(monkeypatch, tmp_path):
    """移行処理は追加しない — 履歴が無い古いキャッシュも従来どおり返す。"""
    cache = tmp_path / "vix_state.json"
    legacy_payload = {
        "vix": {"level": 14.9, "classification": "CALM", "change_1d": -1.65, "change_5d": -6.82},
        "cached_at": pd.Timestamp.now().isoformat(),
    }
    import json
    cache.write_text(json.dumps(legacy_payload))
    monkeypatch.setattr(vix_tracker, "CACHE_FILE", cache)
    monkeypatch.setattr(vix_tracker, "_is_cache_fresh", lambda: True)

    ctx = vix_tracker.get_vix_context()

    assert ctx["source"] == "cache"
    assert ctx["vix"]["level"] == 14.9
    assert "history_1mo" not in ctx["vix"]  # 移行処理なし、素通し


def test_get_vix_context_writes_history_1mo_to_cache_on_fresh_fetch(monkeypatch, tmp_path):
    cache = tmp_path / "vix_state.json"
    monkeypatch.setattr(vix_tracker, "CACHE_FILE", cache)
    monkeypatch.setattr(vix_tracker, "_is_cache_fresh", lambda: False)

    fake_result = {"vix": {"level": 14.9, "history_1mo": [{"date": "2026-08-01", "close": 14.9}]}}
    monkeypatch.setattr(vix_tracker, "_fetch_all", lambda: dict(fake_result))

    ctx = vix_tracker.get_vix_context()
    assert ctx["source"] == "yfinance"
    assert ctx["vix"]["history_1mo"] == [{"date": "2026-08-01", "close": 14.9}]

    from utils import load_json
    on_disk = load_json(cache, {})
    assert on_disk["vix"]["history_1mo"] == [{"date": "2026-08-01", "close": 14.9}]


def test_get_vix_context_fetch_failure_falls_back_to_stale_cache_without_history(monkeypatch, tmp_path):
    cache = tmp_path / "vix_state.json"
    import json
    cache.write_text(json.dumps({"vix": {"level": 20.1, "classification": "ELEVATED"}}))
    monkeypatch.setattr(vix_tracker, "CACHE_FILE", cache)
    monkeypatch.setattr(vix_tracker, "_is_cache_fresh", lambda: False)
    monkeypatch.setattr(vix_tracker, "_fetch_all", lambda: None)

    ctx = vix_tracker.get_vix_context()

    assert ctx["source"] == "stale_cache"
    assert ctx["vix"]["level"] == 20.1
    assert "history_1mo" not in ctx["vix"]


def test_get_vix_context_total_failure_fallback_has_empty_history_not_missing_key(monkeypatch, tmp_path):
    cache = tmp_path / "vix_state.json"  # 存在しない
    monkeypatch.setattr(vix_tracker, "CACHE_FILE", cache)
    monkeypatch.setattr(vix_tracker, "_is_cache_fresh", lambda: False)
    monkeypatch.setattr(vix_tracker, "_fetch_all", lambda: None)

    ctx = vix_tracker.get_vix_context()

    assert ctx["source"] == "error"
    assert ctx["vix"]["history_1mo"] == []

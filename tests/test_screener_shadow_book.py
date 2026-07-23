"""tests/test_screener_shadow_book.py — screener候補のobserve-only計測の回帰テスト。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import screener_shadow_book as ssb

# コスト計算を config ファイルに依存させないための最小 config（JP 100k → 20bps×2 = 0.004）。
_CONFIG = {
    "notional_jpy": 100_000,
    "cost_model": {
        "jp_spread_bps_each_side": {"notional_lte_100k": 20, "notional_lte_500k": 10, "larger": 5},
        "us_commission_rate_each_side": 0.00495,
        "us_commission_cap_usd_each_side": 22,
        "us_spread_bps_each_side": 5,
        "rakuten_fx_spread_jpy_per_usd_each_side": 0.25,
        "jp_short": {
            "standard_borrow_rate_annual": 0.011,
            "reverse_daily_fee_buffer_annual": 0.01,
            "general_borrow_rate_annual_max": 0.05,
        },
    },
}


def _write_screen_results(path: Path, candidates: list[dict], timestamp: str) -> None:
    path.write_text(
        json.dumps({"timestamp": timestamp, "candidates": candidates}, ensure_ascii=False),
        encoding="utf-8",
    )


def _make_prices(start: str, opens: list[float], closes: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=len(opens))
    return pd.DataFrame({"Open": opens, "Close": closes}, index=idx)


# ────────────────────────────── capture ──────────────────────────────

def test_capture_appends_new_candidates(tmp_path):
    _write_screen_results(
        tmp_path / "screen_results.json",
        [{"ticker": "7211.T", "strategy": "モメンタム", "price": 361.8, "is_japan": True, "composite_score": 50.0},
         {"ticker": "IBM", "strategy": "ギャップダウン", "price": 200.0, "is_japan": False}],
        "2026-07-10T18:00:00",
    )
    log = tmp_path / "log.jsonl"
    res = ssb.capture_candidates(
        result_files=("screen_results.json",), log_path=log, base_dir=tmp_path,
    )
    assert res["captured"] == 2
    rows = ssb._read_log(log)
    assert {r["ticker"] for r in rows} == {"7211.T", "IBM"}
    assert {r["market"] for r in rows} == {"JP", "US"}
    assert all(r["as_of_date"] == "2026-07-10" for r in rows)


def test_capture_idempotent_same_day(tmp_path):
    _write_screen_results(
        tmp_path / "screen_results.json",
        [{"ticker": "7211.T", "strategy": "モメンタム", "is_japan": True}],
        "2026-07-10T18:00:00",
    )
    log = tmp_path / "log.jsonl"
    first = ssb.capture_candidates(result_files=("screen_results.json",), log_path=log, base_dir=tmp_path)
    second = ssb.capture_candidates(result_files=("screen_results.json",), log_path=log, base_dir=tmp_path)
    assert first["captured"] == 1
    assert second["captured"] == 0
    assert len(ssb._read_log(log)) == 1


def test_capture_cooldown_skips_reappearance_then_allows_new_episode(tmp_path):
    log = tmp_path / "log.jsonl"

    def _cap(as_of):
        _write_screen_results(
            tmp_path / "screen_results.json",
            [{"ticker": "7211.T", "strategy": "モメンタム", "is_japan": True}],
            f"{as_of}T18:00:00",
        )
        return ssb.capture_candidates(
            result_files=("screen_results.json",), log_path=log, base_dir=tmp_path,
            cooldown_days=28,
        )

    assert _cap("2026-01-05")["captured"] == 1
    assert _cap("2026-01-15")["captured"] == 0   # 10日後 → cooldown内でスキップ
    assert _cap("2026-03-01")["captured"] == 1   # 55日後 → 新規エピソード
    rows = ssb._read_log(log)
    assert len(rows) == 2
    assert {r["as_of_date"] for r in rows} == {"2026-01-05", "2026-03-01"}


def test_capture_skips_rows_missing_ticker_or_strategy(tmp_path):
    _write_screen_results(
        tmp_path / "screen_results.json",
        [{"ticker": "7211.T", "strategy": "", "is_japan": True},
         {"ticker": "", "strategy": "モメンタム"},
         {"strategy": "モメンタム"}],
        "2026-07-10T18:00:00",
    )
    log = tmp_path / "log.jsonl"
    res = ssb.capture_candidates(result_files=("screen_results.json",), log_path=log, base_dir=tmp_path)
    assert res["captured"] == 0


# ────────────────────────────── measure ──────────────────────────────

def _seed_log(path: Path, episodes: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ep in episodes:
            f.write(json.dumps(ep, ensure_ascii=False) + "\n")


def test_measure_forward_return_math(tmp_path):
    log = tmp_path / "log.jsonl"
    _seed_log(log, [{
        "episode_id": "e1", "as_of_date": "2026-01-05", "ticker": "7211.T",
        "strategy": "モメンタム", "market": "JP",
    }])
    # 2026-01-06(火)始値100 → +5営業日後の終値110 / +20営業日後の終値130
    opens = [100.0] + [0.0] * 24
    closes = [100, 101, 102, 103, 104, 110, 106, 107, 108, 109,
              110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 130, 131, 132, 133, 134]
    prices = _make_prices("2026-01-06", opens, [float(c) for c in closes])

    book = ssb.measure(
        log_path=log, output_path=None, config=_CONFIG,
        price_loader=lambda ts: {"7211.T": prices},
    )
    by_h = {r["horizon_days"]: r for r in book["returns"]}
    assert by_h[5]["entry_price"] == 100.0
    assert by_h[5]["gross_return"] == 0.10
    # JP 100k round-trip cost = 2 * 20bps = 0.004
    assert by_h[5]["net_return"] == round(0.10 - 0.004, 8)
    assert by_h[20]["gross_return"] == 0.30
    assert by_h[20]["net_return"] == round(0.30 - 0.004, 8)


def test_measure_pending_when_not_matured(tmp_path):
    log = tmp_path / "log.jsonl"
    _seed_log(log, [{
        "episode_id": "e1", "as_of_date": "2026-01-05", "ticker": "AAA",
        "strategy": "モメンタム", "market": "US",
    }])
    # 3営業日分しか無い → 5/20 とも満期未達 → pending、集計対象ゼロ
    prices = _make_prices("2026-01-06", [100.0, 101.0, 102.0], [100.0, 101.0, 102.0])
    book = ssb.measure(
        log_path=log, output_path=None, config=_CONFIG,
        price_loader=lambda ts: {"AAA": prices},
    )
    assert book["measured_return_count"] == 0
    assert book["pending_episode_count"] == 1


def test_measure_partial_maturity_records_only_matured_horizon(tmp_path):
    log = tmp_path / "log.jsonl"
    _seed_log(log, [{
        "episode_id": "e1", "as_of_date": "2026-01-05", "ticker": "AAA",
        "strategy": "モメンタム", "market": "US",
    }])
    # 10営業日分 → 5日はmatured / 20日は未達
    opens = [100.0] + [0.0] * 9
    closes = [100, 101, 102, 103, 104, 108, 106, 107, 108, 109]
    prices = _make_prices("2026-01-06", opens, [float(c) for c in closes])
    book = ssb.measure(
        log_path=log, output_path=None, config=_CONFIG,
        price_loader=lambda ts: {"AAA": prices},
    )
    horizons = {r["horizon_days"] for r in book["returns"]}
    assert horizons == {5}
    assert book["pending_episode_count"] == 0  # 少なくとも1 horizon 確定なので pending 扱いにしない


def test_measure_missing_price_surfaced(tmp_path):
    log = tmp_path / "log.jsonl"
    _seed_log(log, [{
        "episode_id": "e1", "as_of_date": "2026-01-05", "ticker": "NOPRICE",
        "strategy": "モメンタム", "market": "US",
    }])
    book = ssb.measure(log_path=log, output_path=None, config=_CONFIG, price_loader=lambda ts: {})
    assert book["measured_return_count"] == 0
    assert "NOPRICE" in book["missing_price_tickers"]


# ────────────────────────────── aggregate ──────────────────────────────

def test_aggregate_by_strategy_hit_rate_and_mean():
    book = {"returns": [
        {"strategy": "モメンタム", "horizon_days": 20, "net_return": 0.05},
        {"strategy": "モメンタム", "horizon_days": 20, "net_return": 0.03},
        {"strategy": "モメンタム", "horizon_days": 20, "net_return": -0.02},
        {"strategy": "逆張り", "horizon_days": 20, "net_return": -0.10},
    ]}
    agg = ssb.aggregate_by_strategy(book)
    assert agg["モメンタム"]["20"]["n"] == 3
    assert agg["モメンタム"]["20"]["hit_rate"] == round(2 / 3, 4)
    assert agg["モメンタム"]["20"]["mean_net_return"] == round((0.05 + 0.03 - 0.02) / 3, 6)
    assert agg["逆張り"]["20"]["hit_rate"] == 0.0


def test_measure_writes_book_to_disk(tmp_path):
    log = tmp_path / "log.jsonl"
    _seed_log(log, [{
        "episode_id": "e1", "as_of_date": "2026-01-05", "ticker": "AAA",
        "strategy": "モメンタム", "market": "US",
    }])
    opens = [100.0] + [0.0] * 24
    closes = [float(100 + i) for i in range(25)]
    prices = _make_prices("2026-01-06", opens, closes)
    out = tmp_path / "book.json"
    ssb.measure(log_path=log, output_path=out, config=_CONFIG, price_loader=lambda ts: {"AAA": prices})
    assert out.exists()
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "observe_only_screener_shadow"
    assert written["measured_return_count"] >= 1

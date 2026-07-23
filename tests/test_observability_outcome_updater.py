"""Tests for append-only catalyst/sell outcome measurement."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from almanac.observability.logs import (
    write_catalyst_hypothesis_generated,
    write_sell_decision,
)
from almanac.observability.outcome_updater import (
    _TICKER_ALIASES,
    _event_entry_date,
    is_unmeasurable_primary_ticker,
    update_catalyst_outcomes,
    update_sell_outcomes,
)


class FakePriceProvider:
    def __init__(self, prices: dict[tuple[str, str], float]):
        self.prices = {(ticker.upper(), day): price for (ticker, day), price in prices.items()}

    def price_on_or_after(self, ticker: str, after_date: date) -> float | None:
        return self.prices.get((ticker.upper(), after_date.isoformat()))


class RecordingPriceProvider(FakePriceProvider):
    def __init__(self, prices: dict[tuple[str, str], float]):
        super().__init__(prices)
        self.calls: list[tuple[str, str]] = []

    def price_on_or_after(self, ticker: str, after_date: date) -> float | None:
        self.calls.append((ticker.upper(), after_date.isoformat()))
        return super().price_on_or_after(ticker, after_date)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_update_catalyst_outcomes_appends_due_rows(tmp_path: Path) -> None:
    hlog = tmp_path / "catalyst_hypothesis_log.jsonl"
    olog = tmp_path / "catalyst_outcome_log.jsonl"
    write_catalyst_hypothesis_generated(
        hlog,
        hypothesis_id="h1",
        analysis_id="a1",
        analysis_date="2026-05-01",
        event_at="2026-05-01T09:00:00",
        hypothesis_type="bull_pullback",
        primary_ticker="NVDA",
        catalyst_score=0.7,
        scenario_readiness=0.6,
        priced_in_penalty=0.1,
        surprise_score=0.5,
        gross_expected_return_bps=200,
        conviction_at_generation=70,
        price_at_event=100.0,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_price_at_event={"QQQ": 400.0},
        usdjpy_at_event=156.0,
        fsync=False,
    )
    provider = FakePriceProvider({
        ("NVDA", "2026-05-06"): 110.0,
        ("QQQ", "2026-05-06"): 408.0,
    })

    written = update_catalyst_outcomes(
        hypothesis_log_path=hlog,
        outcome_log_path=olog,
        today=date(2026, 5, 7),
        horizons=[3],
        price_provider=provider,
        fsync=False,
    )

    assert written == 1
    row = _rows(olog)[0]
    assert row["hypothesis_id"] == "h1"
    assert row["horizon_days"] == 3
    assert row["return_pct"] == 0.1
    assert row["benchmark_return_pct"] == 0.02
    assert row["excess_return_bps"] == 800


def test_update_catalyst_outcomes_does_not_duplicate_existing_measurements(
    tmp_path: Path,
) -> None:
    hlog = tmp_path / "catalyst_hypothesis_log.jsonl"
    olog = tmp_path / "catalyst_outcome_log.jsonl"
    write_catalyst_hypothesis_generated(
        hlog,
        hypothesis_id="h1",
        analysis_id="a1",
        analysis_date="2026-05-01",
        event_at="2026-05-01T09:00:00",
        hypothesis_type="bull_pullback",
        primary_ticker="NVDA",
        catalyst_score=0.7,
        scenario_readiness=0.6,
        priced_in_penalty=0.1,
        surprise_score=0.5,
        gross_expected_return_bps=200,
        conviction_at_generation=70,
        price_at_event=100.0,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_price_at_event={"QQQ": 400.0},
        usdjpy_at_event=156.0,
        fsync=False,
    )
    provider = FakePriceProvider({
        ("NVDA", "2026-05-06"): 110.0,
        ("QQQ", "2026-05-06"): 408.0,
    })
    kwargs = dict(
        hypothesis_log_path=hlog,
        outcome_log_path=olog,
        today=date(2026, 5, 7),
        horizons=[3],
        price_provider=provider,
        fsync=False,
    )

    assert update_catalyst_outcomes(**kwargs) == 1
    assert update_catalyst_outcomes(**kwargs) == 0
    assert len(_rows(olog)) == 1


def test_update_catalyst_outcomes_applies_fx_to_mixed_benchmark(
    tmp_path: Path,
) -> None:
    hlog = tmp_path / "catalyst_hypothesis_log.jsonl"
    olog = tmp_path / "catalyst_outcome_log.jsonl"
    write_catalyst_hypothesis_generated(
        hlog,
        hypothesis_id="h1",
        analysis_id="a1",
        analysis_date="2026-05-01",
        event_at="2026-05-01T09:00:00",
        hypothesis_type="ipo_proxy",
        primary_ticker="9984.T",
        catalyst_score=0.7,
        scenario_readiness=0.6,
        priced_in_penalty=0.1,
        surprise_score=0.5,
        gross_expected_return_bps=200,
        conviction_at_generation=70,
        price_at_event=8000.0,
        benchmark_basket=["TOPIX", "QQQ"],
        benchmark_weights=[0.5, 0.5],
        benchmark_currency_normalized_to="JPY",
        benchmark_price_at_event={"TOPIX": 2800.0, "QQQ": 400.0},
        usdjpy_at_event=150.0,
        fsync=False,
    )
    provider = FakePriceProvider({
        ("9984.T", "2026-05-06"): 8400.0,
        ("TOPIX", "2026-05-06"): 2912.0,  # +4%
        ("QQQ", "2026-05-06"): 408.0,     # +2% USD, +8.8% JPY with FX
        ("JPY=X", "2026-05-06"): 160.0,
    })

    assert update_catalyst_outcomes(
        hypothesis_log_path=hlog,
        outcome_log_path=olog,
        today=date(2026, 5, 7),
        horizons=[3],
        price_provider=provider,
        fsync=False,
    ) == 1
    row = _rows(olog)[0]
    assert row["benchmark_currency_normalized_to"] == "JPY"
    assert round(row["benchmark_return_pct"], 3) == 0.064


def test_update_sell_outcomes_appends_counterfactual_rows(tmp_path: Path) -> None:
    dlog = tmp_path / "sell_decision_log.jsonl"
    olog = tmp_path / "sell_outcome_log.jsonl"
    write_sell_decision(
        dlog,
        sell_decision_id="s1",
        ticker="NVDA",
        action_type="trim",
        shares_recommended=10,
        price_at_recommend=100.0,
        reason="trim",
        conviction_at_sell=60,
        recommended_at="2026-05-01T09:00:00",
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        fsync=False,
    )
    provider = FakePriceProvider({
        ("NVDA", "2026-05-06"): 108.0,
        ("QQQ", "2026-05-01"): 400.0,
        ("QQQ", "2026-05-06"): 408.0,
    })

    written = update_sell_outcomes(
        sell_decision_log_path=dlog,
        sell_outcome_log_path=olog,
        today=date(2026, 5, 7),
        horizons=[3],
        price_provider=provider,
        fsync=False,
    )

    assert written == 1
    row = _rows(olog)[0]
    assert row["sell_decision_id"] == "s1"
    assert row["missed_gain_pct"] == 0.08
    assert row["benchmark_return_pct"] == 0.02
    assert row["missed_excess_return_bps"] == 600


# ── Bug fix regressions ─────────────────────────────────────────────────────


def test_ticker_aliases_cover_topx() -> None:
    """^TOPX / TOPIX は yfinance で取得不可なので 1306.T に alias されること。"""
    assert _TICKER_ALIASES.get("^TOPX") == "1306.T"
    assert _TICKER_ALIASES.get("^TOPIX") == "1306.T"
    assert _TICKER_ALIASES.get("TOPIX") == "1306.T"


@pytest.mark.parametrize(
    ("event_at", "ticker", "expected"),
    [
        ("2026-05-01T19:59:00+00:00", "NVDA", date(2026, 5, 1)),
        ("2026-05-01T20:00:00+00:00", "NVDA", date(2026, 5, 2)),
        ("2026-06-01T15:29:00+09:00", "7203.T", date(2026, 6, 1)),
        ("2026-06-01T15:30:00+09:00", "7203.T", date(2026, 6, 2)),
    ],
)
def test_event_entry_date_respects_local_market_close(
    event_at: str,
    ticker: str,
    expected: date,
) -> None:
    """引け後に計算した特徴量へ、その日の終値を先読みで割り当てないこと。"""
    assert _event_entry_date(event_at, ticker) == expected


def test_event_entry_date_keeps_legacy_naive_timestamp() -> None:
    """Timezone 不明の旧ログは従来どおり日付を維持する。"""
    assert _event_entry_date("2026-05-01T21:00:00", "NVDA") == date(2026, 5, 1)


def test_after_close_disclosure_never_fetches_same_day_close(tmp_path: Path) -> None:
    """US引け後の特徴量は翌セッション側を価格・horizonの起点にする。"""
    hlog = tmp_path / "catalyst_hypothesis_log.jsonl"
    olog = tmp_path / "catalyst_outcome_log.jsonl"
    write_catalyst_hypothesis_generated(
        hlog,
        hypothesis_id="h-after-close",
        analysis_id="a1",
        analysis_date="2026-05-01",
        event_at="2026-05-01T21:00:00+00:00",  # 17:00 EDT
        hypothesis_type="disclosure_catalyst",
        primary_ticker="NVDA",
        catalyst_score=0.7,
        scenario_readiness=0.0,
        priced_in_penalty=0.0,
        surprise_score=0.5,
        gross_expected_return_bps=200,
        conviction_at_generation=70,
        price_at_event=None,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_price_at_event={"QQQ": None},
        usdjpy_at_event=156.0,
        fsync=False,
    )
    provider = RecordingPriceProvider({
        ("NVDA", "2026-05-02"): 100.0,
        ("NVDA", "2026-05-07"): 110.0,
        ("QQQ", "2026-05-02"): 400.0,
        ("QQQ", "2026-05-07"): 408.0,
    })

    assert update_catalyst_outcomes(
        hypothesis_log_path=hlog,
        outcome_log_path=olog,
        today=date(2026, 5, 8),
        horizons=[3],
        price_provider=provider,
        fsync=False,
    ) == 1
    assert ("NVDA", "2026-05-02") in provider.calls
    assert ("NVDA", "2026-05-01") not in provider.calls
    assert _rows(olog)[0]["return_pct"] == pytest.approx(0.1)


def test_update_catalyst_outcomes_fetches_event_price_when_null(tmp_path: Path) -> None:
    """price_at_event=None の仮説でも、プロバイダから価格を取得して outcome を書けること。"""
    hlog = tmp_path / "catalyst_hypothesis_log.jsonl"
    olog = tmp_path / "catalyst_outcome_log.jsonl"
    write_catalyst_hypothesis_generated(
        hlog,
        hypothesis_id="h-null-price",
        analysis_id="a1",
        analysis_date="2026-05-01",
        event_at="2026-05-01T09:00:00",
        hypothesis_type="bull_pullback",
        primary_ticker="NVDA",
        catalyst_score=0.7,
        scenario_readiness=0.6,
        priced_in_penalty=0.1,
        surprise_score=0.5,
        gross_expected_return_bps=200,
        conviction_at_generation=70,
        price_at_event=None,          # ← 生成時に価格未記録
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_price_at_event={"QQQ": 400.0},
        usdjpy_at_event=156.0,
        fsync=False,
    )
    provider = FakePriceProvider({
        ("NVDA", "2026-05-01"): 100.0,   # event date → プロバイダが返す価格
        ("NVDA", "2026-05-06"): 110.0,   # horizon=3 の測定日
        ("QQQ",  "2026-05-06"): 408.0,
    })

    written = update_catalyst_outcomes(
        hypothesis_log_path=hlog,
        outcome_log_path=olog,
        today=date(2026, 5, 7),
        horizons=[3],
        price_provider=provider,
        fsync=False,
    )

    assert written == 1, "price_at_event=None でも outcome が書かれるべき"
    row = _rows(olog)[0]
    assert row["hypothesis_id"] == "h-null-price"
    assert row["return_pct"] == 0.1
    assert row["benchmark_return_pct"] == 0.02


def test_is_unmeasurable_primary_ticker_covers_account_suffix_pseudo_names() -> None:
    """AVGO_特定 / AVGO_一般 型の口座区分サフィックス疑似名も測定除外されること。"""
    assert is_unmeasurable_primary_ticker("AVGO_特定")
    assert is_unmeasurable_primary_ticker("AVGO_一般")
    assert is_unmeasurable_primary_ticker("META_特定")
    assert not is_unmeasurable_primary_ticker("AVGO")
    assert not is_unmeasurable_primary_ticker("1489.T")


def test_update_catalyst_outcomes_skips_unmeasurable_fund_pseudo_ticker(tmp_path: Path) -> None:
    hlog = tmp_path / "catalyst_hypothesis_log.jsonl"
    olog = tmp_path / "catalyst_outcome_log.jsonl"
    write_catalyst_hypothesis_generated(
        hlog,
        hypothesis_id="h-slim",
        analysis_id="a1",
        analysis_date="2026-05-01",
        event_at="2026-05-01T09:00:00",
        hypothesis_type="legacy",
        primary_ticker="SLIM_SP500",
        catalyst_score=0.7,
        scenario_readiness=0.6,
        priced_in_penalty=0.1,
        surprise_score=0.5,
        gross_expected_return_bps=200,
        conviction_at_generation=70,
        price_at_event=None,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_price_at_event={"QQQ": 400.0},
        usdjpy_at_event=156.0,
        fsync=False,
    )
    provider = RecordingPriceProvider({
        ("SLIM_SP500", "2026-05-01"): 10_000.0,
        ("SLIM_SP500", "2026-05-06"): 10_500.0,
        ("QQQ", "2026-05-06"): 408.0,
    })

    written = update_catalyst_outcomes(
        hypothesis_log_path=hlog,
        outcome_log_path=olog,
        today=date(2026, 5, 7),
        horizons=[3],
        price_provider=provider,
        fsync=False,
    )

    assert is_unmeasurable_primary_ticker("SLIM_SP500")
    assert written == 0
    assert provider.calls == []
    assert not olog.exists()


def test_usdjpy_placeholder_falls_back_to_provider(tmp_path: Path) -> None:
    """usdjpy_at_event=1.0（プレースホルダ）は無効と判定し JPY=X をプロバイダから取得すること。"""
    hlog = tmp_path / "catalyst_hypothesis_log.jsonl"
    olog = tmp_path / "catalyst_outcome_log.jsonl"
    write_catalyst_hypothesis_generated(
        hlog,
        hypothesis_id="h-usdjpy",
        analysis_id="a1",
        analysis_date="2026-05-01",
        event_at="2026-05-01T09:00:00",
        hypothesis_type="ipo_proxy",
        primary_ticker="9984.T",
        catalyst_score=0.6,
        scenario_readiness=0.5,
        priced_in_penalty=0.0,
        surprise_score=0.5,
        gross_expected_return_bps=300,
        conviction_at_generation=65,
        price_at_event=8000.0,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="JPY",
        benchmark_price_at_event={"QQQ": 400.0},
        usdjpy_at_event=1.0,           # ← プレースホルダ（誤値）
        fsync=False,
    )
    provider = FakePriceProvider({
        ("9984.T", "2026-05-06"): 8400.0,
        ("QQQ",    "2026-05-06"): 408.0,
        ("JPY=X",  "2026-05-01"): 155.0,   # プロバイダが返す正しいレート（event日）
        ("JPY=X",  "2026-05-06"): 158.0,   # 測定日レート
    })

    written = update_catalyst_outcomes(
        hypothesis_log_path=hlog,
        outcome_log_path=olog,
        today=date(2026, 5, 7),
        horizons=[3],
        price_provider=provider,
        fsync=False,
    )

    assert written == 1, "usdjpy=1.0 プレースホルダでも JPY=X fallback で outcome が書かれるべき"
    row = _rows(olog)[0]
    # usdjpy=1.0 が使われていたら benchmark は USD/USD 計算になり FX 変換なし → 2%
    # usdjpy=155→158 が使われた場合 QQQ の JPY 換算リターン = (408*158)/(400*155) - 1 ≈ 6.4%
    assert row["benchmark_return_pct"] != pytest.approx(0.02, abs=1e-4), (
        "usdjpy=1.0 プレースホルダが使われてしまっている（FX fallback が機能していない）"
    )


def test_update_sell_outcomes_skips_zero_price_legacy_row(tmp_path: Path) -> None:
    """price_at_recommend=0.0 の遺産行が catch-up バッチ全体を中断させない。

    実データに validation 導入前の 0.0 価格行が 1 件あり、write_sell_outcome の
    fail-loud (non-zero 必須) が update_sell_outcomes ごと落としていた。不正行は
    自分だけ skip し、後続の正常行は計測される。
    """
    import json as _json

    dlog = tmp_path / "sell_decision_log.jsonl"
    olog = tmp_path / "sell_outcome_log.jsonl"
    # Legacy row written before price validation existed — raw append on purpose.
    dlog.write_text(
        _json.dumps({
            "sell_decision_id": "bad1",
            "ticker": "QCOM",
            "recommended_at": "2026-05-01T09:00:00",
            "price_at_recommend": 0.0,
        }) + "\n",
        encoding="utf-8",
    )
    write_sell_decision(
        dlog,
        sell_decision_id="good1",
        ticker="NVDA",
        action_type="trim",
        shares_recommended=10,
        price_at_recommend=100.0,
        reason="trim",
        conviction_at_sell=60,
        recommended_at="2026-05-01T09:00:00",
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        fsync=False,
    )
    provider = FakePriceProvider({
        ("NVDA", "2026-05-06"): 108.0,
        ("QQQ", "2026-05-01"): 400.0,
        ("QQQ", "2026-05-06"): 408.0,
    })

    written = update_sell_outcomes(
        sell_decision_log_path=dlog,
        sell_outcome_log_path=olog,
        today=date(2026, 5, 7),
        horizons=[3],
        price_provider=provider,
        fsync=False,
    )

    assert written == 1  # good1 measured, bad1 skipped, no exception
    rows = _rows(olog)
    assert [row["sell_decision_id"] for row in rows] == ["good1"]

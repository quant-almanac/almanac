"""red_team_ledger: RedTeam verdict記録+結果測定の回帰テスト"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import red_team_ledger as rtl  # noqa: E402


class _FakePriceProvider:
    """ticker -> {date_str: price} の固定テーブルで価格を返すfake。"""

    def __init__(self, prices: dict[str, dict[str, float]]):
        self._prices = prices

    def price_on_or_after(self, ticker: str, after_date: date):
        table = self._prices.get(ticker, {})
        for d_str in sorted(table.keys()):
            if date.fromisoformat(d_str) >= after_date:
                return table[d_str]
        return None


def test_record_verdict_rejects_invalid_verdict(tmp_path):
    log_path = tmp_path / "verdicts.jsonl"
    try:
        rtl.record_verdict(
            ticker="AAPL", action="buy", verdict="maybe",
            verdict_reason="test", model="deepseek", log_path=log_path,
        )
        assert False, "invalid verdictはValueErrorになるべき"
    except ValueError:
        pass


def test_record_verdict_is_deterministic_same_day(tmp_path):
    log_path = tmp_path / "verdicts.jsonl"
    id1 = rtl.record_verdict(
        ticker="AAPL", action="buy 10株", verdict="reject",
        verdict_reason="過熱感", model="deepseek",
        analysis_date="2026-06-01", log_path=log_path,
    )
    id2 = rtl.record_verdict(
        ticker="AAPL", action="buy 10株", verdict="reject",
        verdict_reason="過熱感(再掲)", model="deepseek",
        analysis_date="2026-06-01", log_path=log_path,
    )
    assert id1 == id2  # 同一ticker/action/日付/modelは同じID


def test_measure_outcomes_skips_before_horizon(tmp_path):
    v_path = tmp_path / "verdicts.jsonl"
    o_path = tmp_path / "outcomes.jsonl"
    rtl.record_verdict(
        ticker="AAPL", action="buy", verdict="reject", verdict_reason="x",
        model="deepseek", analysis_date="2026-07-01", log_path=v_path,
    )
    result = rtl.measure_outcomes(
        horizon_days=20, verdict_log_path=v_path, outcome_log_path=o_path,
        as_of=date(2026, 7, 10),  # まだ20日経過していない
        price_provider=_FakePriceProvider({}),
    )
    assert result["newly_measured"] == 0
    assert not o_path.exists() or o_path.read_text() == ""


def test_measure_outcomes_computes_return_after_horizon(tmp_path):
    v_path = tmp_path / "verdicts.jsonl"
    o_path = tmp_path / "outcomes.jsonl"
    rtl.record_verdict(
        ticker="AAPL", action="buy", verdict="reject", verdict_reason="過熱感",
        model="deepseek", analysis_date="2026-06-01", log_path=v_path,
    )
    fake = _FakePriceProvider({"AAPL": {"2026-06-01": 100.0, "2026-06-21": 90.0}})
    result = rtl.measure_outcomes(
        horizon_days=20, verdict_log_path=v_path, outcome_log_path=o_path,
        as_of=date(2026, 6, 25), price_provider=fake,
    )
    assert result["newly_measured"] == 1
    rows = rtl._read_jsonl(o_path)
    assert len(rows) == 1
    assert abs(rows[0]["return_pct"] - (-0.10)) < 1e-9


def test_measure_outcomes_is_idempotent(tmp_path):
    v_path = tmp_path / "verdicts.jsonl"
    o_path = tmp_path / "outcomes.jsonl"
    rtl.record_verdict(
        ticker="AAPL", action="buy", verdict="reject", verdict_reason="x",
        model="deepseek", analysis_date="2026-06-01", log_path=v_path,
    )
    fake = _FakePriceProvider({"AAPL": {"2026-06-01": 100.0, "2026-06-21": 90.0}})
    rtl.measure_outcomes(horizon_days=20, verdict_log_path=v_path, outcome_log_path=o_path,
                          as_of=date(2026, 6, 25), price_provider=fake)
    rtl.measure_outcomes(horizon_days=20, verdict_log_path=v_path, outcome_log_path=o_path,
                          as_of=date(2026, 6, 25), price_provider=fake)
    rows = rtl._read_jsonl(o_path)
    assert len(rows) == 1  # 2回実行しても重複しない


def test_aggregate_save_rate_reject_that_fell_counts_as_save(tmp_path):
    v_path = tmp_path / "verdicts.jsonl"
    o_path = tmp_path / "outcomes.jsonl"
    rtl.record_verdict(ticker="AAPL", action="buy", verdict="reject",
                        verdict_reason="x", model="deepseek",
                        analysis_date="2026-06-01", log_path=v_path)
    fake = _FakePriceProvider({"AAPL": {"2026-06-01": 100.0, "2026-06-21": 90.0}})
    rtl.measure_outcomes(horizon_days=20, verdict_log_path=v_path, outcome_log_path=o_path,
                         as_of=date(2026, 6, 25), price_provider=fake)

    stats = rtl.aggregate_save_rate(horizon_days=20, verdict_log_path=v_path, outcome_log_path=o_path)
    assert stats["n_reject_measured"] == 1
    assert stats["saves"] == 1
    assert stats["false_rejects"] == 0
    assert stats["save_rate"] == 1.0


def test_aggregate_save_rate_reject_that_rose_counts_as_false_reject(tmp_path):
    v_path = tmp_path / "verdicts.jsonl"
    o_path = tmp_path / "outcomes.jsonl"
    rtl.record_verdict(ticker="AAPL", action="buy", verdict="reject",
                        verdict_reason="x", model="deepseek",
                        analysis_date="2026-06-01", log_path=v_path)
    fake = _FakePriceProvider({"AAPL": {"2026-06-01": 100.0, "2026-06-21": 115.0}})
    rtl.measure_outcomes(horizon_days=20, verdict_log_path=v_path, outcome_log_path=o_path,
                         as_of=date(2026, 6, 25), price_provider=fake)

    stats = rtl.aggregate_save_rate(horizon_days=20, verdict_log_path=v_path, outcome_log_path=o_path)
    assert stats["saves"] == 0
    assert stats["false_rejects"] == 1
    assert stats["save_rate"] == 0.0

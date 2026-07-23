import types

import analyzer


def _stock(ticker: str = "AAPL") -> dict:
    return {
        "ticker": ticker,
        "price": 100.0,
        "change_pct": 1.2,
        "rsi": 32.0,
        "volume_ratio": 1.5,
        "mom_1m": 8.0,
        "mom_3m": 12.0,
        "strategy": "モメンタム",
        "atr_pct": 2.0,
        "stop_loss_atr": 96.0,
        "reason": "強い上昇",
    }


def _macro() -> tuple:
    return (8, 16.0, 150.0, 4.2, True, True, "強気")


def test_analyzer_haiku_fallback_logs_usage(monkeypatch):
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text="短文分析")],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(input_tokens=88, output_tokens=12),
            )

    monkeypatch.setattr(analyzer, "_get_deepseek", lambda: None)
    monkeypatch.setattr(analyzer, "client", types.SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setattr(analyzer, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = analyzer._call_fast_llm("system", "user", max_tokens=120)

    assert result == "短文分析"
    assert rows, "analyzer Haiku fallback should be included in llm spend accounting"
    row = rows[-1]
    assert row["role"] == "analyzer_haiku_fallback"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 88
    assert row["output_tokens"] == 12


def test_analyzer_final_judgment_logs_usage(monkeypatch):
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        type="tool_use",
                        input={
                            "signal": "買い",
                            "score": 4,
                            "entry_price": 100,
                            "target_price": 120,
                            "stop_loss": 94,
                            "reason": "三視点一致",
                            "holding_period": "1-2週間",
                        },
                    )
                ],
                stop_reason="tool_use",
                usage=types.SimpleNamespace(input_tokens=222, output_tokens=33),
            )

    monkeypatch.setattr(analyzer, "client", types.SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setattr(analyzer, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = analyzer.analyze_with_agents(
        _stock("AAPL"),
        _macro(),
        batch_results={
            "bull-AAPL": "強気",
            "bear-AAPL": "慎重",
            "risk-AAPL": "リスク",
        },
    )

    assert result["signal"] == "買い"
    assert rows, "analyzer final judgment should log Anthropic usage"
    row = rows[-1]
    assert row["role"] == "analyzer_final_judgment"
    assert row["status"] == "ok"
    assert row["ticker"] == "AAPL"
    assert row["input_tokens"] == 222
    assert row["output_tokens"] == 33


def test_analyzer_batch_results_log_usage(monkeypatch):
    rows: list[dict] = []

    class FakeBatches:
        def create(self, *, requests):
            return types.SimpleNamespace(id="batch_analyzer_1", processing_status="in_progress")

        def retrieve(self, batch_id):
            assert batch_id == "batch_analyzer_1"
            return types.SimpleNamespace(processing_status="ended")

        def results(self, batch_id):
            assert batch_id == "batch_analyzer_1"
            yield types.SimpleNamespace(
                custom_id="bull-AAPL",
                result=types.SimpleNamespace(
                    type="succeeded",
                    message=types.SimpleNamespace(
                        content=[types.SimpleNamespace(text="強気")],
                        stop_reason="end_turn",
                        usage=types.SimpleNamespace(input_tokens=55, output_tokens=9),
                    ),
                ),
            )

    monkeypatch.setattr(
        analyzer,
        "client",
        types.SimpleNamespace(messages=types.SimpleNamespace(batches=FakeBatches())),
    )
    monkeypatch.setattr(analyzer, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = analyzer._run_batch(
        [
            {
                "custom_id": "bull-AAPL",
                "params": {
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": "context"}],
                },
            }
        ],
        label="test",
    )

    assert result == {"bull-AAPL": "強気"}
    result_rows = [row for row in rows if row["role"] == "analyzer_batch_result"]
    assert result_rows, "analyzer Batch result usage should be logged for spend accounting"
    row = result_rows[-1]
    assert row["batch"] is True
    assert row["batch_id"] == "batch_analyzer_1"
    assert row["custom_id"] == "bull-AAPL"
    assert row["input_tokens"] == 55
    assert row["output_tokens"] == 9

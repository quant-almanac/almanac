from concurrent.futures import Future

from analyst import data_gatherer


def test_collect_parallel_results_uses_global_timeout_for_pending_futures():
    ready = Future()
    ready.set_result({"vix": 12.3})
    pending = Future()

    result = data_gatherer._collect_parallel_results(
        {"indicators": ready, "news": pending},
        fallbacks={"indicators": {}, "news": {"market": [], "holdings": {}}},
        timeout_seconds=0.01,
        labels={"indicators": "市場指標", "news": "ニュース"},
    )

    assert result == {
        "indicators": {"vix": 12.3},
        "news": {"market": [], "holdings": {}},
    }
    assert pending.cancelled()


def test_collect_parallel_results_falls_back_on_task_exception():
    failed = Future()
    failed.set_exception(RuntimeError("provider exploded"))

    result = data_gatherer._collect_parallel_results(
        {"jp": failed},
        fallbacks={"jp": {}},
        timeout_seconds=0.01,
        labels={"jp": "日本株ファンダメンタルズ"},
    )

    assert result == {"jp": {}}

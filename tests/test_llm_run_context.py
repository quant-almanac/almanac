from concurrent.futures import ThreadPoolExecutor

from llm_cost_accounting import normalize_usage_row
from llm_run_context import (
    analysis_run_context,
    current_analysis_id,
    submit_with_current_context,
)


def test_usage_row_inherits_analysis_id_without_overwriting_explicit_id():
    with analysis_run_context("analysis-123"):
        inherited = normalize_usage_row({"model": "claude-haiku-4-5"})
        explicit = normalize_usage_row({
            "model": "claude-haiku-4-5",
            "analysis_id": "standalone-job",
        })
    assert inherited["analysis_id"] == "analysis-123"
    assert explicit["analysis_id"] == "standalone-job"
    assert current_analysis_id() is None


def test_analysis_context_propagates_to_worker_threads():
    with ThreadPoolExecutor(max_workers=1) as executor:
        with analysis_run_context("analysis-thread"):
            future = submit_with_current_context(
                executor,
                lambda: normalize_usage_row({"model": "claude-sonnet-5"}),
            )
            row = future.result(timeout=2)
    assert row["analysis_id"] == "analysis-thread"

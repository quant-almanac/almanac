"""Tests for almanac.observability.proxy_mapper.

Coverage pins down all 4 layers of the proxy-mapping architecture
(plan §5 step 10 / Round 6 C6-5):

- L1 seed lookup — hit, miss, normalisation variants.
- L2+L3 single-run path (self_consistency_n=1).
- L4 self-consistency path (self_consistency_n >= 2).
- Audit log written exactly once per invocation.
- Edge cases: empty entity, empty samples, single sample, low Jaccard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.proxy_mapper import (  # noqa: E402
    LLMProvider,
    ProxyResult,
    jaccard_intersection,
    load_seed_map,
    lookup_seed,
    propose_proxies,
)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_seed_map() -> dict[str, list[str]]:
    return {
        "openai": ["9984.T", "MSFT", "NVDA", "ARM"],
        "spacex": ["GOOGL"],
        "stripe": ["V", "MA", "PYPL"],
    }


class _FixedProvider:
    """Fake LLMProvider that always returns the same lists."""

    def __init__(
        self,
        proposed: list[str],
        filtered: list[str],
    ) -> None:
        self._proposed = proposed
        self._filtered = filtered
        self.propose_calls: list[tuple[str, dict]] = []
        self.critique_calls: list[tuple[list[str], dict]] = []

    def propose(self, entity: str, context: dict) -> list[str]:
        self.propose_calls.append((entity, context))
        return list(self._proposed)

    def critique(self, proposal: list[str], context: dict) -> list[str]:
        self.critique_calls.append((proposal, context))
        return list(self._filtered)


class _CyclingProvider:
    """Fake LLMProvider that cycles through different filtered results per call."""

    def __init__(self, cycles: list[list[str]]) -> None:
        self._cycles = cycles
        self._call_idx = 0

    def propose(self, entity: str, context: dict) -> list[str]:
        # Always propose the union of all possible tickers.
        all_tickers: set[str] = set()
        for c in self._cycles:
            all_tickers.update(c)
        return sorted(all_tickers)

    def critique(self, proposal: list[str], context: dict) -> list[str]:
        result = self._cycles[self._call_idx % len(self._cycles)]
        self._call_idx += 1
        return list(result)


class _RaisingProvider:
    """Fake LLMProvider whose propose() / critique() raise on demand."""

    def __init__(self, raise_propose: bool = False, raise_critique: bool = False) -> None:
        self._raise_propose = raise_propose
        self._raise_critique = raise_critique

    def propose(self, entity: str, context: dict) -> list[str]:
        if self._raise_propose:
            raise RuntimeError("propose boom")
        return ["NVDA"]

    def critique(self, proposal: list[str], context: dict) -> list[str]:
        if self._raise_critique:
            raise RuntimeError("critique boom")
        return list(proposal)


# ---------------------------------------------------------------------------
# load_seed_map
# ---------------------------------------------------------------------------


def test_load_seed_map_valid(tmp_path: Path) -> None:
    p = tmp_path / "seed.json"
    data = {"openai": ["MSFT", "NVDA"]}
    p.write_text(json.dumps(data), encoding="utf-8")
    result = load_seed_map(p)
    assert result == data


def test_load_seed_map_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_seed_map(p)


def test_load_seed_map_non_dict(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text(json.dumps(["openai", "MSFT"]))
    with pytest.raises(ValueError, match="expected top-level JSON object"):
        load_seed_map(p)


def test_load_seed_map_value_not_list(tmp_path: Path) -> None:
    p = tmp_path / "bad_val.json"
    p.write_text(json.dumps({"openai": "MSFT"}))
    with pytest.raises(ValueError, match="must be a list"):
        load_seed_map(p)


# ---------------------------------------------------------------------------
# lookup_seed — normalisation
# ---------------------------------------------------------------------------


def test_lookup_seed_exact_lowercase_hit() -> None:
    sm = _make_seed_map()
    assert lookup_seed("openai", sm) == ["9984.T", "MSFT", "NVDA", "ARM"]


def test_lookup_seed_uppercase_normalised() -> None:
    sm = _make_seed_map()
    assert lookup_seed("OpenAI", sm) == ["9984.T", "MSFT", "NVDA", "ARM"]


def test_lookup_seed_padded_whitespace_normalised() -> None:
    sm = _make_seed_map()
    assert lookup_seed("  OPENAI  ", sm) == ["9984.T", "MSFT", "NVDA", "ARM"]


def test_lookup_seed_miss_returns_empty_list() -> None:
    sm = _make_seed_map()
    assert lookup_seed("unknown_entity_xyz", sm) == []


def test_lookup_seed_empty_map_returns_empty() -> None:
    assert lookup_seed("openai", {}) == []


# ---------------------------------------------------------------------------
# jaccard_intersection — edge cases
# ---------------------------------------------------------------------------


def test_jaccard_intersection_empty_samples_returns_empty() -> None:
    assert jaccard_intersection([]) == []


def test_jaccard_intersection_single_sample_returns_it() -> None:
    result = jaccard_intersection([["NVDA", "AVGO"]])
    assert result == ["NVDA", "AVGO"]


def test_jaccard_intersection_identical_samples_returns_all() -> None:
    result = jaccard_intersection([["NVDA", "AVGO"], ["NVDA", "AVGO"], ["NVDA", "AVGO"]])
    assert set(result) == {"NVDA", "AVGO"}


def test_jaccard_intersection_disjoint_samples_returns_empty() -> None:
    result = jaccard_intersection([["NVDA"], ["AVGO"], ["MSFT"]])
    assert result == []


def test_jaccard_intersection_partial_overlap_returns_common() -> None:
    # NVDA in all three; AVGO in two; MSFT in one.
    result = jaccard_intersection([
        ["NVDA", "AVGO", "MSFT"],
        ["NVDA", "AVGO"],
        ["NVDA"],
    ])
    assert result == ["NVDA"]


def test_jaccard_intersection_preserves_first_sample_order() -> None:
    result = jaccard_intersection([
        ["Z", "A", "M"],
        ["A", "M", "Z"],
        ["M", "Z", "A"],
    ])
    # All three share all; order from first sample.
    assert result == ["Z", "A", "M"]


# ---------------------------------------------------------------------------
# propose_proxies — seed-only path (llm=None)
# ---------------------------------------------------------------------------


def test_seed_only_entity_in_map() -> None:
    sm = _make_seed_map()
    result = propose_proxies("openai", seed_map=sm)
    assert result.entity == "openai"
    assert result.seed_proxies == ["9984.T", "MSFT", "NVDA", "ARM"]
    assert result.llm_proposed == []
    assert result.llm_filtered == []
    assert result.final_proxies == ["9984.T", "MSFT", "NVDA", "ARM"]
    assert result.jaccard_self_consistency is None
    assert result.used_layers == ["seed"]


def test_seed_only_entity_not_in_map_no_exception() -> None:
    sm = _make_seed_map()
    result = propose_proxies("totally_unknown_entity", seed_map=sm)
    assert result.final_proxies == []
    assert result.used_layers == ["seed"]
    assert result.jaccard_self_consistency is None


def test_seed_only_entity_normalisation_uppercase() -> None:
    sm = _make_seed_map()
    result = propose_proxies("OpenAI", seed_map=sm)
    assert result.entity == "openai"
    assert result.final_proxies == ["9984.T", "MSFT", "NVDA", "ARM"]


def test_seed_only_entity_normalisation_padded() -> None:
    sm = _make_seed_map()
    result = propose_proxies("  OPENAI  ", seed_map=sm)
    assert result.entity == "openai"
    assert result.seed_proxies == ["9984.T", "MSFT", "NVDA", "ARM"]


def test_seed_only_returns_proxy_result_frozen() -> None:
    sm = _make_seed_map()
    result = propose_proxies("spacex", seed_map=sm)
    assert isinstance(result, ProxyResult)
    # dataclass is frozen — should not allow attribute assignment.
    with pytest.raises(Exception):
        result.entity = "foo"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# propose_proxies — L2+L3 single-run path (self_consistency_n=1)
# ---------------------------------------------------------------------------


def test_llm_single_run_used_layers() -> None:
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA", "AVGO"], filtered=["NVDA"])
    result = propose_proxies(
        "openai",
        seed_map=sm,
        llm=provider,
        self_consistency_n=1,
    )
    assert result.used_layers == ["seed", "llm_propose", "llm_skeptic"]
    assert result.jaccard_self_consistency is None


def test_llm_single_run_propose_and_critique_called_once() -> None:
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA", "AVGO"], filtered=["NVDA"])
    propose_proxies("openai", seed_map=sm, llm=provider, self_consistency_n=1)
    assert len(provider.propose_calls) == 1
    assert len(provider.critique_calls) == 1


def test_llm_context_always_includes_entity_for_skeptic() -> None:
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA", "AVGO"], filtered=["NVDA"])
    propose_proxies(
        "OpenAI",
        seed_map=sm,
        llm=provider,
        self_consistency_n=1,
        context={"source": "unit"},
    )
    assert provider.propose_calls[0][1]["entity"] == "openai"
    assert provider.critique_calls[0][1]["entity"] == "openai"
    assert provider.critique_calls[0][1]["source"] == "unit"


def test_llm_single_run_proposer_output_captured() -> None:
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA", "AVGO"], filtered=["NVDA"])
    result = propose_proxies("openai", seed_map=sm, llm=provider, self_consistency_n=1)
    assert result.llm_proposed == ["NVDA", "AVGO"]
    assert result.llm_filtered == ["NVDA"]


def test_llm_single_run_skeptic_filters_proposals() -> None:
    """Proposer returns ["NVDA", "AVGO"]; skeptic returns ["NVDA"] only."""
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA", "AVGO"], filtered=["NVDA"])
    result = propose_proxies("openai", seed_map=sm, llm=provider, self_consistency_n=1)
    # NVDA survives; AVGO rejected by skeptic.
    assert "NVDA" in result.final_proxies
    assert "AVGO" not in result.final_proxies


def test_llm_entity_not_in_seed_uses_llm_proposals() -> None:
    """Entity missing from seed → L2/L3 can still propose proxies."""
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["TSLA", "UBER"], filtered=["TSLA"])
    result = propose_proxies(
        "totally_unknown_entity",
        seed_map=sm,
        llm=provider,
        self_consistency_n=1,
    )
    assert result.seed_proxies == []
    assert result.final_proxies == ["TSLA"]


def test_llm_seed_merged_with_llm_no_duplicates() -> None:
    """Seed returns ["GOOGL"]; LLM also proposes "GOOGL" plus "UBER"."""
    sm = {**_make_seed_map(), "spacex": ["GOOGL"]}
    provider = _FixedProvider(proposed=["GOOGL", "UBER"], filtered=["GOOGL", "UBER"])
    result = propose_proxies("spacex", seed_map=sm, llm=provider, self_consistency_n=1)
    # GOOGL appears once despite being in both seed and LLM output.
    assert result.final_proxies.count("GOOGL") == 1
    assert "UBER" in result.final_proxies


def test_llm_seed_comes_first_in_final_proxies() -> None:
    """Seed proxies must appear before LLM-only proxies in final_proxies."""
    sm = {"acme": ["SEED_A", "SEED_B"]}
    provider = _FixedProvider(proposed=["LLM_C", "LLM_D"], filtered=["LLM_C", "LLM_D"])
    result = propose_proxies("acme", seed_map=sm, llm=provider, self_consistency_n=1)
    assert result.final_proxies[0] == "SEED_A"
    assert result.final_proxies[1] == "SEED_B"
    assert "LLM_C" in result.final_proxies
    assert "LLM_D" in result.final_proxies


# ---------------------------------------------------------------------------
# propose_proxies — L4 self-consistency (self_consistency_n >= 2)
# ---------------------------------------------------------------------------


def test_self_consistency_used_layers_includes_self_consistency() -> None:
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA"], filtered=["NVDA"])
    result = propose_proxies("openai", seed_map=sm, llm=provider, self_consistency_n=3)
    assert "self_consistency" in result.used_layers


def test_self_consistency_consistent_result_jaccard_one() -> None:
    """Same filtered result 3 times → Jaccard = 1.0."""
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA", "AVGO"], filtered=["NVDA"])
    result = propose_proxies(
        "openai",
        seed_map=sm,
        llm=provider,
        self_consistency_n=3,
        consistency_threshold=0.5,
    )
    assert result.jaccard_self_consistency == pytest.approx(1.0)
    assert "NVDA" in result.final_proxies


def test_self_consistency_propose_called_n_times() -> None:
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA"], filtered=["NVDA"])
    propose_proxies("openai", seed_map=sm, llm=provider, self_consistency_n=4)
    assert len(provider.propose_calls) == 4


def test_self_consistency_critique_called_n_times() -> None:
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA"], filtered=["NVDA"])
    propose_proxies("openai", seed_map=sm, llm=provider, self_consistency_n=4)
    assert len(provider.critique_calls) == 4


def test_self_consistency_inconsistent_below_threshold_final_empty_llm() -> None:
    """3 completely different filtered sets → mean Jaccard = 0 < threshold → LLM discarded."""
    sm = {"mystery": []}
    provider = _CyclingProvider([["NVDA"], ["AVGO"], ["MSFT"]])
    result = propose_proxies(
        "mystery",
        seed_map=sm,
        llm=provider,
        self_consistency_n=3,
        consistency_threshold=0.5,
    )
    # All three sets are pairwise disjoint → mean Jaccard = 0.
    assert result.jaccard_self_consistency == pytest.approx(0.0)
    # LLM proxies discarded; seed is also empty → final empty.
    assert result.final_proxies == []


def test_self_consistency_partial_overlap_intersection_returned() -> None:
    """Pairwise Jaccard >= threshold → intersection of all samples kept."""
    sm = {"entity": []}
    # All three contain "NVDA"; runs 0+1 also contain "AVGO".
    provider = _CyclingProvider([["NVDA", "AVGO"], ["NVDA", "AVGO"], ["NVDA"]])
    result = propose_proxies(
        "entity",
        seed_map=sm,
        llm=provider,
        self_consistency_n=3,
        consistency_threshold=0.3,
    )
    # Intersection across all 3 is {"NVDA"} only.
    assert "NVDA" in result.final_proxies
    assert "AVGO" not in result.final_proxies


def test_self_consistency_n_equals_2_runs_l4() -> None:
    """n=2 is sufficient to trigger L4."""
    sm = _make_seed_map()
    provider = _FixedProvider(proposed=["NVDA"], filtered=["NVDA"])
    result = propose_proxies("openai", seed_map=sm, llm=provider, self_consistency_n=2)
    assert result.jaccard_self_consistency is not None
    assert "self_consistency" in result.used_layers


# ---------------------------------------------------------------------------
# propose_proxies — audit log
# ---------------------------------------------------------------------------


def test_audit_log_written_once_per_invocation(tmp_path: Path) -> None:
    sm = _make_seed_map()
    log_path = tmp_path / "proxy_audit_log.jsonl"
    propose_proxies("openai", seed_map=sm, audit_log_path=log_path)
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1


def test_audit_log_written_per_each_call(tmp_path: Path) -> None:
    sm = _make_seed_map()
    log_path = tmp_path / "proxy_audit_log.jsonl"
    propose_proxies("openai", seed_map=sm, audit_log_path=log_path)
    propose_proxies("spacex", seed_map=sm, audit_log_path=log_path)
    propose_proxies("stripe", seed_map=sm, audit_log_path=log_path)
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3


def test_audit_log_row_has_required_fields(tmp_path: Path) -> None:
    sm = _make_seed_map()
    log_path = tmp_path / "proxy_audit_log.jsonl"
    result = propose_proxies(
        "openai",
        seed_map=sm,
        audit_log_path=log_path,
        context={"source": "test"},
    )
    row = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert "row_id" in row
    assert row["entity"] == "openai"
    assert row["context"] == {"source": "test"}
    assert row["seed_proxies"] == result.seed_proxies
    assert row["final_proxies"] == result.final_proxies
    assert "recorded_at" in row


def test_audit_log_not_written_when_path_is_none(tmp_path: Path) -> None:
    sm = _make_seed_map()
    # Should not raise; no file created.
    result = propose_proxies("openai", seed_map=sm, audit_log_path=None)
    assert isinstance(result, ProxyResult)
    assert not any(tmp_path.iterdir())  # nothing written


def test_audit_log_written_with_llm_provider(tmp_path: Path) -> None:
    sm = _make_seed_map()
    log_path = tmp_path / "proxy_audit_log.jsonl"
    provider = _FixedProvider(proposed=["NVDA", "AVGO"], filtered=["NVDA"])
    propose_proxies(
        "openai",
        seed_map=sm,
        llm=provider,
        self_consistency_n=1,
        audit_log_path=log_path,
    )
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["llm_proposed"] == ["NVDA", "AVGO"]
    assert row["llm_filtered"] == ["NVDA"]


# ---------------------------------------------------------------------------
# propose_proxies — defensive / error cases
# ---------------------------------------------------------------------------


def test_empty_entity_raises_value_error() -> None:
    sm = _make_seed_map()
    with pytest.raises(ValueError, match="non-empty string"):
        propose_proxies("", seed_map=sm)


def test_whitespace_only_entity_raises_value_error() -> None:
    sm = _make_seed_map()
    with pytest.raises(ValueError, match="non-empty string"):
        propose_proxies("   ", seed_map=sm)


def test_proposer_exception_swallowed_returns_seed_only(tmp_path: Path) -> None:
    sm = {"entity": ["SEED_X"]}
    provider = _RaisingProvider(raise_propose=True)
    result = propose_proxies("entity", seed_map=sm, llm=provider, self_consistency_n=1)
    # Proposer raised → filtered is empty → only seed survives.
    assert result.seed_proxies == ["SEED_X"]
    assert result.final_proxies == ["SEED_X"]


def test_skeptic_exception_swallowed_filtered_empty(tmp_path: Path) -> None:
    sm = {"entity": []}
    provider = _RaisingProvider(raise_propose=False, raise_critique=True)
    result = propose_proxies("entity", seed_map=sm, llm=provider, self_consistency_n=1)
    # Skeptic raised → filtered = [] → final = seed (empty).
    assert result.llm_filtered == []
    assert result.final_proxies == []


# ---------------------------------------------------------------------------
# LLMProvider Protocol structural check
# ---------------------------------------------------------------------------


def test_llm_provider_protocol_is_runtime_checkable() -> None:
    """LLMProvider must be a runtime-checkable Protocol."""
    provider = _FixedProvider(proposed=[], filtered=[])
    assert isinstance(provider, LLMProvider)


def test_non_provider_not_instance_of_protocol() -> None:
    class NotAProvider:
        pass

    assert not isinstance(NotAProvider(), LLMProvider)


# ---------------------------------------------------------------------------
# ProxyResult — public API guarantees
# ---------------------------------------------------------------------------


def test_proxy_result_fields_are_all_present() -> None:
    sm = _make_seed_map()
    result = propose_proxies("openai", seed_map=sm)
    assert hasattr(result, "entity")
    assert hasattr(result, "seed_proxies")
    assert hasattr(result, "llm_proposed")
    assert hasattr(result, "llm_filtered")
    assert hasattr(result, "final_proxies")
    assert hasattr(result, "jaccard_self_consistency")
    assert hasattr(result, "used_layers")


def test_proxy_result_used_layers_is_list_of_str() -> None:
    sm = _make_seed_map()
    result = propose_proxies("openai", seed_map=sm)
    assert isinstance(result.used_layers, list)
    assert all(isinstance(s, str) for s in result.used_layers)


def test_proxy_result_final_proxies_is_list_of_str() -> None:
    sm = _make_seed_map()
    result = propose_proxies("openai", seed_map=sm)
    assert isinstance(result.final_proxies, list)
    assert all(isinstance(t, str) for t in result.final_proxies)


# ---------------------------------------------------------------------------
# proxy_seed_map.json fixture — file exists and has ≥ 9 required entries
# ---------------------------------------------------------------------------


def test_proxy_seed_map_json_exists() -> None:
    json_path = _REPO_ROOT / "proxy_seed_map.json"
    assert json_path.exists(), f"proxy_seed_map.json not found at {json_path}"


def test_proxy_seed_map_json_valid_and_has_required_keys() -> None:
    json_path = _REPO_ROOT / "proxy_seed_map.json"
    data = load_seed_map(json_path)
    # NOTE: spacex は 2026-06-12 に NASDAQ 上場(SPCX)したため proxy エンティティ
    # から除外し、SPCX を tickers.json ユニバースへ直接オンボードした。上場済の
    # エンティティは proxy_seed_map に残さない(残すと SpaceX news を GOOGL へ誤誘導する)。
    required = {
        "openai", "stripe", "anthropic", "deepseek",
        "perplexity", "ai_data_center", "lithium_battery", "rare_earth",
    }
    missing = required - set(data.keys())
    assert not missing, f"proxy_seed_map.json missing required keys: {missing}"


def test_proxy_seed_map_json_openai_includes_softbank() -> None:
    json_path = _REPO_ROOT / "proxy_seed_map.json"
    data = load_seed_map(json_path)
    assert "9984.T" in data["openai"], "OpenAI seed must include 9984.T (SoftBank stake)"


# ---------------------------------------------------------------------------
# __all__ completeness
# ---------------------------------------------------------------------------


def test_module_all_exports_required_symbols() -> None:
    import almanac.observability.proxy_mapper as mod

    required = {
        "LLMProvider",
        "ProxyResult",
        "load_seed_map",
        "lookup_seed",
        "jaccard_intersection",
        "propose_proxies",
    }
    missing = required - set(mod.__all__)
    assert not missing, f"Missing from __all__: {missing}"

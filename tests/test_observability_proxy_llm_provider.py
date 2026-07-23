"""Tests for almanac.observability.proxy_llm_provider.

The production :class:`LLMProvider` wraps a real Sonnet call but is entirely
exercised here via an injected fake — no test makes a real LLM API call.
Coverage focuses on:

- ``propose()`` parses JSON / regex / dict responses
- ``propose()`` fail-open semantics on llm_call errors and garbage output
- ``critique()`` returns a subset of the proposal (never introduces new tickers)
- ``critique()`` fail-open semantics on llm_call errors
- Ticker sanitization: regex shape, universe filter, dedup, cap
- Constructor parameter routing (temperatures passed through, etc.)
- Prompt construction includes the entity / proposal
- Integration with :func:`almanac.observability.proxy_mapper.propose_proxies`
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.proxy_llm_provider import (  # noqa: E402
    TICKER_REGEX,
    SonnetProxyProvider,
    default_llm_call,
)


# ---------------------------------------------------------------------------
# Fake llm_call factories
# ---------------------------------------------------------------------------


def _fake_constant(response: str):
    """Return a fake that always emits the same response and captures calls."""
    captured: list[dict] = []

    def fake(prompt: str, system: str, temperature: float) -> str:
        captured.append({"prompt": prompt, "system": system, "temperature": temperature})
        return response

    fake.captured = captured  # type: ignore[attr-defined]
    return fake


def _fake_raises(exc: type[Exception] = RuntimeError):
    def fake(prompt, system, temperature):
        raise exc("boom")
    return fake


# ---------------------------------------------------------------------------
# TICKER_REGEX
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker,valid",
    [
        ("AAPL", True),
        ("9984.T", True),
        ("1489.T", True),
        ("V", True),               # 1-char US
        ("BRKB", True),
        ("nvda", False),           # lowercase rejected (must be normalized first)
        ("GIBBERISHTOOLONG", False),
        ("A.B.C", False),
        ("NVDA.US", False),        # .T is the only allowed suffix
        ("", False),
        (".T", False),
    ],
)
def test_ticker_regex_matches_expected(ticker: str, valid: bool) -> None:
    assert bool(TICKER_REGEX.match(ticker)) is valid


# ---------------------------------------------------------------------------
# propose() — parsing
# ---------------------------------------------------------------------------


def test_propose_parses_strict_json_response() -> None:
    fake = _fake_constant('{"tickers": ["NVDA", "MSFT"]}')
    p = SonnetProxyProvider(llm_call=fake)
    assert p.propose("OpenAI", {}) == ["NVDA", "MSFT"]


def test_propose_parses_dict_response_directly() -> None:
    """Some llm_call implementations return a dict, not a JSON string."""
    def fake(prompt, system, temperature):
        return {"tickers": ["AAPL", "GOOGL"]}  # type: ignore[return-value]
    p = SonnetProxyProvider(llm_call=fake)
    assert p.propose("OpenAI", {}) == ["AAPL", "GOOGL"]


def test_propose_falls_back_to_regex_sweep_on_prose() -> None:
    """Sonnet sometimes wraps the answer in commentary."""
    fake = _fake_constant("Here are some candidates: NVDA and MSFT could work.")
    p = SonnetProxyProvider(llm_call=fake)
    assert p.propose("OpenAI", {}) == ["NVDA", "MSFT"]


def test_propose_returns_empty_on_empty_entity() -> None:
    fake = _fake_constant('{"tickers": ["NVDA"]}')
    p = SonnetProxyProvider(llm_call=fake)
    assert p.propose("", {}) == []
    assert p.propose("   ", {}) == []
    # And the fake llm_call should not have been called.
    assert fake.captured == []  # type: ignore[attr-defined]


def test_propose_returns_empty_on_llm_exception() -> None:
    """Fail-open: a single bad call must not break the analyzer."""
    p = SonnetProxyProvider(llm_call=_fake_raises(RuntimeError))
    assert p.propose("OpenAI", {}) == []


def test_propose_returns_empty_on_garbage_response() -> None:
    fake = _fake_constant("@@@ not json and no uppercase words @@@")
    p = SonnetProxyProvider(llm_call=fake)
    assert p.propose("OpenAI", {}) == []


def test_propose_returns_empty_on_none_response() -> None:
    fake = _fake_constant("")
    p = SonnetProxyProvider(llm_call=fake)
    assert p.propose("OpenAI", {}) == []


# ---------------------------------------------------------------------------
# propose() — sanitization
# ---------------------------------------------------------------------------


def test_propose_filters_invalid_ticker_shapes() -> None:
    fake = _fake_constant('{"tickers": ["nvda", "GIBBERISHTOOLONG", "AAPL", "1234567"]}')
    p = SonnetProxyProvider(llm_call=fake)
    # Lowercase 'nvda' gets upcased → 'NVDA' (valid).
    # 'GIBBERISHTOOLONG' (16 chars) rejected.
    # 'AAPL' valid.
    # '1234567' (7 chars) rejected.
    assert p.propose("X", {}) == ["NVDA", "AAPL"]


def test_propose_caps_at_max_proxies() -> None:
    fake = _fake_constant('{"tickers": ["A", "B", "C", "D", "E", "F", "G"]}')
    p = SonnetProxyProvider(llm_call=fake, max_proxies=3)
    assert p.propose("X", {}) == ["A", "B", "C"]


def test_propose_dedupes_case_insensitive() -> None:
    fake = _fake_constant('{"tickers": ["nvda", "NVDA", "Nvda", "MSFT"]}')
    p = SonnetProxyProvider(llm_call=fake)
    assert p.propose("X", {}) == ["NVDA", "MSFT"]


def test_propose_intersects_with_ticker_universe() -> None:
    fake = _fake_constant('{"tickers": ["NVDA", "MSFT", "FOO"]}')
    p = SonnetProxyProvider(llm_call=fake, ticker_universe={"NVDA", "AAPL"})
    assert p.propose("X", {}) == ["NVDA"]


def test_propose_empty_universe_drops_everything() -> None:
    """A defensive empty-set universe is interpreted literally."""
    fake = _fake_constant('{"tickers": ["NVDA"]}')
    p = SonnetProxyProvider(llm_call=fake, ticker_universe=set())
    assert p.propose("X", {}) == []


def test_propose_universe_none_means_no_filter() -> None:
    fake = _fake_constant('{"tickers": ["NVDA", "MSFT"]}')
    p = SonnetProxyProvider(llm_call=fake, ticker_universe=None)
    assert p.propose("X", {}) == ["NVDA", "MSFT"]


def test_propose_handles_jp_tickers() -> None:
    fake = _fake_constant('{"tickers": ["9984.T", "1489.T", "AAPL"]}')
    p = SonnetProxyProvider(llm_call=fake)
    assert p.propose("X", {}) == ["9984.T", "1489.T", "AAPL"]


# ---------------------------------------------------------------------------
# propose() — prompt construction
# ---------------------------------------------------------------------------


def test_propose_passes_entity_into_prompt() -> None:
    fake = _fake_constant('{"tickers": []}')
    p = SonnetProxyProvider(llm_call=fake)
    p.propose("OpenAI Inc", {"sector": "AI"})
    call = fake.captured[0]  # type: ignore[attr-defined]
    assert "OpenAI Inc" in call["prompt"]
    assert "AI" in call["prompt"]  # sector hint surfaced


def test_propose_includes_seed_proxies_in_context_when_provided() -> None:
    fake = _fake_constant('{"tickers": []}')
    p = SonnetProxyProvider(llm_call=fake)
    p.propose("OpenAI", {"seed_proxies": ["MSFT", "NVDA"]})
    call = fake.captured[0]  # type: ignore[attr-defined]
    assert "MSFT" in call["prompt"]


def test_propose_uses_configured_temperature() -> None:
    fake = _fake_constant('{"tickers": []}')
    p = SonnetProxyProvider(llm_call=fake, propose_temperature=0.9)
    p.propose("X", {})
    assert fake.captured[0]["temperature"] == 0.9  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# critique() — semantics
# ---------------------------------------------------------------------------


def test_critique_returns_subset_of_proposal() -> None:
    fake = _fake_constant('{"tickers": ["NVDA", "MSFT"]}')
    p = SonnetProxyProvider(llm_call=fake)
    out = p.critique(["NVDA", "AAPL", "MSFT"], context={"entity": "X"})
    assert set(out) == {"NVDA", "MSFT"}


def test_critique_drops_new_tickers_not_in_proposal() -> None:
    """The critique must never introduce symbols — it can only remove."""
    fake = _fake_constant('{"tickers": ["NVDA", "TSLA"]}')  # TSLA wasn't proposed
    p = SonnetProxyProvider(llm_call=fake)
    out = p.critique(["NVDA", "MSFT"], context={})
    assert out == ["NVDA"]
    assert "TSLA" not in out


def test_critique_returns_original_on_llm_exception() -> None:
    p = SonnetProxyProvider(llm_call=_fake_raises(RuntimeError))
    out = p.critique(["NVDA", "MSFT"], context={})
    assert out == ["NVDA", "MSFT"]


def test_critique_returns_original_on_garbage_response() -> None:
    """Garbage parse → fail-open, keep the proposal intact."""
    fake = _fake_constant("nothing parseable here")
    p = SonnetProxyProvider(llm_call=fake)
    out = p.critique(["NVDA", "MSFT"], context={})
    assert out == ["NVDA", "MSFT"]


def test_critique_returns_original_when_filtered_subset_is_empty() -> None:
    """If the critic dropped every input symbol, treat as parse failure."""
    fake = _fake_constant('{"tickers": ["TSLA", "GOOGL"]}')  # none of the input
    p = SonnetProxyProvider(llm_call=fake)
    out = p.critique(["NVDA", "MSFT"], context={})
    assert out == ["NVDA", "MSFT"]


def test_critique_empty_proposal_returns_empty() -> None:
    fake = _fake_constant('{"tickers": ["NVDA"]}')
    p = SonnetProxyProvider(llm_call=fake)
    assert p.critique([], context={}) == []
    # And the llm should not have been called.
    assert fake.captured == []  # type: ignore[attr-defined]


def test_critique_case_insensitive_subset_check() -> None:
    """Mixed-case proposal entries should still match the upcased critic output."""
    fake = _fake_constant('{"tickers": ["nvda"]}')  # lowercase from critic
    p = SonnetProxyProvider(llm_call=fake)
    out = p.critique(["NVDA", "AAPL"], context={})
    assert out == ["NVDA"]


def test_critique_passes_entity_into_prompt() -> None:
    fake = _fake_constant('{"tickers": ["NVDA"]}')
    p = SonnetProxyProvider(llm_call=fake)
    p.critique(["NVDA"], context={"entity": "OpenAI"})
    call = fake.captured[0]  # type: ignore[attr-defined]
    assert "OpenAI" in call["prompt"]
    assert "NVDA" in call["prompt"]


def test_critique_uses_configured_temperature() -> None:
    fake = _fake_constant('{"tickers": ["NVDA"]}')
    p = SonnetProxyProvider(llm_call=fake, critique_temperature=0.0)
    p.critique(["NVDA"], context={})
    assert fake.captured[0]["temperature"] == 0.0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Integration with proxy_mapper.propose_proxies
# ---------------------------------------------------------------------------


def test_integrates_with_proxy_mapper_propose_proxies() -> None:
    """End-to-end: SonnetProxyProvider plugged into the 4-layer orchestrator."""
    from almanac.observability.proxy_mapper import propose_proxies

    fake = _fake_constant('{"tickers": ["MSFT", "NVDA"]}')
    provider = SonnetProxyProvider(llm_call=fake)
    seed_map = {"openai": ["9984.T"]}  # seed has one entry; LLM adds more

    result = propose_proxies(
        "OpenAI",
        seed_map=seed_map,
        llm=provider,
        self_consistency_n=1,  # skip L4 for a deterministic test
    )
    # Seed contributed 9984.T; LLM proposed MSFT/NVDA. Critique passes them
    # (fake returns the same JSON for both calls), so all 3 should be present.
    assert "9984.T" in result.final_proxies
    assert "MSFT" in result.final_proxies
    assert "NVDA" in result.final_proxies
    assert "seed" in result.used_layers
    assert "llm_propose" in result.used_layers


def test_integration_survives_llm_failure() -> None:
    """If the injected LLM dies, the orchestrator should still return the seeds."""
    from almanac.observability.proxy_mapper import propose_proxies

    provider = SonnetProxyProvider(llm_call=_fake_raises(RuntimeError))
    seed_map = {"openai": ["9984.T", "MSFT"]}

    result = propose_proxies(
        "OpenAI",
        seed_map=seed_map,
        llm=provider,
        self_consistency_n=1,
    )
    # Seeds survived even though propose() returned [] from the fail-open path.
    assert set(result.final_proxies) >= {"9984.T", "MSFT"}


# ---------------------------------------------------------------------------
# default_llm_call
# ---------------------------------------------------------------------------
#
# ``default_llm_call`` is a tiny lazy-import wrapper. Its happy path needs the
# real analyst.llm_client (real LLM SDK), and its failure paths involve
# patching an already-imported module — both are out of scope for unit tests
# that promise NEVER to hit a real LLM. Production callers pass their own
# ``llm_call`` to the constructor, which IS thoroughly tested above.

def test_default_llm_call_is_callable_and_takes_three_kwargs() -> None:
    """At minimum, the signature must match what SonnetProxyProvider expects."""
    import inspect
    sig = inspect.signature(default_llm_call)
    assert set(sig.parameters.keys()) == {"prompt", "system", "temperature"}


def test_default_llm_call_uses_existing_call_claude(monkeypatch) -> None:
    """Production analyst.llm_client exposes call_claude, not call_sonnet."""
    from analyst import llm_client

    captured = {}

    def fake_call_claude(**kwargs):
        captured.update(kwargs)
        return '{"tickers": ["NVDA"]}'

    monkeypatch.setattr(llm_client, "call_claude", fake_call_claude)
    result = default_llm_call(prompt="Prompt text", system="System text", temperature=0.2)

    assert result == '{"tickers": ["NVDA"]}'
    assert captured["user"] == "Prompt text"
    assert captured["system"] == "System text"
    assert captured["temperature"] == 0.2
    assert captured["use_tool"] is False


# ---------------------------------------------------------------------------
# Constructor edge cases
# ---------------------------------------------------------------------------


def test_max_proxies_clamped_to_at_least_one() -> None:
    fake = _fake_constant('{"tickers": ["NVDA", "MSFT"]}')
    p = SonnetProxyProvider(llm_call=fake, max_proxies=0)  # absurd input
    result = p.propose("X", {})
    assert len(result) == 1  # clamped


def test_provider_does_not_call_real_llm_by_default_until_invoked() -> None:
    """Constructor must not eagerly import analyst.llm_client."""
    # Just constructing with no llm_call must not raise.
    p = SonnetProxyProvider()
    assert p is not None

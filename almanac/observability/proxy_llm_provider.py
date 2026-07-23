"""Production :class:`LLMProvider` for :mod:`almanac.observability.proxy_mapper`.

Phase 2-D shipped a Protocol-based 4-layer proxy mapper that takes an
``LLMProvider | None`` — tests inject fakes; production needs a real
Sonnet-backed implementation. This module is that implementation.

Design constraints
------------------

- **Injectable LLM call.** The constructor accepts ``llm_call``: a callable
  ``(prompt: str, system: str, temperature: float) -> str``. Production
  wires this to ``analyst.llm_client``; tests wire a fake.
- **Lazy import of the real client.** ``analyst.llm_client`` lives outside
  this package and pulls in heavy dependencies (the anthropic SDK). We
  import it only inside the default-factory path so importing
  ``proxy_llm_provider`` itself stays cheap and side-effect free.
- **Fail-open vs fail-closed semantics.** ``propose()`` returns ``[]`` on
  any parse / API failure (silent — caller decides what to do); we'd
  rather emit zero proxies than fabricate noise. ``critique()`` returns
  the **original** proposal on failure — better to keep noise than to
  silently drop signal we couldn't verify.
- **Ticker hygiene.** Outputs are uppercased, whitespace-trimmed, and
  shape-validated (``^[A-Z0-9]+(\\.T)?$``, length 1-6 excluding suffix).
  An optional ``ticker_universe`` set further intersects results so a
  hallucinated symbol like "GIBBERISH" cannot reach the catalyst layer.
- **R6 C6-5 audit**: this module does NOT write proxy_audit_log itself;
  the orchestrator in ``proxy_mapper.propose_proxies`` already does that.
  Keeping audit centralized prevents double-logging when callers compose.

See plan §5 step 10 and Round 6 C6-5 for the larger context.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from .proxy_mapper import LLMProvider as _LLMProviderProtocol

__all__ = [
    "SonnetProxyProvider",
    "default_llm_call",
    "TICKER_REGEX",
]

logger = logging.getLogger(__name__)

#: Accept US (``AAPL``) and JP (``9984.T``) listings. Disallows dots that
#: don't precede ``T`` so a hallucinated ``A.B.C`` cannot slip through.
TICKER_REGEX = re.compile(r"^[A-Z0-9]{1,6}(\.T)?$")


# ---------------------------------------------------------------------------
# LLM call default — lazy import of analyst.llm_client
# ---------------------------------------------------------------------------


def default_llm_call(
    prompt: str,
    system: str,
    temperature: float,
) -> str:
    """Default ``llm_call`` factory that delegates to ``analyst.llm_client``.

    Imported lazily so ``proxy_llm_provider`` can be imported (and tested)
    without bringing the analyst LLM client transitively into scope. If
    ``analyst.llm_client`` is unavailable or the expected helper is missing,
    this raises :class:`RuntimeError` — that surfaces the misconfiguration
    rather than silently degrading to empty output.

    Production callers normally inject their own ``llm_call`` so this
    default is mostly a convenience for one-shot scripts.
    """
    try:
        from analyst import llm_client  # noqa: WPS433 — deferred import is the point
    except ImportError as exc:  # pragma: no cover — environmental
        raise RuntimeError(
            "analyst.llm_client not importable; pass an explicit "
            "llm_call to SonnetProxyProvider instead"
        ) from exc

    call_claude = getattr(llm_client, "call_claude", None)
    if callable(call_claude):
        return call_claude(
            system=system,
            user=prompt,
            temperature=temperature,
            use_tool=False,
        )

    # Probe for legacy wrappers in priority order. Different analyst versions
    # have exposed the helper under slightly different names.
    for attr in ("run_sonnet_tool", "call_sonnet", "run_sonnet"):
        helper = getattr(llm_client, attr, None)
        if callable(helper):
            return helper(prompt=prompt, system=system, temperature=temperature)
    raise RuntimeError(
        "analyst.llm_client has no call_claude / run_sonnet_tool / "
        "call_sonnet / run_sonnet; "
        "pass an explicit llm_call to SonnetProxyProvider"
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class SonnetProxyProvider:
    """Production :class:`LLMProvider` implementation backed by Sonnet.

    Examples
    --------
    Production wiring::

        from almanac.observability.proxy_llm_provider import SonnetProxyProvider
        from almanac.observability.proxy_mapper import propose_proxies

        provider = SonnetProxyProvider(ticker_universe={...known tickers...})
        result = propose_proxies(
            "OpenAI",
            seed_map=seed_map,
            llm=provider,
            audit_log_path="proxy_audit_log.jsonl",
        )

    Testing with a fake::

        captured = []
        def fake(prompt, system, temperature):
            captured.append(prompt)
            return '{"tickers": ["NVDA", "MSFT"]}'
        provider = SonnetProxyProvider(llm_call=fake)
    """

    def __init__(
        self,
        *,
        llm_call: Callable[[str, str, float], str] | None = None,
        propose_temperature: float = 0.6,
        critique_temperature: float = 0.2,
        max_proxies: int = 5,
        ticker_universe: set[str] | None = None,
    ) -> None:
        self._llm_call = llm_call or default_llm_call
        self._propose_temperature = propose_temperature
        self._critique_temperature = critique_temperature
        self._max_proxies = max(1, int(max_proxies))
        self._ticker_universe = (
            {t.strip().upper() for t in ticker_universe if t}
            if ticker_universe is not None
            else None
        )

    # -- propose ---------------------------------------------------------

    def propose(self, entity: str, context: dict[str, Any]) -> list[str]:
        """Ask Sonnet for proxy tickers given a non-listed ``entity``.

        Fail-open: returns ``[]`` on any error so a single bad call cannot
        bring down a daily analyzer run.
        """
        if not entity or not entity.strip():
            return []
        prompt = self._build_propose_prompt(entity, context)
        system = (
            "You are a financial proxy mapper. Given a non-listed entity, "
            "respond with a JSON object {\"tickers\": [...]}. List up to "
            f"{self._max_proxies} publicly-listed ticker symbols whose price "
            "is meaningfully correlated with the entity's economic outcome. "
            "Use US tickers (AAPL) or Tokyo tickers (9984.T). No commentary."
        )
        try:
            raw = self._llm_call(
                prompt=prompt,
                system=system,
                temperature=self._propose_temperature,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            logger.warning("propose(): llm_call raised: %s", exc)
            return []
        tickers = _extract_tickers(raw)
        return self._sanitize(tickers)

    def _build_propose_prompt(self, entity: str, context: dict[str, Any]) -> str:
        seed_hint = context.get("seed_proxies") if context else None
        sector_hint = context.get("sector") if context else None
        notes = context.get("notes") if context else None
        lines = [f"Entity: {entity}"]
        if seed_hint:
            lines.append(f"Known seed proxies (already considered): {seed_hint}")
        if sector_hint:
            lines.append(f"Sector hint: {sector_hint}")
        if notes:
            lines.append(f"Notes: {notes}")
        lines.append('Respond with: {"tickers": ["...", "..."]}')
        return "\n".join(lines)

    # -- critique --------------------------------------------------------

    def critique(self, proposal: list[str], context: dict[str, Any]) -> list[str]:
        """Ask Sonnet to drop implausible entries from ``proposal``.

        Fail-open: returns ``proposal`` unchanged on any error — better to
        propagate noise than to silently drop signal we couldn't verify.
        """
        if not proposal:
            return []
        prompt = self._build_critique_prompt(proposal, context)
        system = (
            "You are a skeptical financial reviewer. Given a list of proposed "
            "proxy tickers, return only the ones whose price is genuinely "
            "correlated with the entity's economic outcome — discard weak, "
            "tangential, or hallucinated symbols. Respond with a JSON object "
            "{\"tickers\": [...]}. No commentary."
        )
        try:
            raw = self._llm_call(
                prompt=prompt,
                system=system,
                temperature=self._critique_temperature,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            logger.warning("critique(): llm_call raised: %s", exc)
            return list(proposal)
        kept = _extract_tickers(raw)
        kept = self._sanitize(kept)
        # The critique result must be a *subset* of the proposal — never
        # introduce new symbols. Intersect strictly.
        proposal_set = {t.upper() for t in proposal}
        filtered = [t for t in kept if t in proposal_set]
        if not filtered:
            # Critic dropped everything → that may be correct (all noise) OR
            # may indicate a parse failure. Conservatively fail-open and
            # return the original proposal so the caller's own filters /
            # self-consistency can take it from here.
            logger.debug("critique(): subset was empty; returning original proposal")
            return list(proposal)
        return filtered

    def _build_critique_prompt(self, proposal: list[str], context: dict[str, Any]) -> str:
        entity = (context or {}).get("entity", "")
        lines = []
        if entity:
            lines.append(f"Entity: {entity}")
        lines.append(f"Proposed tickers: {proposal}")
        lines.append('Respond with the surviving subset: {"tickers": ["...", "..."]}')
        return "\n".join(lines)

    # -- sanitization ----------------------------------------------------

    def _sanitize(self, raw_tickers: list[str]) -> list[str]:
        """Normalize, validate shape, intersect with universe, dedupe, cap."""
        out: list[str] = []
        seen: set[str] = set()
        for raw in raw_tickers:
            if not isinstance(raw, str):
                continue
            t = raw.strip().upper()
            if not t or t in seen:
                continue
            if not TICKER_REGEX.match(t):
                continue
            if self._ticker_universe is not None and t not in self._ticker_universe:
                continue
            seen.add(t)
            out.append(t)
            if len(out) >= self._max_proxies:
                break
        return out


# Runtime sanity: the concrete class must satisfy the Protocol the orchestrator
# expects. ``isinstance`` against ``runtime_checkable`` Protocols doesn't help
# here (the Protocol is not necessarily runtime-checkable), but a structural
# attribute check at import time would catch a future signature drift.
assert hasattr(SonnetProxyProvider, "propose")
assert hasattr(SonnetProxyProvider, "critique")
del _LLMProviderProtocol  # only imported for documentation; not used at runtime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_tickers(raw: Any) -> list[str]:
    """Best-effort extraction of a ticker list from an LLM response.

    Accepts (in order of preference):

    - A dict already containing ``tickers``.
    - A JSON string with a ``tickers`` array.
    - A loose text response — falls back to a regex sweep for token
      candidates (uppercase, optional ``.T``).

    Returns ``[]`` on total failure rather than raising.
    """
    if isinstance(raw, dict):
        candidate = raw.get("tickers")
        if isinstance(candidate, list):
            return [str(t) for t in candidate if isinstance(t, (str, int))]
        return []
    if not isinstance(raw, str) or not raw.strip():
        return []
    # First try strict JSON parse.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("tickers"), list):
        return [str(t) for t in parsed["tickers"] if isinstance(t, (str, int))]
    if isinstance(parsed, list):
        return [str(t) for t in parsed if isinstance(t, (str, int))]
    # Fall back to regex sweep — Sonnet sometimes wraps the JSON in prose.
    matches = re.findall(r"\b[A-Z0-9]{1,6}(?:\.T)?\b", raw)
    return matches

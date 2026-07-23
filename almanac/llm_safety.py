"""External-LLM safety layer — the privacy choke-point for non-Anthropic models.

Phase 0 / cross-cutting track of the ALMANAC public-disclosure feature pipeline
plan.

ALMANAC sends *public* market context to third-party models (DeepSeek, Groq,
Gemini) for disclosure / news feature extraction and Red Team idea generation.
This module is the gate for non-Anthropic calls that are contractually public
or anonymized — disclosure-feature extraction, the analyst Bull/Bear/Risk
debate, the Red Team, and the Judge cross-validation. Explicitly book-aware
tier analysis is configured separately through ``call_tier_analysis`` and is
outside this public-payload contract. Public-only screener candidate calls
(``screener.py`` / ``long_term_screener.py``) send only public screening data
and are intentionally out of scope.

Design (plan "Cross-cutting — external-LLM safety layer"):

1. **Allowlist by payload *type*, not regex.** Only a :class:`Payload` whose
   ``kind`` is in :data:`ALLOWED_KINDS` may leave the process; anything else
   raises :class:`PrivacyViolation`. ``public_social`` is intentionally
   *excluded* until Phase 0.5 — social chatter is a different (noisier) trust
   tier and is added later with its own validation.
2. **Secondary regex PII scan** as defense-in-depth. Internal field names
   (``value_jpy`` / ``unrealized_pct`` / ``pos_summary`` …) are screened on
   *every* kind; the Japanese holdings vocabulary (評価額 / 含み損 / 保有銘柄 …)
   is screened ONLY on anonymized (book-derived) kinds, because those words occur
   legitimately in public EDINET/TDnet filings and would otherwise fail-close a
   valid disclosure. The *allowlist* is the primary contract; the regex is a
   backstop.
3. **Usage capture** — ``model_id`` / ``input_tokens`` / ``output_tokens`` /
   ``source_url`` are appended to ``logs/llm_calls.jsonl`` so external spend and
   provenance become observable (the legacy log carried call metadata only).

NOTE the module lives at ``almanac/llm_safety.py`` and **not** ``utils/``: a
top-level ``utils.py`` already exists and ``analyzer.py`` imports it via
``__import__('utils')``; a ``utils/`` package would shadow and break it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from almanac.observability.append_only_log import append_jsonl_safe

__all__ = [
    "ALLOWED_KINDS",
    "DEFERRED_KINDS",
    "BOOK_AWARE_KIND",
    "PrivacyViolation",
    "Payload",
    "ExternalLLMResult",
    "scan_text_for_pii",
    "assert_public_payload",
    "call_external_llm",
    "call_book_aware_llm",
]

# Repo root: almanac/llm_safety.py → parents[1] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LOG_PATH = _REPO_ROOT / "logs" / "llm_calls.jsonl"


# ---------------------------------------------------------------------------
# Payload-type allowlist (the primary guard)
# ---------------------------------------------------------------------------

ALLOWED_KINDS: frozenset[str] = frozenset(
    {
        "public_disclosure",
        "public_news",
        "public_market_context",
        "anonymized_market_gap",
        "anonymized_recommendations",
    }
)
"""Payload kinds permitted to reach a non-Anthropic endpoint.

Public-data kinds (only internal field-name leakage is screened):

- ``public_disclosure`` — EDINET / TDnet / EDGAR filing text.
- ``public_news`` — news headline / body.
- ``public_market_context`` — technical / macro / news context for a single
  candidate (the analyzer Bull/Bear/Risk debate).

Anonymized kinds (book-derived but stripped; the JP book-vocabulary scan also
applies as a backstop):

- ``anonymized_market_gap`` — Red Team context reduced to an abstract gap
  ("light on semis momentum"), computed locally — no holdings / sizes / P&L.
- ``anonymized_recommendations`` — the Judge cross-validation with tickers
  pseudonymized (T1/T2/…) and free-text dropped, so structure (contradiction /
  overconfidence / consensus) survives without revealing the book.
"""

DEFERRED_KINDS: frozenset[str] = frozenset({"public_social"})
"""Kinds explicitly *not* allowed yet. ``public_social`` arrives in Phase 0.5
with its own validation; until then it is rejected like any unknown kind."""

BOOK_AWARE_KIND = "book_aware_tier"
"""Kind for *book-aware* tier analysis. Deliberately **not** in
:data:`ALLOWED_KINDS`: it carries the portfolio book (holdings / margin / P&L)
to DeepSeek by explicit, user-approved policy, so it must travel through
:func:`call_book_aware_llm` (which skips the public-payload PII scan) and would
be *rejected* by :func:`call_external_llm`. Its reason to exist is observability,
not privacy — see :func:`call_book_aware_llm`."""


# ---------------------------------------------------------------------------
# Secondary PII scan (defense-in-depth backstop)
# ---------------------------------------------------------------------------

# Internal book/account field identifiers — snake_case names from our own data
# structures. These never legitimately appear in public filing/news text, so
# they are screened for EVERY payload kind.
_INTERNAL_FIELD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"value_jpy", re.IGNORECASE),
    re.compile(r"unrealized_pct", re.IGNORECASE),
    re.compile(r"unrealized_pnl", re.IGNORECASE),
    re.compile(r"\bpos_summary\b", re.IGNORECASE),
    re.compile(r"portfolio_total", re.IGNORECASE),
    re.compile(r"account_balance", re.IGNORECASE),
    # Portfolio risk-metric field names (snake_case; never in public filings).
    re.compile(r"\bvar_95\b", re.IGNORECASE),
    re.compile(r"\bcvar_95\b", re.IGNORECASE),
)

# Japanese natural-language holdings/balance vocabulary. These DO occur in public
# EDINET/TDnet filings (有報・決算短信), so screening them on a public_disclosure /
# public_news payload would over-block legitimate text. They are applied ONLY to
# anonymized (book-derived) kinds, where any holdings language signals a leak.
_JP_BOOK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"保有(?:株|数|銘柄|残高)"),
    re.compile(r"口座残高"),
    re.compile(r"含み(?:益|損)"),
    re.compile(r"評価額"),
    # Portfolio stress-test loss figure leaked into a Red Team prompt (R-round).
    re.compile(r"推定損失"),
)

# Kinds for which the JP book-vocabulary scan also runs (book-derived contexts).
_ANON_KINDS: frozenset[str] = frozenset(
    {"anonymized_market_gap", "anonymized_recommendations"}
)


def scan_text_for_pii(text: str, *, include_jp_book: bool = True) -> list[str]:
    """Return the list of PII-marker substrings found in ``text``.

    Empty list means clean. The match strings (not the patterns) are returned so
    callers can log *what* tripped the scan without echoing the whole payload.

    ``include_jp_book`` adds the Japanese holdings/balance vocabulary. It defaults
    on (generic callers want the strict scan), but :func:`assert_public_payload`
    turns it OFF for public_disclosure / public_news / public_market_context
    payloads, where those words are legitimate public text.
    """
    if not text:
        return []
    patterns = _INTERNAL_FIELD_PATTERNS + (_JP_BOOK_PATTERNS if include_jp_book else ())
    hits: list[str] = []
    for pat in patterns:
        m = pat.search(text)
        if m:
            hits.append(m.group(0))
    return hits


# ---------------------------------------------------------------------------
# Errors & types
# ---------------------------------------------------------------------------


class PrivacyViolation(ValueError):
    """Raised when a payload would leak non-public data to an external model."""


@dataclass(frozen=True)
class Payload:
    """A vetted unit of work for an external (non-Anthropic) model.

    ``kind`` is the contract: it must be in :data:`ALLOWED_KINDS`. ``system``
    and ``user`` are the prompt strings actually sent. ``source_url`` and
    ``evidence`` are provenance carried into the usage log; they are not sent
    to the model unless the caller also put them in ``user``.
    """

    kind: str
    system: str
    user: str
    source_url: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalLLMResult:
    """Return value of :func:`call_external_llm`."""

    content: str
    model_id: str
    input_tokens: int | None
    output_tokens: int | None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def assert_public_payload(payload: Payload) -> None:
    """Validate that ``payload`` is safe to send to a non-Anthropic model.

    Two gates, in order:

    1. ``payload.kind`` must be in :data:`ALLOWED_KINDS` (primary contract).
       A ``DEFERRED_KINDS`` value gets a more specific message so the Phase-0.5
       deferral is obvious; everything else is a generic rejection.
    2. The serialized ``system`` + ``user`` text must contain no PII markers
       (secondary backstop).

    Raises
    ------
    PrivacyViolation
        On either gate. The message never echoes the full payload.
    """
    kind = getattr(payload, "kind", None)
    if kind not in ALLOWED_KINDS:
        if kind in DEFERRED_KINDS:
            raise PrivacyViolation(
                f"payload kind {kind!r} is deferred to Phase 0.5 and not yet "
                f"permitted; allowed kinds: {sorted(ALLOWED_KINDS)}"
            )
        raise PrivacyViolation(
            f"payload kind {kind!r} is not in the allowlist "
            f"{sorted(ALLOWED_KINDS)}; refusing to send to an external model"
        )

    # JP holdings vocabulary is legitimate in public filings/news, so only screen
    # it on the anonymized (book-derived) kinds; internal field names are always
    # screened.
    combined = f"{payload.system}\n{payload.user}"
    hits = scan_text_for_pii(combined, include_jp_book=(kind in _ANON_KINDS))
    if hits:
        raise PrivacyViolation(
            f"payload tripped the PII backstop (markers: {sorted(set(hits))}); "
            f"refusing to send personal/book data to an external model"
        )


# ---------------------------------------------------------------------------
# Transport + call
# ---------------------------------------------------------------------------

# A transport takes the resolved call parameters and returns
# (content, {"input_tokens": int|None, "output_tokens": int|None}).
Transport = Callable[..., tuple[str, Mapping[str, Any]]]


def _default_transport(
    *,
    base_url: str,
    api_key: str,
    model_id: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, Mapping[str, Any]]:
    """OpenAI-compatible chat call (DeepSeek / Groq / Gemini). Network path."""
    from openai import OpenAI  # imported lazily so validation/tests need no SDK

    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    content = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    return content, {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
    }


def call_external_llm(
    payload: Payload,
    *,
    base_url: str,
    api_key: str,
    model_id: str,
    role: str = "external",
    max_tokens: int = 1200,
    temperature: float = 0.3,
    transport: Transport | None = None,
    log_path: Path | str | None = None,
    fsync: bool = True,
) -> ExternalLLMResult:
    """Validate ``payload`` then send it to a non-Anthropic model and log usage.

    This is the sanctioned path for external (DeepSeek / Groq / Gemini) calls
    whose contract is public or anonymized — disclosure extraction, the analyst
    debate, the Red Team, and the Judge. Explicit book-aware tier analysis and
    public-only screener calls are outside this contract; see the module
    docstring. This function refuses non-allowlisted or PII-marked payloads and
    records ``model_id`` / token usage / ``source_url`` for provenance.

    ``transport`` is injectable so the validation and logging contract can be
    unit-tested without the network or the ``openai`` SDK.

    Raises
    ------
    PrivacyViolation
        If ``payload`` fails :func:`assert_public_payload` — raised *before*
        any network call, so a bad payload never leaves the process.
    """
    assert_public_payload(payload)

    tx = transport or _default_transport
    content, usage = tx(
        base_url=base_url,
        api_key=api_key,
        model_id=model_id,
        system=payload.system,
        user=payload.user,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    from llm_cost_accounting import normalize_usage_row

    append_jsonl_safe(
        Path(log_path) if log_path is not None else _DEFAULT_LOG_PATH,
        normalize_usage_row({
            "ts": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "model": model_id,
            "kind": payload.kind,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "source_url": payload.source_url,
            "evidence_count": len(payload.evidence),
            "adapter": "almanac.llm_safety",
        }),
        fsync=fsync,
    )

    return ExternalLLMResult(
        content=content,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def call_book_aware_llm(
    payload: Payload,
    *,
    model_id: str,
    transport: Transport,
    role: str = "tier_analysis",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    base_url: str = "",
    api_key: str = "",
    log_path: Path | str | None = None,
    fsync: bool = True,
) -> ExternalLLMResult:
    """Observability path for *book-aware* tier analysis (DeepSeek).

    Codex P2 #13. Unlike :func:`call_external_llm`, this deliberately does NOT
    run the public-payload allowlist / PII scan: book-aware tier analysis sends
    the portfolio book to a non-Anthropic model by explicit, user-approved
    policy (see ``analyst.llm_client.call_tier_analysis``). Its purpose is to
    keep even book-bearing external calls *centrally logged* (model / usage /
    kind + ``book_aware`` markers) instead of bypassing observability, so audit
    and spend tracking are not scattered.

    ``payload.kind`` MUST be :data:`BOOK_AWARE_KIND`. ``transport`` is required —
    the caller supplies the resolved adapter call (e.g. wrapping
    ``llm_adapters.call_by_role``); ``base_url`` / ``api_key`` are forwarded but
    typically unused when the adapter resolves them itself.

    Raises
    ------
    PrivacyViolation
        If ``payload.kind`` is not :data:`BOOK_AWARE_KIND` (so a public payload
        can't accidentally take the scan-skipping path).
    """
    if getattr(payload, "kind", None) != BOOK_AWARE_KIND:
        raise PrivacyViolation(
            f"call_book_aware_llm requires payload kind {BOOK_AWARE_KIND!r}, "
            f"got {getattr(payload, 'kind', None)!r}"
        )

    _log = Path(log_path) if log_path is not None else _DEFAULT_LOG_PATH

    # Codex re-review #13: 失敗した通信も監査ログに残す (送信を試みた事実 + error を記録)。
    try:
        content, usage = transport(
            base_url=base_url,
            api_key=api_key,
            model_id=model_id,
            system=payload.system,
            user=payload.user,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as e:
        append_jsonl_safe(
            _log,
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "role": role,
                "model": model_id,
                "kind": payload.kind,
                "book_aware": True,
                "contains_book": True,
                "status": "error",
                "error": str(e)[:300],
                "adapter": "almanac.llm_safety",
            },
            fsync=fsync,
        )
        raise

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    from llm_cost_accounting import normalize_usage_row

    append_jsonl_safe(
        _log,
        normalize_usage_row({
            "ts": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "model": model_id,
            "kind": payload.kind,
            "book_aware": True,
            "contains_book": True,
            "status": "ok",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "adapter": "almanac.llm_safety",
        }),
        fsync=fsync,
    )

    return ExternalLLMResult(
        content=content,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

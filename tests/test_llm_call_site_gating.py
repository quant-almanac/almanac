"""Regression guard: every direct LLM client instantiation must either be an
explicitly-reviewed public-content path, or be gated by
almanac.llm_safety.assert_book_aware_allowed() before it can carry portfolio
context to an external model.

This is a coarse, file-level heuristic (it checks that the gate function is
*called somewhere in the file*, not that it guards the specific call site) —
cheap to run, no network or SDK dependency, and it catches the most likely
regression: a new call site added without going through the gate at all.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files that intentionally instantiate an LLM client directly because their
# payload is public market/news/screening data, not portfolio context — or
# because they *are* the gateway/transport layer itself. Adding a file here
# is a deliberate, reviewed decision; it is not the default.
PUBLIC_ALLOWLIST: frozenset[str] = frozenset({
    "almanac/llm_safety.py",       # the gateway itself
    "analyst/llm_client.py",       # call_claude (transport, gated by callers
                                    # via call_tier_analysis) + public web search
    "llm_adapters.py",             # shared transport (call_by_role) used by both
                                    # gated (call_tier_analysis) and public callers
    "analyzer.py",                 # ticker/market context, not the book
    "geopolitical_monitor.py",     # public news/scenarios
    "ipo_watch.py",                # public web search only (prompt states no book data)
    "screener.py",                 # public screening candidates
    "long_term_screener.py",       # public screening candidates
    "test.py",                     # legacy root-level smoke script, not part of the served app
    "test_analyzer.py",            # legacy root-level smoke script, not part of the served app
})

_MARKERS = ("anthropic.Anthropic(", "OpenAI(")
_SKIP_PREFIXES = ("tests/", "venv/", ".venv/", "frontend/", "__pycache__/")


def _direct_instantiation_files() -> list[str]:
    hits = []
    for pyfile in REPO_ROOT.rglob("*.py"):
        rel = pyfile.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(_SKIP_PREFIXES):
            continue
        try:
            text = pyfile.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(marker in text for marker in _MARKERS):
            hits.append((rel, text))
    return hits


def test_no_ungated_direct_llm_client_instantiation():
    violations = []
    for rel, text in _direct_instantiation_files():
        if rel in PUBLIC_ALLOWLIST:
            continue
        if "assert_book_aware_allowed" in text:
            continue
        violations.append(rel)

    assert not violations, (
        "these files directly instantiate an LLM client (anthropic.Anthropic("
        " / OpenAI() ) without calling almanac.llm_safety.assert_book_aware_"
        f"allowed() anywhere in the file, and are not in PUBLIC_ALLOWLIST: "
        f"{violations}. If the payload may carry portfolio context (holdings, "
        "balance, P&L, allocation), route it through "
        "assert_book_aware_allowed()/log_book_aware_call() first. If the "
        "payload is genuinely public/market data only, add the file to "
        "PUBLIC_ALLOWLIST in this test with a one-line reason."
    )


def test_public_allowlist_files_still_exist():
    """Catch stale allowlist entries left behind after a rename/delete."""
    missing = [rel for rel in PUBLIC_ALLOWLIST if not (REPO_ROOT / rel).exists()]
    assert not missing, f"PUBLIC_ALLOWLIST references files that no longer exist: {missing}"

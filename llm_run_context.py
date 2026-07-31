"""Run-local metadata automatically attached to LLM usage rows."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from typing import Callable, Iterator, Optional, TypeVar


_CURRENT_ANALYSIS_ID: ContextVar[Optional[str]] = ContextVar(
    "almanac_current_analysis_id",
    default=None,
)
_T = TypeVar("_T")


def current_analysis_id() -> Optional[str]:
    value = _CURRENT_ANALYSIS_ID.get()
    return str(value).strip() if value else None


@contextmanager
def analysis_run_context(analysis_id: str) -> Iterator[None]:
    value = str(analysis_id or "").strip()
    if not value:
        raise ValueError("analysis_id is required")
    token = _CURRENT_ANALYSIS_ID.set(value)
    try:
        yield
    finally:
        _CURRENT_ANALYSIS_ID.reset(token)


def submit_with_current_context(executor, function: Callable[..., _T], *args, **kwargs):
    """Submit work while preserving ContextVars in a new worker thread."""
    context = copy_context()
    return executor.submit(context.run, function, *args, **kwargs)

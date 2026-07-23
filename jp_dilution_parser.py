"""Conservative deterministic parser for Japanese dilution and audit events."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

PARSER_VERSION = "jp-dilution-1.0"

_DILUTION_TERMS = (
    "公募増資",
    "株式の売出し",
    "株式売出し",
    "第三者割当",
    "自己株式の処分",
    "自己株処分",
    "MSCB",
    "転換社債型新株予約権付社債",
)
_GOING_CONCERN_TERMS = (
    "継続企業の前提に関する注記",
    "継続企業の前提に重要な疑義",
    "意見不表明",
    "限定付適正意見",
    "監査意見の変更",
)
_DATE_PAT = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{4}/\d{1,2}/\d{1,2}"
)
_DILUTION_PCT_PATTERNS = (
    re.compile(r"(?:希薄化率|希釈化率)[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*%"),
    re.compile(r"議決権[^。\n]{0,40}?(?:希薄化|希釈化)[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*%"),
)


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").replace("％", "%")
    return _DATE_PAT.sub(" ", normalized)


def parse_dilution_event(text: str) -> tuple[bool, Optional[float]]:
    """Return ``(flag, dilution_ratio)``; ratio is ``None`` when not explicit."""
    normalized = _normalize(text)
    flag = any(term in normalized for term in _DILUTION_TERMS)
    if not flag:
        return False, None
    for pattern in _DILUTION_PCT_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        value = float(match.group(1)) / 100.0
        if 0.0 <= value <= 5.0:
            return True, value
        return True, None
    return True, None


def parse_going_concern_flag(title: str) -> bool:
    """High-precision title match for going-concern and audit-opinion events."""
    normalized = _normalize(title)
    return any(term in normalized for term in _GOING_CONCERN_TERMS)

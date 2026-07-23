"""Deterministic parser for Japanese earnings-guidance revisions.

The parser intentionally returns ``None`` when the old/new operating-profit
figures cannot be identified with high confidence. It never asks an LLM to
guess a missing value.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

PARSER_VERSION = "jp-guidance-1.0"

_NUMBER = r"[△▲\-−]?\s*\(?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*\)?"
_OLD = r"(?:前回(?:発表)?予想|従来予想|修正前)"
_NEW = r"(?:今回(?:修正)?予想|修正後|新予想)"

# 修正開示の見出しには「(2026年5月14日発表)」のような日付が紛れ込み、行の数値列パース
# (sales, 営業利益, ...) で 2026/5/14 を金額と誤読する。数値抽出の前に日付を除去する。
_DATE_PAT = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{4}/\d{1,2}/\d{1,2}"
)
# 業績予想修正が ±1000% を超えるのは実在するが極めて稀。多くはパース崩れなので破棄する
# (決定論特徴量は「正しい値 か None」が命綱で、誤値は None より遥かに悪い)。
_MAX_ABS_REVISION = 10.0


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _DATE_PAT.sub(" ", text)
    text = text.replace("百万円", " 百万円 ").replace("億円", " 億円 ")
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())


def _number(value: str) -> Optional[float]:
    raw = re.sub(r"\s+", "", value or "")
    negative = any(mark in raw for mark in ("△", "▲", "-", "−"))
    raw = raw.replace("△", "").replace("▲", "").replace("−", "-")
    raw = raw.replace("(", "").replace(")", "").replace(",", "")
    raw = raw.lstrip("-")
    try:
        number = float(raw)
    except ValueError:
        return None
    return -number if negative else number


def _explicit_pair(text: str) -> tuple[Optional[float], Optional[float]]:
    patterns = (
        rf"営業利益[^\n]{{0,120}}?{_OLD}\s*[:：]?\s*({_NUMBER})[^\n]{{0,120}}?{_NEW}\s*[:：]?\s*({_NUMBER})",
        rf"営業利益[^\n]{{0,120}}?{_NEW}\s*[:：]?\s*({_NUMBER})[^\n]{{0,120}}?{_OLD}\s*[:：]?\s*({_NUMBER})",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, re.DOTALL)
        if not match:
            continue
        first, second = _number(match.group(1)), _number(match.group(2))
        return (first, second) if index == 0 else (second, first)
    return None, None


def _row_values(text: str) -> tuple[Optional[float], Optional[float]]:
    """Parse common TDnet rows where sales is followed by operating profit."""

    old_row = re.search(rf"(?m)^.*?{_OLD}.*$", text)
    new_row = re.search(rf"(?m)^.*?{_NEW}.*$", text)
    if not old_row or not new_row:
        return None, None

    def operating_profit(row: str) -> Optional[float]:
        values = re.findall(_NUMBER, row)
        parsed = [number for number in (_number(v) for v in values) if number is not None]
        # Standard TDnet revision tables order columns as sales, operating
        # profit, ordinary profit, net income. Requiring at least two values
        # avoids treating an isolated percentage as operating profit.
        return parsed[1] if len(parsed) >= 2 else None

    return operating_profit(old_row.group(0)), operating_profit(new_row.group(0))


def parse_guidance_revision_pct(text: str) -> Optional[float]:
    """Return ``(new - old) / abs(old)`` for operating profit, else ``None``."""

    normalized = _normalize(text)
    if "営業利益" not in normalized:
        return None
    old, new = _explicit_pair(normalized)
    if old is None or new is None:
        old, new = _row_values(normalized)
    if old is None or new is None or old == 0:
        return None
    revision = (new - old) / abs(old)
    if abs(revision) > _MAX_ABS_REVISION:
        return None  # almost certainly a parse error, not a real revision
    return revision

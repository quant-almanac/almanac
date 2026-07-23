#!/usr/bin/env python3
"""Fail when the public snapshot contains known private identifiers or state."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_BASENAMES = {
    "account.json",
    "holdings.json",
    "nisa_portfolio.json",
    "espp_plan.json",
    "credit_card_plans.json",
    "action_executions.json",
    "ai_portfolio_analysis.json",
}
FORBIDDEN_TEXT = {
    "former employer name": "ク" + "ボタ",
    "former employer romanization": "Ku" + "bota",
    "former employer ticker": "63" + "26",
    "local username": "ik" + "ura",
}
SECRET_PATTERNS = {
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    "OpenAI key": re.compile(r"sk-proj-[A-Za-z0-9_-]{16,}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "Slack token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    "absolute home path": re.compile(r"/(?:Users|home)/[^/\s]+/"),
}


def tracked_files() -> list[Path]:
    raw = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
    )
    return [ROOT / item.decode("utf-8") for item in raw.split(b"\0") if item]


def main() -> int:
    failures: list[str] = []
    for path in tracked_files():
        rel = path.relative_to(ROOT)
        if path.name in PRIVATE_BASENAMES:
            failures.append(f"{rel}: private runtime-state filename is tracked")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, literal in FORBIDDEN_TEXT.items():
            if literal.lower() in text.lower():
                failures.append(f"{rel}: contains {label}")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{rel}: contains {label}")

    if failures:
        print("Public-snapshot safety check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Public-snapshot safety check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

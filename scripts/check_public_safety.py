#!/usr/bin/env python3
"""Fail closed when a public ALMANAC snapshot contains private material.

The checker intentionally scans both file *paths* and text.  ``--history``
also scans every reachable blob of a ref before a public release, so deleting a
file in the working tree cannot make a historical disclosure invisible.
"""
from __future__ import annotations

import argparse
import json
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
    "macro_event_state.json",
    "tunable_params_state.json",
    "holdings_attestation.json",
    "flow_adjusted_dd_shadow.json",
    "short_candidates.json",
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


def _git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def tracked_files() -> list[Path]:
    raw = _git("ls-files", "-z")
    return [ROOT / item.decode("utf-8") for item in raw.split(b"\0") if item]


def _scan_path(label: str, path_text: str, failures: list[str]) -> None:
    path = Path(path_text)
    if path.name in PRIVATE_BASENAMES:
        failures.append(f"{label}: private runtime-state filename is tracked")
    lowered = path_text.lower()
    for name, literal in FORBIDDEN_TEXT.items():
        if literal.lower() in lowered:
            failures.append(f"{label}: path contains {name}")


def _scan_text(label: str, text: str, failures: list[str]) -> None:
    lowered = text.lower()
    for name, literal in FORBIDDEN_TEXT.items():
        if literal.lower() in lowered:
            failures.append(f"{label}: contains {name}")
    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            failures.append(f"{label}: contains {name}")


def _history_objects(ref: str) -> list[tuple[str, str]]:
    """Return unique ``(blob_sha, path)`` relations reachable from ``ref``."""
    raw = _git("rev-list", "--objects", ref).decode("utf-8", errors="strict")
    objects: list[tuple[str, str]] = []
    for line in raw.splitlines():
        try:
            sha, path = line.split(" ", 1)
        except ValueError:
            continue  # commit/tree object without a filename relation
        if path:
            objects.append((sha, path))
    return objects


def _scan_history(ref: str, failures: list[str], skipped: list[str]) -> None:
    seen_blobs: set[str] = set()
    for sha, path in _history_objects(ref):
        label = f"history:{sha[:12]}:{path}"
        _scan_path(label, path, failures)
        if sha in seen_blobs:
            continue
        seen_blobs.add(sha)
        try:
            raw = _git("cat-file", "-p", sha)
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(label + " (non-UTF-8 blob)")
            continue
        except subprocess.CalledProcessError as exc:
            failures.append(label + f" (cannot read blob: {exc.returncode})")
            continue
        _scan_text(label, text, failures)


def _validate_short_defaults(failures: list[str]) -> None:
    short_config_path = ROOT / "disclosure_shadow_config.json"
    try:
        short_config = json.loads(short_config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        failures.append(f"{short_config_path.name}: cannot validate short defaults ({exc})")
        return
    for key in ("us_short_enabled", "jp_short_enabled"):
        if short_config.get(key) is not False:
            failures.append(f"{short_config_path.name}: {key} must be false in the public default")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        metavar="REF",
        help="scan every reachable file blob and filename from this git ref",
    )
    parser.add_argument(
        "--strict-unreadable",
        action="store_true",
        help="treat non-UTF-8 tracked/history blobs as failures rather than reporting them",
    )
    args = parser.parse_args()

    failures: list[str] = []
    skipped: list[str] = []
    _validate_short_defaults(failures)
    for path in tracked_files():
        rel = str(path.relative_to(ROOT))
        _scan_path(rel, rel, failures)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped.append(rel + " (non-UTF-8 tracked file)")
            continue
        except OSError as exc:
            failures.append(rel + f" (cannot read tracked file: {exc})")
            continue
        _scan_text(rel, text, failures)
    if args.history:
        _scan_history(args.history, failures, skipped)
    if args.strict_unreadable and skipped:
        failures.extend("unreadable: " + item for item in skipped)

    if failures:
        print("Public-snapshot safety check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Public-snapshot safety check passed.")
    if skipped:
        print("Non-text files not content-scanned (path scan completed):")
        for item in skipped:
            print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

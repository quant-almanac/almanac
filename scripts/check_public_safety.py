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
    "broad_execution_routes.json",
    "deployment_rollout_state.json",
    "deployment_rollout_audit.jsonl",
}
FORBIDDEN_TEXT = {
    "former employer name": "ク" + "ボタ",
    "former employer romanization": "Ku" + "bota",
    "former employer ticker": "63" + "26",
    "local username": "ik" + "ura",
}
#: 保有者を特定できる情報・household identity map。
#:
#: 公開版 README の契約は「保有者を特定できる情報や household identity map を
#: 含めない」だが、以前この検査には勤務先・ユーザー名・秘密鍵しか無く、
#: 実在の証券会社名・世帯区分 (husband/wife)・現金ウォレット名が公開版へ
#: 入っても検査が通っていた (レビューで発覚)。
#:
#: ⚠️ ここは **新しいコードの混入を止める** ための検査。既存の公開スナップ
#: ショット由来の出現は別途の棚卸しが要る (履歴書換えを伴うため、判断は人間)。
IDENTITY_PATTERNS = {
    "broker name (ja)": re.compile("楽" + "天証券"),
    "broker name (en)": re.compile(r"\bRakuten\s+Securities\b", re.IGNORECASE),
    "household role": re.compile(r"\b(?:husband|wife)\b", re.IGNORECASE),
    "cash wallet route": re.compile(r"\bCASH_(?:JPY|USD)_[A-Z_]+\b"),
    "mmf wallet": re.compile(r"\bGS_MMF_[A-Z]+\b"),
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


#: 既に保有者特定情報を含んでいるファイル (初回の公開スナップショット由来)。
#: アプリ本体は世帯2名ぶんの owner と特定のウォレット経路をモデル化して
#: いるので、これらの文字列は **機能上必要** で、単純に消すことはできない。
#:
#: この baseline の目的は「新しい混入を止める」ことだけ。公開ミラーが
#: そもそもこれらを含んでよいのか、履歴を書き換えるのかは、履歴書換え
#: (force-push) を伴う人間の判断なので、この検査は決めない。
IDENTITY_BASELINE_PATH = ROOT / "scripts" / "public_identity_baseline.txt"


def _identity_baseline() -> set[str]:
    try:
        return {
            line.strip()
            for line in IDENTITY_BASELINE_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
    except OSError:
        return set()


def _scan_identities(label: str, text: str, failures: list[str],
                     baseline: set[str]) -> None:
    """保有者を特定できる情報を **作業ツリーに対して** 検査する。

    baseline に載っていないファイルで見つかったら失敗させる。履歴には
    初回スナップショット由来の出現が多数あり、そこまで失敗にすると検査が
    恒久的に赤くなって役に立たなくなる。
    """
    if label in baseline:
        return
    for name, pattern in IDENTITY_PATTERNS.items():
        if pattern.search(text):
            failures.append(
                f"{label}: contains {name} (owner-identifying; not in "
                f"{IDENTITY_BASELINE_PATH.name})")


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


def _scan_history_identities(ref: str, failures: list[str]) -> None:
    """Catch private author metadata even when every committed blob is safe."""
    raw = _git(
        "log",
        "--format=%H%x00%an%x00%ae%x00%cn%x00%ce",
        ref,
    ).decode("utf-8", errors="strict")
    for row in raw.splitlines():
        fields = row.split("\0")
        if len(fields) != 5:
            continue
        sha, author_name, author_email, committer_name, committer_email = fields
        _scan_text(f"history-commit:{sha[:12]}:author", f"{author_name} <{author_email}>", failures)
        _scan_text(
            f"history-commit:{sha[:12]}:committer",
            f"{committer_name} <{committer_email}>",
            failures,
        )


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


def _validate_broad_route_example(failures: list[str]) -> None:
    path = ROOT / "examples" / "private_state" / "broad_execution_routes.example.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        failures.append(f"{path.relative_to(ROOT)}: cannot validate public example ({exc})")
        return
    if payload.get("example_only") is not True:
        failures.append(f"{path.relative_to(ROOT)}: example_only must be true")
    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        failures.append(f"{path.relative_to(ROOT)}: routes must contain a disabled placeholder")
        return
    for index, route in enumerate(routes):
        label = f"{path.relative_to(ROOT)}:routes[{index}]"
        if not isinstance(route, dict):
            failures.append(f"{label}: route must be an object")
            continue
        if route.get("active") is not False:
            failures.append(f"{label}: active must be false")
        for key in ("owner", "broker", "account", "cash_route"):
            if not str(route.get(key) or "").startswith("example_"):
                failures.append(f"{label}: {key} must be an example_ placeholder")
        if route.get("wallet_key"):
            failures.append(f"{label}: wallet_key must not be embedded in the public example")


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
    identity_baseline = _identity_baseline()
    _validate_short_defaults(failures)
    _validate_broad_route_example(failures)
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
        _scan_identities(rel, text, failures, identity_baseline)
    if args.history:
        _scan_history(args.history, failures, skipped)
        _scan_history_identities(args.history, failures)
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

#!/usr/bin/env python3
"""Check bilingual specification parity and root-module catalog coverage."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC_EN = ROOT / "docs" / "SYSTEM_SPEC.md"
SPEC_JA = ROOT / "docs" / "SYSTEM_SPEC.ja.md"
CATALOG_EN = ROOT / "docs" / "MODULE_CATALOG.md"
CATALOG_JA = ROOT / "docs" / "MODULE_CATALOG.ja.md"

_NUMBERED_H2 = re.compile(r"^##\s+(\d+)\.\s+", re.MULTILINE)
_MODULE = re.compile(r"`([^`/]+\.py)`")
_README_HEADING = re.compile(r"^(#{2,6})\s+.+$", re.MULTILINE)
_START = "<!-- ROOT_MODULES_START -->"
_END = "<!-- ROOT_MODULES_END -->"
_README_RUNTIME_MARKERS = {
    "README.md": (
        "Only the two short-candidate switches are mutable",
        "US proxy-eligibility rate",
        "JP loanable-eligibility rate",
        "excludes that individual ticker fail-closed",
        "authority source, last confirmation time, freshness, and job heartbeats",
    ),
    "README.ja.md": (
        "変更可能なのは日米の空売り候補スイッチだけ",
        "米国はproxy適格率",
        "日本株は実データの貸借可能率",
        "その銘柄だけを fail-closed で除外",
        "最終確認時刻、鮮度、ジョブのheartbeat",
    ),
}
_EXPECTED_SPEC_SECTIONS = [str(number) for number in range(1, 25)]
_SPEC_REVIEW_MARKERS = {
    "SYSTEM_SPEC.md": (
        "The persisted-scaler inference contract is implemented",
        "## 21. Review artifact map",
        "## 22. Write authority and order boundary",
        "## 23. Prose-first review protocol",
        "## 24. Current limits and documentation maintenance",
    ),
    "SYSTEM_SPEC.ja.md": (
        "per-ticker scalerの永続化inference契約は実装済みです",
        "## 21. レビュー用artifact map",
        "## 22. 書込み権威と注文境界",
        "## 23. 文章から始めるreview手順",
        "## 24. 現在の限界と文書保守",
    ),
}


def numbered_sections(path: Path) -> list[str]:
    return _NUMBERED_H2.findall(path.read_text(encoding="utf-8"))


def catalog_modules(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    if _START not in text or _END not in text:
        raise ValueError(f"{path}: root-module marker is missing")
    body = text.split(_START, 1)[1].split(_END, 1)[0]
    return set(_MODULE.findall(body))


def catalog_module_entries(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if _START not in text or _END not in text:
        raise ValueError(f"{path}: root-module marker is missing")
    body = text.split(_START, 1)[1].split(_END, 1)[0]
    return _MODULE.findall(body)


def readme_heading_levels(path: Path) -> list[int]:
    return [len(match.group(1)) for match in _README_HEADING.finditer(path.read_text(encoding="utf-8"))]


def root_modules() -> set[str]:
    return {path.name for path in ROOT.glob("*.py") if path.is_file()}


def check() -> list[str]:
    failures: list[str] = []
    required = (SPEC_EN, SPEC_JA, CATALOG_EN, CATALOG_JA)
    for path in required:
        if not path.is_file():
            failures.append(f"missing documentation file: {path.relative_to(ROOT)}")
    if failures:
        return failures

    for english, japanese, label in (
        (SPEC_EN, SPEC_JA, "system specification"),
        (CATALOG_EN, CATALOG_JA, "module catalog"),
    ):
        en_sections = numbered_sections(english)
        ja_sections = numbered_sections(japanese)
        if en_sections != ja_sections:
            failures.append(
                f"{label}: numbered H2 sections differ: "
                f"English={en_sections}, Japanese={ja_sections}"
            )

    spec_sections = numbered_sections(SPEC_EN)
    if spec_sections != _EXPECTED_SPEC_SECTIONS:
        failures.append(
            "system specification: expected numbered H2 sections "
            f"{_EXPECTED_SPEC_SECTIONS}, found {spec_sections}"
        )

    for name, markers in _SPEC_REVIEW_MARKERS.items():
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                failures.append(f"docs/{name}: missing reviewer-guide marker: {marker}")

    expected = root_modules()
    for path in (CATALOG_EN, CATALOG_JA):
        documented = catalog_modules(path)
        missing = sorted(expected - documented)
        extra = sorted(documented - expected)
        if missing:
            failures.append(
                f"{path.relative_to(ROOT)}: undocumented root modules: {missing}"
            )
        if extra:
            failures.append(
                f"{path.relative_to(ROOT)}: catalog entries not found at root: {extra}"
            )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_ja = (ROOT / "README.ja.md").read_text(encoding="utf-8")
    en_heading_levels = readme_heading_levels(ROOT / "README.md")
    ja_heading_levels = readme_heading_levels(ROOT / "README.ja.md")
    if en_heading_levels != ja_heading_levels:
        failures.append(
            "README heading structures differ: "
            f"English={en_heading_levels}, Japanese={ja_heading_levels}"
        )
    if len(en_heading_levels) != 45:
        failures.append(
            f"README contract expects 45 paired sections, found {len(en_heading_levels)}"
        )

    for link in ("docs/SYSTEM_SPEC.md", "docs/MODULE_CATALOG.md"):
        if link not in readme:
            failures.append(f"README.md: missing link to {link}")
    for link in ("docs/SYSTEM_SPEC.ja.md", "docs/MODULE_CATALOG.ja.md"):
        if link not in readme_ja:
            failures.append(f"README.ja.md: missing link to {link}")

    readmes = {"README.md": readme, "README.ja.md": readme_ja}
    for name, markers in _README_RUNTIME_MARKERS.items():
        for marker in markers:
            if marker not in readmes[name]:
                failures.append(f"{name}: missing runtime-contract marker: {marker}")

    for path in (CATALOG_EN, CATALOG_JA):
        entries = catalog_module_entries(path)
        duplicates = sorted({name for name in entries if entries.count(name) > 1})
        if duplicates:
            failures.append(
                f"{path.relative_to(ROOT)}: duplicate root-module entries: {duplicates}"
            )
    return failures


def main() -> int:
    failures = check()
    if failures:
        print("Documentation consistency check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Documentation consistency check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

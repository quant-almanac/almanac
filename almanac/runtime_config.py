"""Runtime naming compatibility for ALMANAC.

New ALMANAC names are canonical. A separate, older NexusTrader naming
generation remains supported in a couple of spots below; that predates and is
unrelated to the more recent KAIROS rename, which has been fully retired.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]


def get_env(
    name: str,
    default: str | None = None,
    *,
    legacy_name: str | None = None,
) -> str | None:
    """Read an env var, falling back to a non-KAIROS legacy alias if given."""
    old_value = os.environ.get(legacy_name) if legacy_name else None
    new_value = os.environ.get(name)
    if new_value not in (None, ""):
        return new_value
    if old_value not in (None, ""):
        return old_value
    return default


def env_bool(name: str, default: bool = False, *, legacy_name: str | None = None) -> bool:
    raw = get_env(name, legacy_name=legacy_name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, legacy_name: str | None = None) -> int:
    raw = get_env(name, str(default), legacy_name=legacy_name)
    try:
        return int(float(raw)) if raw is not None else default
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float, *, legacy_name: str | None = None) -> float:
    raw = get_env(name, str(default), legacy_name=legacy_name)
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def resolve_api_key_path() -> Path:
    """Return the configured API key path."""
    return Path.home() / ".config" / "almanac" / "api_key"


def load_api_key() -> str:
    """Load the FastAPI write key from env or local config."""
    env_key = get_env("ALMANAC_API_KEY", "")
    if env_key:
        return env_key.strip()
    key_path = resolve_api_key_path()
    if key_path.exists():
        try:
            return key_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def default_secrets_paths() -> list[Path | str]:
    """Return secrets files in read order: ALMANAC first, legacy fallback second."""
    configured = get_env(
        "ALMANAC_SECRETS_FILE",
        legacy_name="NEXUSTRADER_SECRETS_FILE",
    )
    if configured:
        return [configured]
    return [
        Path.home() / ".almanac_secrets",
        Path.home() / ".nexustrader_secrets",
    ]


def resolve_db_path(base_dir: Path | str | None = None) -> Path:
    """Resolve the portfolio SQLite path without renaming live databases."""
    root = Path(base_dir) if base_dir is not None else REPO_ROOT
    configured = get_env("ALMANAC_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    new_path = root / "almanac.db"
    if new_path.exists():
        return new_path
    legacy_path = root / "nexustrader.db"
    if legacy_path.exists():
        return legacy_path
    return new_path


def existing_sqlite_targets(base_dir: Path | str | None = None) -> list[tuple[str, Path]]:
    """Return existing SQLite backup targets with stable archive names."""
    root = Path(base_dir) if base_dir is not None else REPO_ROOT
    candidates: Iterable[tuple[str, Path]] = (
        ("almanac.db", root / "almanac.db"),
        ("nexustrader.db", root / "nexustrader.db"),
    )
    return [(name, path) for name, path in candidates if path.exists()]

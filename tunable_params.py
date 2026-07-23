"""Runtime-safe access to ALMANAC tunable parameters.

``tunable_params.json`` is the tracked definition/seed file.  Mutable values,
AI recommendations and provenance live in ``tunable_params_state.json`` so a
scheduled Auto Tune run never dirties the source checkout.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from utils import atomic_write_json, load_json

BASE_DIR = Path(__file__).parent
PARAMS_FILE = BASE_DIR / "tunable_params.json"
STATE_FILE = BASE_DIR / "tunable_params_state.json"
HISTORY_FILE = BASE_DIR / "tunable_params_history.jsonl"
LOCK_FILE = BASE_DIR / "locks" / "tunable_params.lock"

_RUNTIME_FIELDS = {
    "value",
    "last_changed",
    "last_changed_by",
    "last_rationale",
    "ai_recommended",
    "ai_rationale",
    "ai_recommended_at",
}


class TuningValidationError(ValueError):
    pass


class TuningConflictError(TuningValidationError):
    pass


@contextmanager
def _state_lock():
    """Serialize state mutations across API and LaunchAgent processes."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _read_definitions() -> dict:
    try:
        if not PARAMS_FILE.exists():
            return {}
        value = load_json(PARAMS_FILE, default={}) or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _seed_runtime_state(definitions: dict | None = None) -> dict:
    definitions = definitions if definitions is not None else _read_definitions()
    params: dict[str, dict] = {}
    for key, entry in definitions.items():
        if not isinstance(entry, dict):
            continue
        runtime = {field: entry[field] for field in _RUNTIME_FIELDS if field in entry}
        if "value" not in runtime:
            runtime["value"] = entry.get("default")
        params[key] = runtime
    return {
        "version": 1,
        "revision": 0,
        "migrated_at": datetime.now().isoformat(),
        "updated_at": None,
        "params": params,
    }


def _read_runtime_state(*, seed_if_missing: bool = True) -> dict:
    try:
        value = load_json(STATE_FILE, default={}) or {}
        if isinstance(value, dict) and isinstance(value.get("params"), dict):
            return value
    except Exception:
        pass
    return _seed_runtime_state() if seed_if_missing else {}


def ensure_runtime_state() -> dict:
    """Create the mutable state from the tracked seed without changing values."""
    with _state_lock():
        existing = _read_runtime_state(seed_if_missing=False)
        if existing:
            return existing
        state = _seed_runtime_state()
        atomic_write_json(STATE_FILE, state)
        return state


def _merged(definitions: dict, state: dict) -> dict:
    runtime_params = state.get("params") if isinstance(state, dict) else {}
    runtime_params = runtime_params if isinstance(runtime_params, dict) else {}
    result: dict[str, dict] = {}
    for key, definition in definitions.items():
        if not isinstance(definition, dict):
            continue
        row = dict(definition)
        runtime = runtime_params.get(key)
        if isinstance(runtime, dict):
            row.update({field: runtime[field] for field in _RUNTIME_FIELDS if field in runtime})
        result[key] = row
    return result


def _read_all() -> dict:
    return _merged(_read_definitions(), _read_runtime_state())


def get(key: str, fallback: Any = None) -> Any:
    entry = _read_all().get(key)
    if not isinstance(entry, dict):
        return fallback
    value = entry.get("value")
    return value if value is not None else fallback


def get_revision() -> int:
    try:
        return int(_read_runtime_state().get("revision") or 0)
    except Exception:
        return 0


def list_all() -> dict:
    return _read_all()


def get_meta(key: str) -> dict | None:
    return _read_all().get(key)


def _validate(entry: dict, value: Any) -> Any:
    if not isinstance(entry, dict):
        return value
    typ = entry.get("type", "number")
    if typ == "number":
        try:
            value = float(value)
            if entry.get("integer"):
                value = int(value)
        except Exception as exc:
            raise TuningValidationError(f"値が数値ではない: {value}") from exc
        minimum = entry.get("min")
        maximum = entry.get("max")
        if minimum is not None and value < minimum:
            raise TuningValidationError(f"値 {value} が min {minimum} を下回る")
        if maximum is not None and value > maximum:
            raise TuningValidationError(f"値 {value} が max {maximum} を上回る")
        step = entry.get("step")
        base = entry.get("min", 0)
        if step not in (None, 0):
            units = (float(value) - float(base)) / float(step)
            if abs(units - round(units)) > 1e-8:
                raise TuningValidationError(f"値 {value} が step {step} に一致しない")
    elif typ == "list" and not isinstance(value, list):
        raise TuningValidationError(f"値がリストではない: {value}")
    return value


def validate_candidate(key: str, value: Any) -> Any:
    entry = get_meta(key)
    if entry is None:
        raise KeyError(f"unknown tunable key: {key}")
    return _validate(entry, value)


def validate_invariants(values: dict[str, Any]) -> None:
    usd = values.get("currency_usd_target_pct")
    jpy = values.get("currency_jpy_target_pct")
    if usd is not None and jpy is not None and abs(float(usd) + float(jpy) - 100.0) > 1e-8:
        raise TuningValidationError("USD/JPY 目標比率の合計は100である必要があります")
    sector_max = values.get("sector_max_pct")
    sector_trigger = values.get("sector_rebalance_threshold_pct")
    if sector_max is not None and sector_trigger is not None:
        if float(sector_trigger) > float(sector_max) - 1.0:
            raise TuningValidationError("セクター発動閾値は上限より1ポイント以上低くしてください")


def _append_history(rows: Iterable[dict]) -> None:
    rows = list(rows)
    if not rows:
        return
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()


def apply_batch(
    changes: dict[str, Any],
    *,
    source: str = "user",
    rationale: str | None = None,
    rationale_by_key: dict[str, str] | None = None,
    expected_values: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict:
    """Validate and atomically apply a related set of values."""
    if not changes:
        return {"changed": {}, "revision": get_revision()}
    rationale_by_key = rationale_by_key or {}
    expected_values = expected_values or {}
    definitions = _read_definitions()
    now = datetime.now().isoformat()

    with _state_lock():
        old_state = _read_runtime_state()
        current = _merged(definitions, old_state)
        validated: dict[str, Any] = {}
        for key, value in changes.items():
            entry = current.get(key)
            if entry is None:
                raise KeyError(f"unknown tunable key: {key}")
            if key in expected_values and entry.get("value") != expected_values[key]:
                raise TuningConflictError(
                    f"{key} changed concurrently: expected={expected_values[key]} actual={entry.get('value')}"
                )
            validated[key] = _validate(entry, value)

        prospective = {key: row.get("value") for key, row in current.items()}
        prospective.update(validated)
        validate_invariants(prospective)

        state = json.loads(json.dumps(old_state, ensure_ascii=False))
        state.setdefault("params", {})
        changed: dict[str, dict] = {}
        history_rows: list[dict] = []
        for key, new_value in validated.items():
            old_value = current[key].get("value")
            if new_value == old_value:
                continue
            row = dict(state["params"].get(key) or {})
            row.update({
                "value": new_value,
                "last_changed": now,
                "last_changed_by": source,
                "last_rationale": rationale_by_key.get(key, rationale),
            })
            state["params"][key] = row
            change = {"old_value": old_value, "new_value": new_value}
            changed[key] = change
            history_rows.append({
                "key": key,
                **change,
                "source": source,
                "rationale": rationale_by_key.get(key, rationale),
                "timestamp": now,
                "run_id": run_id,
            })

        if not changed:
            return {"changed": {}, "revision": int(old_state.get("revision") or 0)}
        state["revision"] = int(old_state.get("revision") or 0) + 1
        state["updated_at"] = now
        state["last_transaction"] = {
            "run_id": run_id,
            "source": source,
            "timestamp": now,
            "changes": changed,
        }
        atomic_write_json(STATE_FILE, state)
        try:
            _append_history(history_rows)
        except Exception:
            atomic_write_json(STATE_FILE, old_state)
            raise
        return {"changed": changed, "revision": state["revision"], "updated_at": now}


def set_value(key: str, value: Any, source: str = "user", rationale: str | None = None) -> dict:
    apply_batch({key: value}, source=source, rationale=rationale)
    result = get_meta(key)
    if result is None:
        raise KeyError(f"unknown tunable key: {key}")
    return result


def reset(key: str, source: str = "user") -> dict:
    entry = get_meta(key)
    if entry is None:
        raise KeyError(f"unknown tunable key: {key}")
    return set_value(key, entry.get("default"), source=source, rationale="reset to default")


def set_ai_recommendations(recommendations: Iterable[dict]) -> None:
    definitions = _read_definitions()
    now = datetime.now().isoformat()
    with _state_lock():
        state = _read_runtime_state()
        state.setdefault("params", {})
        changed = False
        for recommendation in recommendations:
            key = recommendation.get("key")
            if key not in definitions:
                continue
            value = _validate(definitions[key], recommendation.get("recommended"))
            row = dict(state["params"].get(key) or {})
            row.update({
                "ai_recommended": value,
                "ai_rationale": recommendation.get("rationale", ""),
                "ai_recommended_at": now,
            })
            state["params"][key] = row
            changed = True
        if changed:
            state["updated_at"] = now
            atomic_write_json(STATE_FILE, state)


def set_ai_recommendation(key: str, recommended_value: Any, rationale: str) -> None:
    set_ai_recommendations([{"key": key, "recommended": recommended_value, "rationale": rationale}])


def categories() -> list[str]:
    return sorted({row.get("category") for row in _read_all().values() if row.get("category")})


def by_category() -> dict[str, list[tuple[str, dict]]]:
    grouped: dict[str, list[tuple[str, dict]]] = {}
    for key, row in _read_all().items():
        grouped.setdefault(row.get("category", "other"), []).append((key, row))
    return grouped


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if not args or args[0] == "list":
        print(json.dumps(list_all(), ensure_ascii=False, indent=2))
    elif args[0] == "get" and len(args) >= 2:
        print(get(args[1], "(none)"))
    elif args[0] == "set" and len(args) >= 3:
        print(set_value(args[1], args[2], source="cli"))
    elif args[0] == "migrate":
        print(json.dumps(ensure_runtime_state(), ensure_ascii=False, indent=2))
    elif args[0] == "categories":
        for category in categories():
            print(category)

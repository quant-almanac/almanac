"""Runtime feature controls and operational status for the Web UI.

The tracked configuration remains the safe default.  User changes are stored in
``feature_control_state.json`` and overlaid at read time, so the public
repository can stay fail-closed while one installation explicitly opts in.

Only features whose consumers are wired to this module are mutable here.
Safety-managed features (GINN promotion, Kelly/FX shadow execution, Auto Tune)
are reported for visibility but retain their existing authorities.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils import atomic_write_json, process_lock

BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = BASE_DIR / "disclosure_shadow_config.json"
SCHEMA_VERSION = 1

_SHORT_FEATURES = {
    "us_short": {
        "config_key": "us_short_enabled",
        "default": False,
        "label": "米国株の空売り",
        "market": "US",
    },
    "jp_short": {
        "config_key": "jp_short_enabled",
        "default": True,
        "label": "日本株の空売り",
        "market": "JP",
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(base_dir: Path | str | None = None) -> Path:
    if base_dir is not None:
        return Path(base_dir)
    return Path(os.environ.get("ALMANAC_STATE_DIR") or BASE_DIR)


def _state_path(base_dir: Path | str | None = None) -> Path:
    return _root(base_dir) / "feature_control_state.json"


def _load_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, json.JSONDecodeError):
        return default


def _load_state(base_dir: Path | str | None = None) -> dict[str, Any]:
    value = _load_json(
        _state_path(base_dir),
        {"schema_version": SCHEMA_VERSION, "features": {}, "history": []},
    )
    if not isinstance(value, dict):
        return {"schema_version": SCHEMA_VERSION, "features": {}, "history": []}
    if not isinstance(value.get("features"), dict):
        value["features"] = {}
    if not isinstance(value.get("history"), list):
        value["history"] = []
    return value


def _load_short_defaults(
    *,
    config_path: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> dict[str, bool]:
    path = Path(config_path) if config_path is not None else _root(base_dir) / DEFAULT_CONFIG_PATH.name
    raw = _load_json(path, {})
    raw = raw if isinstance(raw, dict) else {}
    return {
        key: bool(raw.get(meta["config_key"], meta["default"]))
        for key, meta in _SHORT_FEATURES.items()
    }


def configured_short_features(
    *,
    config_path: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> dict[str, bool]:
    """Return configured short switches after applying the runtime overlay."""
    values = _load_short_defaults(config_path=config_path, base_dir=base_dir)
    state = _load_state(base_dir)
    for key in _SHORT_FEATURES:
        entry = (state.get("features") or {}).get(key)
        if isinstance(entry, dict) and isinstance(entry.get("enabled"), bool):
            values[key] = entry["enabled"]
    return values


def short_market_enabled(
    market: str,
    *,
    config_path: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> bool:
    key = "jp_short" if str(market).upper() == "JP" else "us_short"
    return configured_short_features(config_path=config_path, base_dir=base_dir)[key]


def overlay_disclosure_config(
    config: dict[str, Any],
    *,
    config_path: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Overlay the two short switches onto a disclosure config copy."""
    result = dict(config)
    values = configured_short_features(config_path=config_path, base_dir=base_dir)
    for key, meta in _SHORT_FEATURES.items():
        result[meta["config_key"]] = values[key]
    return result


def set_feature(
    key: str,
    enabled: bool,
    *,
    actor: str,
    rationale: str | None = None,
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Atomically set a mutable feature and retain a compact audit trail."""
    if key not in _SHORT_FEATURES:
        raise KeyError(key)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    changed_at = _now_iso()
    with process_lock("feature_controls", timeout=10):
        state = _load_state(base_dir)
        previous = configured_short_features(base_dir=base_dir).get(key)
        state["features"][key] = {
            "enabled": enabled,
            "updated_at": changed_at,
            "updated_by": actor,
            "rationale": rationale or "",
        }
        history = list(state.get("history") or [])
        history.append({
            "feature": key,
            "old_enabled": previous,
            "new_enabled": enabled,
            "changed_at": changed_at,
            "changed_by": actor,
            "rationale": rationale or "",
        })
        state["history"] = history[-100:]
        state["schema_version"] = SCHEMA_VERSION
        state["updated_at"] = changed_at
        path = _state_path(base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, state)
    return get_feature_status(key, base_dir=base_dir)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_hours(value: Any) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0)


def _latest_short_pipeline(root: Path, market: str) -> dict[str, Any]:
    """Read the latest screener funnel without conflating its stages."""
    payload = _load_json(root / "short_candidates.json", {})
    payload = payload if isinstance(payload, dict) else {}
    candidates = payload.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    is_jp = market == "JP"

    def _belongs(row: Any) -> bool:
        ticker = str(row.get("ticker") or "") if isinstance(row, dict) else ""
        return bool(ticker) and ticker.endswith(".T") == is_jp

    market_candidates = [row for row in candidates if _belongs(row)]
    suffix = "jp" if is_jp else "us"
    requested = payload.get(f"universe_requested_{suffix}")
    downloaded = payload.get(f"price_data_{suffix}")
    candidate_count = payload.get(f"candidate_count_{suffix}")
    shortable_count = payload.get(f"shortable_count_{suffix}")
    if not isinstance(candidate_count, int):
        candidate_count = len(market_candidates)
    if not isinstance(shortable_count, int):
        shortable_count = sum(
            1 for row in market_candidates
            if isinstance(row, dict) and row.get("shortable") is True
        )
    coverage = None
    if isinstance(requested, int) and requested > 0 and isinstance(downloaded, int):
        coverage = round(downloaded / requested * 100, 1)
    return {
        "latest_scan_requested": requested if isinstance(requested, int) else None,
        "latest_scan_downloaded": downloaded if isinstance(downloaded, int) else None,
        "latest_scan_coverage_pct": coverage,
        "latest_candidates": candidate_count,
        "latest_shortable": shortable_count,
        "latest_scan_as_of": payload.get("as_of"),
        "latest_scan_status": payload.get("data_quality"),
    }


def _short_readiness(key: str, root: Path) -> dict[str, Any]:
    if key == "us_short":
        payload = _load_json(root / "data" / "broker_short_us.json", {})
        tickers = payload.get("tickers") if isinstance(payload, dict) else {}
        tickers = tickers if isinstance(tickers, dict) else {}
        eligible = sum(
            1 for row in tickers.values()
            if isinstance(row, dict) and (row.get("rakuten") is True or row.get("sbi") is True)
        )
        universe_count = payload.get("universe_count") if isinstance(payload, dict) else None
        if not isinstance(universe_count, int):
            ticker_config = _load_json(root / "tickers.json", {})
            broad = ticker_config.get("all") if isinstance(ticker_config, dict) else []
            universe_count = sum(
                1 for ticker in (broad or [])
                if ticker and not str(ticker).endswith(".T")
            )
        if not universe_count:
            universe_count = len(tickers)
        as_of = payload.get("generated_at") if isinstance(payload, dict) else None
        age = _age_hours(as_of)
        blockers: list[str] = []
        if not tickers:
            blockers.append("米国株の借株可否データがありません")
        if age is None:
            blockers.append("借株可否データの取得時刻を確認できません")
        elif age > 168:
            blockers.append(f"借株可否データが{age / 24:.1f}日経過しています")
        pipeline = _latest_short_pipeline(root, "US")
        warnings: list[str] = []
        latest_coverage = pipeline.get("latest_scan_coverage_pct")
        if isinstance(latest_coverage, (int, float)) and latest_coverage < 85:
            warnings.append(f"最新の価格取得率が{latest_coverage:.1f}%です")
        return {
            "ready": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "eligible_instruments": eligible,
            "availability_universe_instruments": universe_count,
            "availability_coverage_pct": (
                round(eligible / universe_count * 100, 1)
                if universe_count else None
            ),
            "source_as_of": as_of,
            "source_age_hours": round(age, 1) if age is not None else None,
            "source": "data/broker_short_us.json",
            "source_note": "基準ベースの近似。最終可否・料率は発注画面が権威",
            **pipeline,
        }

    loanable = _load_json(root / "data" / "jp_loanable_state.json", {})
    jsf = _load_json(root / "data" / "jsf_lending_state.json", {})
    loanable_rows = loanable.get("loanable_by_ticker") if isinstance(loanable, dict) else {}
    loanable_rows = loanable_rows if isinstance(loanable_rows, dict) else {}
    loanable_as_of = loanable.get("generated_at") if isinstance(loanable, dict) else None
    jsf_as_of = jsf.get("generated_at") if isinstance(jsf, dict) else None
    loanable_age = _age_hours(loanable_as_of)
    jsf_age = _age_hours(jsf_as_of)
    blockers = []
    if not loanable_rows:
        blockers.append("日本株の貸借銘柄データがありません")
    if loanable_age is None or loanable_age > 72:
        blockers.append("貸借銘柄データが未取得または期限切れです")
    if jsf_age is None or jsf_age > 72:
        blockers.append("日証金データが未取得または期限切れです")
    pipeline = _latest_short_pipeline(root, "JP")
    warnings = []
    latest_coverage = pipeline.get("latest_scan_coverage_pct")
    if isinstance(latest_coverage, (int, float)) and latest_coverage < 85:
        warnings.append(f"最新の価格取得率が{latest_coverage:.1f}%です")
    eligible = sum(1 for value in loanable_rows.values() if value is True)
    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "eligible_instruments": eligible,
        "availability_universe_instruments": len(loanable_rows),
        "availability_coverage_pct": (
            round(eligible / len(loanable_rows) * 100, 1)
            if loanable_rows else None
        ),
        "source_as_of": min(
            [value for value in (loanable_as_of, jsf_as_of) if value],
            default=None,
        ),
        "source_age_hours": round(max(
            [value for value in (loanable_age, jsf_age) if value is not None],
            default=0.0,
        ), 1),
        "source": "data/jp_loanable_state.json + data/jsf_lending_state.json",
        "source_note": "銘柄ごとの貸借可否・逆日歩・規制を発注前に再確認",
        **pipeline,
    }


def _short_status(key: str, *, base_dir: Path | str | None = None) -> dict[str, Any]:
    root = _root(base_dir)
    configured = configured_short_features(base_dir=base_dir)[key]
    readiness = _short_readiness(key, root)
    effective = configured and readiness["ready"]
    state_entry = (_load_state(base_dir).get("features") or {}).get(key) or {}
    if not configured:
        reason = "ユーザー設定でOFFです"
    elif not readiness["ready"]:
        reason = "設定はONですが、安全確認に必要な入力が不足または期限切れです"
    else:
        reason = "候補生成を有効化しています。自動発注はせず、発注画面で借株可否と料率を再確認します"
    return {
        "key": key,
        "label": _SHORT_FEATURES[key]["label"],
        "category": "short",
        "description": "下落候補の検出と借株可否ゲート。売建注文は人が証券会社画面で確認します。",
        "configured_enabled": configured,
        "effective_enabled": effective,
        "mutable": True,
        "mode": "human_execution_only",
        "auto_order_enabled": False,
        "reason": reason,
        "blockers": readiness["blockers"],
        "warnings": readiness.get("warnings", []),
        "updated_at": state_entry.get("updated_at"),
        "updated_by": state_entry.get("updated_by"),
        **{
            k: v for k, v in readiness.items()
            if k not in {"ready", "blockers", "warnings"}
        },
    }


def _analysis_shadow_status(
    key: str,
    *,
    label: str,
    decision_key: str,
    description: str,
    root: Path,
) -> dict[str, Any]:
    payload = _load_json(root / "ai_portfolio_analysis.json", {})
    synthesis = payload.get("synthesis") if isinstance(payload, dict) else {}
    decision = synthesis.get(decision_key) if isinstance(synthesis, dict) else None
    decision = decision if isinstance(decision, dict) else {}
    mode = str(decision.get("mode") or "shadow")
    present = bool(decision)
    return {
        "key": key,
        "label": label,
        "category": "shadow",
        "description": description,
        "configured_enabled": mode != "off",
        "effective_enabled": present and mode != "off",
        "mutable": False,
        "mode": mode,
        "auto_order_enabled": False,
        "reason": (
            "影実行の結果を記録しています。実アクションや注文は変更しません"
            if present and mode != "off"
            else "最新分析に影実行結果がなく、実効状態を確認できません"
        ),
        "blockers": [] if present else ["latest_shadow_result_missing"],
        "updated_at": payload.get("as_of") if isinstance(payload, dict) else None,
        "control_hint": "安全性検証後に別の有効化判断が必要です",
    }


def _ginn_status(root: Path) -> dict[str, Any]:
    disabled = os.environ.get("ALMANAC_DISABLE_GINN", "").strip().lower() in {"1", "true", "yes"}
    pointer = _load_json(root / "models" / "ginn" / "current.json", {})
    version = pointer.get("version") if isinstance(pointer, dict) else None
    manifest = root / "models" / "ginn" / str(version) / "manifest.json" if version else None
    promoted = bool(version and manifest and manifest.exists())
    effective = promoted and not disabled
    if disabled:
        reason = "緊急無効化フラグでGARCH固定です"
    elif not promoted:
        reason = "昇格済みGINN bundleがないためGARCHへfail-closedしています"
    else:
        reason = "検証・昇格済みbundleを利用できます"
    return {
        "key": "ginn",
        "label": "GINNボラティリティ",
        "category": "model",
        "description": "検証済みbundleだけを使用し、条件を満たさなければGJR-GARCHへ縮退します。",
        "configured_enabled": not disabled,
        "effective_enabled": effective,
        "mutable": False,
        "mode": "promoted_bundle_only",
        "auto_order_enabled": False,
        "reason": reason,
        "blockers": [] if effective else ["promoted_bundle_missing_or_disabled"],
        "model_version": version,
        "control_hint": "モデル昇格ゲートが権威のため、この画面から強制ONにはできません",
    }


def _auto_tune_status() -> dict[str, Any]:
    try:
        from auto_tune import get_status
        status = get_status()
    except Exception as exc:
        status = {"mode": "unknown", "disabled_reason": str(exc)[:160]}
    mode = str(status.get("mode") or "off")
    enabled = mode != "off"
    return {
        "key": "auto_tune",
        "label": "Auto Tune",
        "category": "automation",
        "description": "許可された運用パラメータを監査付きでプレビューまたは適用します。",
        "configured_enabled": enabled,
        "effective_enabled": bool(status.get("effective_apply")) if mode == "apply" else enabled,
        "mutable": False,
        "mode": mode,
        "auto_order_enabled": False,
        "reason": status.get("disabled_reason") or (
            "安全検証に合格したパラメータ変更を自動適用します"
            if mode == "apply" else "推奨だけを記録します" if mode == "shadow" else "停止中です"
        ),
        "blockers": [],
        "updated_at": status.get("last_run"),
        "control_hint": "同じ画面のAuto Tune欄で変更できます",
    }


def _execution_plan_status(root: Path) -> dict[str, Any]:
    try:
        from tunable_params import get as tunable
        mode = str(tunable("execution_plan_gate_mode", "observe") or "observe")
    except Exception:
        mode = "observe"
    plan = _load_json(root / "execution_plan_state.json", {})
    active = isinstance(plan, dict) and plan.get("status") == "active"
    effective = active and mode == "enforce"
    return {
        "key": "execution_plan",
        "label": "月次・週次実行計画ゲート",
        "category": "policy",
        "description": "AI候補を月次・週次の資金計画と照合します。",
        "configured_enabled": mode in {"observe", "enforce"},
        "effective_enabled": effective,
        "mutable": False,
        "mode": mode,
        "auto_order_enabled": False,
        "reason": (
            "計画との不整合を実候補から除外します"
            if effective else "観測モードのため、計画との不整合は記録のみです"
        ),
        "blockers": [] if active else ["execution_plan_not_active"],
        "updated_at": plan.get("as_of") if isinstance(plan, dict) else None,
        "control_hint": "下の運用パラメータ execution_plan_gate_mode が権威です",
    }


def get_feature_status(
    key: str,
    *,
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    root = _root(base_dir)
    if key in _SHORT_FEATURES:
        return _short_status(key, base_dir=base_dir)
    if key == "kelly_shadow":
        return _analysis_shadow_status(
            key,
            label="recommendation Kelly",
            decision_key="kelly_shadow_decision",
            description="推薦履歴から推定した上限を、実経路と同じ凍結入力で反実仮想評価します。",
            root=root,
        )
    if key == "fx_hedge_shadow":
        return _analysis_shadow_status(
            key,
            label="FXヘッジ",
            decision_key="fx_hedge_shadow_decision",
            description="経済通貨エクスポージャーと実ヘッジ残高から、ヘッジ目標を影実行します。",
            root=root,
        )
    if key == "ginn":
        return _ginn_status(root)
    if key == "auto_tune":
        return _auto_tune_status()
    if key == "execution_plan":
        return _execution_plan_status(root)
    raise KeyError(key)


def list_feature_statuses(
    *,
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    keys = (
        "us_short",
        "jp_short",
        "ginn",
        "kelly_shadow",
        "fx_hedge_shadow",
        "execution_plan",
        "auto_tune",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "features": [get_feature_status(key, base_dir=base_dir) for key in keys],
    }

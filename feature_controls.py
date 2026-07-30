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
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from utils import atomic_write_json, process_lock

BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = BASE_DIR / "disclosure_shadow_config.json"
SCHEMA_VERSION = 1
SYSTEM_LOCAL_TZ = ZoneInfo("Asia/Tokyo")
_STATUS_CACHE_TTL_SECONDS = 300.0
_GINN_GATE_CACHE: dict[str, Any] = {}
_OPTIONS_SUMMARY_CACHE: dict[str, Any] = {}

_SHORT_FEATURES = {
    "us_short": {
        "config_key": "us_short_enabled",
        "default": False,
        "label": "米国株の空売り",
        "market": "US",
    },
    "jp_short": {
        "config_key": "jp_short_enabled",
        # A missing or malformed tracked config must not silently enable a
        # risk-increasing lane.  Production opts in through the ignored
        # runtime overlay, while public clones start fail-closed.
        "default": False,
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
        parsed = parsed.replace(tzinfo=SYSTEM_LOCAL_TZ)
    return parsed.astimezone(timezone.utc)


def _age_hours(value: Any) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0)


def _freshness_status(
    *,
    exists: bool,
    age_hours: float | None,
    max_age_hours: float | None,
) -> str:
    if not exists:
        return "missing"
    if max_age_hours is None:
        return "not_applicable"
    if age_hours is None:
        return "unknown"
    return "stale" if age_hours > max_age_hours else "fresh"


def _read_only_status(
    *,
    key: str,
    label: str,
    category: str,
    description: str,
    mode: str,
    configured_enabled: bool,
    effective_enabled: bool,
    reason: str,
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
    source: str | None = None,
    source_as_of: Any = None,
    max_age_hours: float | None = None,
    source_note: str | None = None,
    control_hint: str,
    metrics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    age = _age_hours(source_as_of)
    return {
        "key": key,
        "label": label,
        "category": category,
        "description": description,
        "configured_enabled": configured_enabled,
        "effective_enabled": effective_enabled,
        "mutable": False,
        "mode": mode,
        "auto_order_enabled": False,
        "reason": reason,
        "blockers": blockers or [],
        "warnings": warnings or [],
        "source": source,
        "source_as_of": source_as_of,
        "source_age_hours": round(age, 1) if age is not None else None,
        "freshness_status": _freshness_status(
            exists=source_as_of is not None or max_age_hours is None,
            age_hours=age,
            max_age_hours=max_age_hours,
        ),
        "source_note": source_note,
        "control_hint": control_hint,
        "metrics": metrics or [],
    }


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
            "availability_label": "借株proxy該当",
            "availability_metric_kind": "proxy_eligibility_rate",
            "source_as_of": as_of,
            "source_age_hours": round(age, 1) if age is not None else None,
            "freshness_status": _freshness_status(
                exists=bool(tickers),
                age_hours=age,
                max_age_hours=168,
            ),
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
        "availability_label": "貸借可能",
        "availability_metric_kind": "loanable_eligibility_rate",
        "source_as_of": min(
            [value for value in (loanable_as_of, jsf_as_of) if value],
            default=None,
        ),
        "source_age_hours": round(max(
            [value for value in (loanable_age, jsf_age) if value is not None],
            default=0.0,
        ), 1),
        "freshness_status": (
            "missing" if not loanable_rows
            else "stale" if any(
                value is None or value > 72
                for value in (loanable_age, jsf_age)
            )
            else "fresh"
        ),
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
        "control_hint": "この画面のスイッチがruntime設定の権威です",
        **{
            k: v for k, v in readiness.items()
            if k not in {"ready", "blockers", "warnings"}
        },
    }


def _margin_long_status(root: Path) -> dict[str, Any]:
    payload = _load_json(root / "margin_long_candidates.json", {})
    payload = payload if isinstance(payload, dict) else {}
    as_of = payload.get("generated_at")
    age = _age_hours(as_of)
    candidates = payload.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    blocked = bool(payload.get("blocked"))
    fresh = age is not None and age <= 72
    effective = bool(payload) and fresh and not blocked
    blockers = []
    if not payload:
        blockers.append("margin_long_candidates_missing")
    elif not fresh:
        blockers.append("margin_long_candidates_stale")
    if blocked:
        blockers.append("margin_long_market_gate_blocked")
    reason = (
        f"信用買い候補を{len(candidates)}件生成しています。注文は人間実行です"
        if effective else
        "相場・VIX関門が停止中で、候補成果物も期限切れです"
        if blocked and not fresh else
        "相場・VIXの安全関門が信用買い候補を停止しています"
        if blocked else
        "信用買い候補の最新成果物を確認できません"
    )
    return _read_only_status(
        key="margin_long",
        label="信用買い候補",
        category="candidate",
        description="押し目反発候補を抽出し、証拠金・集中・相場関門へ渡します。",
        mode="human_execution_only",
        configured_enabled=True,
        effective_enabled=effective,
        reason=reason,
        blockers=blockers,
        source="margin_long_candidates.json",
        source_as_of=as_of,
        max_age_hours=72,
        source_note="候補生成の権威。発注可否は後段の安全関門と証券会社画面で確定",
        control_hint="候補レーンは常時評価し、相場・VIX・証拠金関門が停止権限を持ちます",
        metrics=[{"label": "候補", "value": len(candidates)}],
    )


def _options_status(root: Path) -> dict[str, Any]:
    cache_dir = root / "data" / "options_cache"
    cache_key = str(root.resolve())
    now_mono = time.monotonic()
    cached = _OPTIONS_SUMMARY_CACHE.get(cache_key)
    if (
        root.resolve() == BASE_DIR.resolve()
        and isinstance(cached, dict)
        and now_mono < float(cached.get("expires_at", 0))
    ):
        total = int(cached["total"])
        fresh_count = int(cached["fresh_count"])
        latest = cached.get("latest")
    else:
        timestamps: list[datetime] = []
        total = 0
        fresh_count = 0
        for path in cache_dir.glob("*.json") if cache_dir.is_dir() else ():
            total += 1
            payload = _load_json(path, {})
            fetched_at = payload.get("fetched_at") if isinstance(payload, dict) else None
            parsed = _parse_time(fetched_at)
            if parsed is None:
                continue
            timestamps.append(parsed)
            if (datetime.now(timezone.utc) - parsed).total_seconds() <= 24 * 3600:
                fresh_count += 1
        latest = max(timestamps).isoformat() if timestamps else None
        if root.resolve() == BASE_DIR.resolve():
            _OPTIONS_SUMMARY_CACHE[cache_key] = {
                "expires_at": now_mono + _STATUS_CACHE_TTL_SECONDS,
                "total": total,
                "fresh_count": fresh_count,
                "latest": latest,
            }
    effective = fresh_count > 0
    return _read_only_status(
        key="options_signals",
        label="オプション指標",
        category="signal",
        description="IV・skew・put/callを分析根拠として使います。オプション注文は生成しません。",
        mode="analysis_signal_only",
        configured_enabled=True,
        effective_enabled=effective,
        reason=(
            f"24時間以内のオプション指標が{fresh_count}銘柄あります"
            if effective else "24時間以内のオプション指標がありません"
        ),
        blockers=[] if effective else ["fresh_options_cache_missing"],
        source="data/options_cache/*.json",
        source_as_of=latest,
        max_age_hours=24,
        source_note="options_fetcher が生成する分析用キャッシュ",
        control_hint="分析シグナル専用です。注文商品としての有効化権限はありません",
        metrics=[
            {"label": "新鮮", "value": fresh_count},
            {"label": "全キャッシュ", "value": total},
        ],
    )


def _tax_basis_status() -> dict[str, Any]:
    mode = str(os.environ.get("ALMANAC_TAX_BASIS_MODE", "compare")).strip().lower()
    if mode not in {"legacy", "compare", "total_average"}:
        mode = "compare"
    reason = {
        "legacy": "旧計算を表示元にしています",
        "compare": "旧計算と総平均法を同一入力で比較しています",
        "total_average": "総平均法の計算を表示元にしています",
    }[mode]
    return _read_only_status(
        key="tax_basis",
        label="税務取得費",
        category="policy",
        description="取得費の算出元を切り替えます。証券会社の年間取引報告書が最終権威です。",
        mode=mode,
        configured_enabled=True,
        effective_enabled=True,
        reason=reason,
        source="ALMANAC_TAX_BASIS_MODE + tax_lot.py",
        max_age_hours=None,
        source_note="モードは算出元だけを変え、API schemaは共通です",
        control_hint="環境変数 ALMANAC_TAX_BASIS_MODE が権威です",
    )


def _privacy_status() -> dict[str, Any]:
    try:
        from almanac.llm_safety import get_privacy_mode
        mode = get_privacy_mode()
    except Exception:
        mode = "strict_local"
    reason = {
        "strict_local": "保有情報を含む外部AI呼び出しを禁止しています",
        "anthropic_book_aware": "保有情報を含む呼び出しをAnthropicだけに許可しています",
        "multi_provider_book_aware": "許可済みの複数AIへ保有情報を含む呼び出しを許可しています",
    }.get(mode, "不明な値をstrict_localへ縮退しています")
    return _read_only_status(
        key="privacy_mode",
        label="AIプライバシー",
        category="policy",
        description="保有・残高を含むbook-awareデータを外部AIへ送れる範囲を制限します。",
        mode=mode,
        configured_enabled=True,
        effective_enabled=True,
        reason=reason,
        source="ALMANAC_PRIVACY_MODE + almanac/llm_safety.py",
        max_age_hours=None,
        source_note="未設定・不正値はstrict_localへfail-closed",
        control_hint="秘密設定の ALMANAC_PRIVACY_MODE が権威です",
    )


def _currency_policy_status(root: Path) -> dict[str, Any]:
    mode = str(os.environ.get("ALMANAC_CURRENCY_POLICY_MODE", "shadow")).strip().lower()
    if mode not in {"off", "shadow", "advisory"}:
        mode = "shadow"
    state = _load_json(root / "currency_policy_state.json", {})
    state = state if isinstance(state, dict) else {}
    as_of = state.get("as_of")
    valid_until = state.get("valid_until")
    try:
        not_expired = date.fromisoformat(str(valid_until)) >= datetime.now(SYSTEM_LOCAL_TZ).date()
    except (TypeError, ValueError):
        not_expired = False
    valid = (
        state.get("basis") == "long_tier"
        and isinstance(state.get("usd_target_pct"), (int, float))
        and isinstance(state.get("jpy_target_pct"), (int, float))
        and abs(float(state["usd_target_pct"]) + float(state["jpy_target_pct"]) - 100) <= 0.01
        and not_expired
    )
    effective = mode != "off" and valid
    blockers = []
    if not state:
        blockers.append("currency_policy_state_missing")
    elif not valid:
        blockers.append("currency_policy_state_invalid_or_expired")
    if mode == "off":
        blockers.append("currency_policy_mode_off")
    return _read_only_status(
        key="currency_policy",
        label="動的通貨方針",
        category="shadow",
        description="USD/JPY配分案を観測します。経済通貨resolver完成までは静的目標を維持します。",
        mode=mode,
        configured_enabled=mode != "off",
        effective_enabled=effective,
        reason=(
            "動的案を記録していますが、実配分には静的目標を適用しています"
            if effective else "動的通貨方針は実効入力として使われていません"
        ),
        blockers=blockers,
        source="currency_policy_state.json + ALMANAC_CURRENCY_POLICY_MODE",
        source_as_of=as_of,
        max_age_hours=24 * 14,
        source_note="現段階のshadow/advisoryはstatic fallbackが実配分の権威",
        control_hint="環境変数とeconomic exposure resolverの検証ゲートが権威です",
        metrics=[
            {"label": "USD案", "value": state.get("usd_target_pct")},
            {"label": "JPY案", "value": state.get("jpy_target_pct")},
        ],
    )


def _market_regime_status(root: Path) -> dict[str, Any]:
    state = _load_json(root / "market_regime_v2_state.json", {})
    state = state if isinstance(state, dict) else {}
    assessment = state.get("assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    portfolio = assessment.get("portfolio")
    portfolio = portfolio if isinstance(portfolio, dict) else {}
    mode = str(
        assessment.get("mode")
        or os.environ.get("ALMANAC_MARKET_REGIME_V2_MODE", "advisory")
    ).lower()
    if mode not in {"off", "shadow", "advisory"}:
        mode = "shadow"
    as_of = assessment.get("evaluated_at") or state.get("updated_at")
    age = _age_hours(as_of)
    eligible = bool(portfolio.get("eligible"))
    effective = mode != "off" and eligible and age is not None and age <= 48
    label = portfolio.get("committed_label") or portfolio.get("raw_label")
    blockers = []
    if not assessment:
        blockers.append("market_regime_assessment_missing")
    elif not eligible:
        blockers.append("market_regime_input_coverage_insufficient")
    if age is None or age > 48:
        blockers.append("market_regime_assessment_stale")
    if mode == "off":
        blockers.append("market_regime_mode_off")
    return _read_only_status(
        key="market_regime_v2",
        label="5段階レジーム",
        category="policy",
        description="トレンド・市場幅・VIX・信用・長期金利から相場状態を判定します。",
        mode=mode,
        configured_enabled=mode != "off",
        effective_enabled=effective,
        reason=(
            f"{label}を実効判定として使用しています"
            if effective else "レジーム判定は停止または安全側に縮退しています"
        ),
        blockers=blockers,
        source="market_regime_v2_state.json + ALMANAC_MARKET_REGIME_V2_MODE",
        source_as_of=as_of,
        max_age_hours=48,
        source_note="advisoryは提案とサイズ上限へ反映しますが自動注文しません",
        control_hint="環境変数と入力カバレッジ・ヒステリシスが権威です",
        metrics=[
            {"label": "判定", "value": label},
            {"label": "score", "value": portfolio.get("score")},
        ],
    )


def _analysis_snapshot_status(root: Path) -> dict[str, Any]:
    state = _load_json(root / "decision_snapshot_state.json", {})
    state = state if isinstance(state, dict) else {}
    latest: dict[str, Any] | None = None
    latest_at: datetime | None = None
    latest_analysis_id: str | None = None
    for analysis_id, stages in state.items():
        if not isinstance(stages, dict):
            continue
        for stage in ("tier", "synthesis"):
            record = stages.get(stage)
            if not isinstance(record, dict):
                continue
            parsed = _parse_time(record.get("frozen_at"))
            if parsed is not None and (latest_at is None or parsed > latest_at):
                latest, latest_at, latest_analysis_id = record, parsed, str(analysis_id)
    as_of = latest.get("frozen_at") if latest else None
    base = (((latest or {}).get("enriched") or {}).get("base") or {})
    freshnesses = [
        row.get("freshness_status")
        for row in base.values()
        if isinstance(row, dict)
    ]
    bad = sum(1 for value in freshnesses if value in {"stale", "missing", "unknown"})
    effective = latest is not None
    warnings = [f"凍結入力に要確認が{bad}件あります"] if bad else []
    return _read_only_status(
        key="analysis_snapshot",
        label="AnalysisSnapshot",
        category="data",
        description="分析開始時の保有・価格・FX・ニュース等を凍結し、後段で同じ入力を使います。",
        mode="frozen_context",
        configured_enabled=True,
        effective_enabled=effective,
        reason=(
            f"最新分析 {latest_analysis_id} の入力を凍結しています"
            if effective else "凍結済み分析入力がありません"
        ),
        blockers=[] if effective else ["decision_snapshot_missing"],
        warnings=warnings,
        source="decision_snapshot_state.json",
        source_as_of=as_of,
        max_age_hours=48,
        source_note="snapshot hashは不変性を示し、各入力のfreshnessは別に判定します",
        control_hint="分析パイプラインが自動生成する監査情報です",
        metrics=[
            {"label": "入力", "value": len(freshnesses)},
            {"label": "要確認", "value": bad},
        ],
    )


def _broker_reconciliation_status(root: Path) -> dict[str, Any]:
    rows = []
    for path in sorted(root.glob("broker_position_snapshot_*.json")):
        payload = _load_json(path, {})
        if isinstance(payload, dict) and payload:
            rows.append((path.name, payload))
    source_times = [
        payload.get("source_as_of") or payload.get("reconciled_at")
        for _, payload in rows
    ]
    parsed_times = [parsed for value in source_times if (parsed := _parse_time(value))]
    oldest = min(parsed_times).isoformat() if parsed_times else None
    complete = sum(1 for _, payload in rows if payload.get("complete") is True)
    valid = bool(rows) and complete == len(rows)
    blockers = []
    if not rows:
        blockers.append("broker_position_snapshots_missing")
    if rows and complete != len(rows):
        blockers.append("broker_position_snapshot_incomplete")
    return _read_only_status(
        key="broker_reconciliation",
        label="証券会社照合",
        category="data",
        description="owner・broker・account・instrument単位の数量と資金の権威を保持します。",
        mode="broker_snapshot",
        configured_enabled=True,
        effective_enabled=valid,
        reason=(
            f"{complete}/{len(rows)}証券会社のsnapshotが完全です。"
            "時間では失効しません。証券会社画面で確認した約定・入出金をWeb登録すれば"
            "権威が前進し、不完全または未登録の後続イベントだけ再照合対象になります"
            if valid else "証券会社snapshotが不足または不完全です"
        ),
        blockers=blockers,
        source="broker_position_snapshot_*.json + execution_reconciliation_state.json",
        source_as_of=oldest,
        max_age_hours=None,
        source_note="確認済み残高はevent-based。時間経過だけでは失効しません",
        control_hint=(
            "初回snapshot後はWebの「証券会社確認済み」実績入力で継続できます。"
            "ALMANAC外の未登録取引がある場合だけCSV等で再照合します"
        ),
        metrics=[
            {"label": "完全", "value": complete},
            {"label": "取得済み", "value": len(rows)},
        ],
    )


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
    as_of = payload.get("as_of") if isinstance(payload, dict) else None
    age = _age_hours(as_of)
    fresh = age is not None and age <= 48
    return {
        "key": key,
        "label": label,
        "category": "shadow",
        "description": description,
        "configured_enabled": mode != "off",
        "effective_enabled": present and mode != "off" and fresh,
        "mutable": False,
        "mode": mode,
        "auto_order_enabled": False,
        "reason": (
            "影実行の結果を記録しています。実アクションや注文は変更しません"
            if present and mode != "off" and fresh
            else "最新分析に影実行結果がなく、実効状態を確認できません"
        ),
        "blockers": (
            [] if present and fresh
            else ["latest_shadow_result_stale"] if present
            else ["latest_shadow_result_missing"]
        ),
        "updated_at": as_of,
        "source": "ai_portfolio_analysis.json",
        "source_as_of": as_of,
        "source_age_hours": round(age, 1) if age is not None else None,
        "freshness_status": _freshness_status(
            exists=present,
            age_hours=age,
            max_age_hours=48,
        ),
        "source_note": "最新分析内の影実行decisionが権威",
        "control_hint": "安全性検証後に別の有効化判断が必要です",
    }


def _ginn_status(root: Path) -> dict[str, Any]:
    disabled = os.environ.get("ALMANAC_DISABLE_GINN", "").strip().lower() in {"1", "true", "yes"}
    pointer = _load_json(root / "models" / "ginn" / "current.json", {})
    version = pointer.get("version") if isinstance(pointer, dict) else None
    active_pointer_version = version
    manifest = root / "models" / "ginn" / str(version) / "manifest.json" if version else None
    manifest_payload = _load_json(manifest, {}) if manifest else {}
    promoted = False
    rejection_reason = "promoted_bundle_missing"
    source_kind = "active" if version else "legacy"
    candidate_manifests = sorted(
        (root / "models" / "ginn").glob("*/manifest.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    latest_candidate_manifest = candidate_manifests[0] if candidate_manifests else None
    if root.resolve() == BASE_DIR.resolve():
        cache_key = str(root.resolve())
        pointer_path = root / "models" / "ginn" / "current.json"
        legacy_manifest_path = root / "models" / "ginn_meta.json"
        signature = tuple(
            path.stat().st_mtime_ns if path is not None and path.exists() else None
            for path in (
                pointer_path,
                legacy_manifest_path,
                latest_candidate_manifest,
            )
        )
        now_mono = time.monotonic()
        cached = _GINN_GATE_CACHE.get(cache_key)
        if (
            isinstance(cached, dict)
            and cached.get("signature") == signature
            and now_mono < float(cached.get("expires_at", 0))
        ):
            version = cached.get("version")
            manifest_payload = dict(cached.get("manifest_payload") or {})
            promoted = bool(cached.get("promoted"))
            rejection_reason = cached.get("rejection_reason")
            source_kind = str(cached.get("source_kind") or source_kind)
        else:
            try:
                from ginn_model import (
                    _load_ginn_meta,
                    _meets_promotion_criteria,
                    _resolve_active_bundle,
                )
                if not active_pointer_version and latest_candidate_manifest is not None:
                    source_kind = "latest_candidate"
                    version = latest_candidate_manifest.parent.name
                    model_path = latest_candidate_manifest.parent / "model.pt"
                    manifest_payload = _load_ginn_meta(
                        latest_candidate_manifest
                    ) or {}
                    if not model_path.exists():
                        rejection_reason = "candidate_model_file_missing"
                    elif not manifest_payload:
                        rejection_reason = "candidate_manifest_missing"
                    else:
                        eligible, gate_reason = _meets_promotion_criteria(
                            manifest_payload
                        )
                        promoted = False
                        rejection_reason = (
                            "candidate_not_promoted"
                            if eligible else gate_reason
                        )
                else:
                    model_path, manifest_path, active_version, pointer_error = _resolve_active_bundle()
                    version = active_version
                    if active_version:
                        source_kind = "active"
                    manifest_payload = _load_ginn_meta(manifest_path) or {}
                    if pointer_error:
                        rejection_reason = pointer_error
                    elif not model_path.exists():
                        rejection_reason = "model_file_missing"
                    elif not manifest_payload:
                        rejection_reason = "manifest_missing"
                    else:
                        promoted, rejection_reason = _meets_promotion_criteria(manifest_payload)
            except Exception:
                rejection_reason = "promotion_gate_unavailable"
            _GINN_GATE_CACHE[cache_key] = {
                "expires_at": now_mono + _STATUS_CACHE_TTL_SECONDS,
                "signature": signature,
                "version": version,
                "manifest_payload": dict(manifest_payload),
                "promoted": promoted,
                "rejection_reason": rejection_reason,
                "source_kind": source_kind,
            }
    else:
        promoted = bool(version and manifest and manifest.exists())
    source_as_of = (
        (manifest_payload.get("data_end") or manifest_payload.get("trained_at"))
        if isinstance(manifest_payload, dict)
        else None
    )
    source_age = _age_hours(source_as_of)
    source_name = {
        "active": "models/ginn/current.json + promoted bundle manifest",
        "latest_candidate": "models/ginn/<latest candidate>/manifest.json",
        "legacy": "models/ginn_meta.json (legacy・default-deny)",
    }.get(source_kind, "models/ginn_meta.json (legacy・default-deny)")
    effective = promoted and not disabled
    if disabled:
        reason = "緊急無効化フラグでGARCH固定です"
    elif not promoted:
        reason = f"GINNは{rejection_reason}のためGARCHへfail-closedしています"
    else:
        reason = "検証・昇格済みbundleを利用できます"
    validation_metrics = (
        manifest_payload.get("validation_metrics")
        if isinstance(manifest_payload, dict)
        else {}
    ) or {}
    validation_mse = validation_metrics.get("mse")
    garch_mse = validation_metrics.get("garch_baseline_mse")
    try:
        garch_ratio = (
            round(float(validation_mse) / float(garch_mse), 2)
            if float(garch_mse) > 0
            else None
        )
    except (TypeError, ValueError):
        garch_ratio = None
    return {
        "key": "ginn",
        "label": "GINNボラティリティ",
        "category": "model",
        "description": (
            "論文上の着想は将来候補として維持しますが、現在の実装候補は"
            "検証ゲートを通ったbundleだけを使用し、不合格ならGJR-GARCHへ縮退します。"
        ),
        "configured_enabled": not disabled,
        "effective_enabled": effective,
        "mutable": False,
        "mode": "promoted_bundle_only",
        "auto_order_enabled": False,
        "roadmap_status": "future_update",
        "roadmap_label": "将来更新",
        "reason": reason,
        "blockers": [] if effective else [
            "disabled_by_env" if disabled else str(rejection_reason)
        ],
        "model_version": version,
        "operating_model": "ginn" if effective else "gjr_garch",
        "operating_model_source": (
            "models/ginn/current.json" if effective else "central_fail_closed_fallback"
        ),
        "audit_candidate_version": (
            version if source_kind == "latest_candidate" else None
        ),
        "source": source_name,
        "source_as_of": source_as_of,
        "source_age_hours": round(source_age, 1) if source_age is not None else None,
        "freshness_status": _freshness_status(
            exists=bool(manifest_payload),
            age_hours=source_age,
            max_age_hours=24 * 10,
        ),
        "source_note": (
            "current.jsonが指す検証・昇格済みbundleだけをロード"
            if source_kind == "active"
            else "最新候補は監査表示のみで、current.jsonへ昇格していません"
            if source_kind == "latest_candidate"
            else "legacy modelは監査用に保持しますが推論には使用しません"
        ),
        "control_hint": "モデル昇格ゲートが権威のため、この画面から強制ONにはできません",
        "metrics": [
            {"label": "validation件数", "value": manifest_payload.get("n_validation")},
            {"label": "validation銘柄数", "value": manifest_payload.get("n_validation_tickers")},
            {"label": "GARCH比MSE", "value": garch_ratio},
            {
                "label": "forward評価",
                "value": (
                    manifest_payload.get("forward_observations")
                    if manifest_payload.get("forward_evaluation_implemented") is True
                    else "未実装"
                ),
            },
        ],
        "detail_sections": [
            {
                "title": "現在の判断",
                "body": (
                    "GINNという考え方を否定した状態ではありません。現在候補の実測が"
                    "GJR-GARCH基準を満たさないため、投資判断にはGJR-GARCHだけを使います。"
                    "上のvalidation値は最新manifestから動的に表示します。forward評価の"
                    "パイプラインは未実装で、予約フィールドの0件を実績とは数えません。"
                ),
            },
            {
                "title": "原論文との差",
                "items": [
                    "現実装は60日窓・2層×64・50 epoch・絶対リターン目標・GARCH項λ=0.3です。",
                    "原論文は90日窓・3層×256・300 epoch・分散を対象にした結合損失を報告しています。",
                    "現実装のVIX/レジームは定数、GARCH σはfold内の単一値で、時系列特徴として未完成です。",
                ],
            },
            {
                "title": "将来の更新条件",
                "items": [
                    "paper-aligned版、または差を明示したGINN-inspired版として学習契約を作り直します。",
                    "look-aheadのないVIX・レジーム履歴とrolling GARCH σ、保存済みscalerを学習・推論で一致させます。",
                    "walk-forward後も未観測期間をshadowで評価し、MSE/QLIKE・tail・peak・3倍clamp頻度をGJR-GARCHと比較します。",
                    "合格後も影響上限付きcanaryから始め、自動注文権限は与えません。",
                ],
            },
        ],
        "references": [
            {
                "label": "GINN原論文（ICAIF 2024 / arXiv）",
                "url": "https://arxiv.org/abs/2410.00288",
            },
        ],
    }


def _auto_tune_status() -> dict[str, Any]:
    try:
        from auto_tune import get_status
        status = get_status()
    except Exception as exc:
        status = {"mode": "unknown", "disabled_reason": str(exc)[:160]}
    mode = str(status.get("mode") or "off")
    enabled = mode != "off"
    as_of = status.get("last_run")
    age = _age_hours(as_of)
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
        "updated_at": as_of,
        "source": "tuning_auto_state.json + auto_tune.get_status()",
        "source_as_of": as_of,
        "source_age_hours": round(age, 1) if age is not None else None,
        "freshness_status": _freshness_status(
            exists=as_of is not None,
            age_hours=age,
            max_age_hours=26,
        ),
        "source_note": "tuning_auto_state.jsonのmodeが実行権威",
        "control_hint": "/tuning のAuto Tune欄で変更できます",
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
    as_of = plan.get("as_of") if isinstance(plan, dict) else None
    age = _age_hours(as_of)
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
        "updated_at": as_of,
        "source": "execution_plan_state.json + execution_plan_gate_mode",
        "source_as_of": as_of,
        "source_age_hours": round(age, 1) if age is not None else None,
        "freshness_status": _freshness_status(
            exists=bool(plan),
            age_hours=age,
            max_age_hours=24 * 35,
        ),
        "source_note": "stateのactive状態とtunable_paramsのgate modeを分離して表示",
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
    if key == "margin_long":
        return _margin_long_status(root)
    if key == "options_signals":
        return _options_status(root)
    if key == "market_regime_v2":
        return _market_regime_status(root)
    if key == "analysis_snapshot":
        return _analysis_snapshot_status(root)
    if key == "broker_reconciliation":
        return _broker_reconciliation_status(root)
    if key == "tax_basis":
        return _tax_basis_status()
    if key == "privacy_mode":
        return _privacy_status()
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
    if key == "currency_policy":
        return _currency_policy_status(root)
    raise KeyError(key)


def list_feature_statuses(
    *,
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    keys = (
        "us_short",
        "jp_short",
        "margin_long",
        "options_signals",
        "market_regime_v2",
        "ginn",
        "analysis_snapshot",
        "broker_reconciliation",
        "tax_basis",
        "privacy_mode",
        "kelly_shadow",
        "fx_hedge_shadow",
        "currency_policy",
        "execution_plan",
        "auto_tune",
    )
    features = []
    for key in keys:
        try:
            features.append(get_feature_status(key, base_dir=base_dir))
        except Exception as exc:
            features.append({
                "key": key,
                "label": key,
                "category": "status_error",
                "description": "運用状態の取得に失敗しました。",
                "configured_enabled": False,
                "effective_enabled": False,
                "mutable": False,
                "mode": "status_resolution_error",
                "auto_order_enabled": False,
                "reason": f"{key}の状態を安全に判定できません",
                "blockers": ["status_resolution_error"],
                "warnings": [],
                "source": "feature_controls.get_feature_status",
                "source_as_of": None,
                "source_age_hours": None,
                "freshness_status": "unknown",
                "source_note": f"error_type={type(exc).__name__}",
                "control_hint": "サーバーログと権威stateを確認してください",
                "metrics": [],
                "status_resolution_failed": True,
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "features": features,
    }

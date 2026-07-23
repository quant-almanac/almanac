"""
GET  /api/scenarios            — シナリオ状態 + プレイブック情報
GET  /api/scenarios/indicators — VIX + テクニカル + 地政学の統合ビュー
POST /api/scenarios/refresh    — シナリオエンジン再評価
"""
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


def _load(name: str, default=None):
    path = BASE_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def build_scenario_summary(state: dict | None) -> dict:
    """scenario_state の一覧表示と Today 用に共通の軽量集計を返す。"""
    source = state if isinstance(state, dict) else {}
    scenarios = source.get("scenarios")
    if isinstance(scenarios, dict):
        rows = [item for item in scenarios.values() if isinstance(item, dict)]
        active = sum(1 for item in rows if item.get("status") == "active")
        partial = sum(1 for item in rows if item.get("status") == "partial")
        watching = sum(1 for item in rows if item.get("status") == "watching")
    else:
        def count(key: str) -> int:
            try:
                return max(0, int(source.get(key) or 0))
            except (TypeError, ValueError):
                return 0
        active = count("active_count")
        partial = count("partial_count")
        watching = count("watching_count")

    return {
        "active": active,
        "partial": partial,
        "watching": watching,
        "alert_level": source.get("overall_alert_level"),
        "evaluated_at": source.get("evaluated_at"),
    }


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _effective_stale_after_hours(
    stale_after_hours: float,
    *,
    weekend_grace_hours: float | None = None,
    now: datetime | None = None,
) -> float:
    """平日 cron source の週末 false stale を避ける。"""
    if weekend_grace_hours is None:
        return stale_after_hours
    local_now = (now or datetime.now(timezone.utc)).astimezone()
    is_weekend = local_now.weekday() in (5, 6)
    is_monday_before_first_run = local_now.weekday() == 0 and local_now.hour < 9
    if is_weekend or is_monday_before_first_run:
        return max(stale_after_hours, weekend_grace_hours)
    return stale_after_hours


def _source_health_at(
    timestamp: str | None,
    *,
    stale_after_hours: float,
    weekend_grace_hours: float | None = None,
    now: datetime | None = None,
    **extra,
) -> dict:
    effective_stale_after = _effective_stale_after_hours(
        stale_after_hours,
        weekend_grace_hours=weekend_grace_hours,
        now=now,
    )
    dt = _parse_datetime(timestamp)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    age_hours = None
    if dt:
        age_hours = round((now_utc - dt).total_seconds() / 3600, 1)
    return {
        "timestamp": timestamp,
        "age_hours": age_hours,
        "stale_after_hours": effective_stale_after,
        "stale": age_hours is None or age_hours > effective_stale_after,
        **extra,
    }


def _build_data_health(
    state: dict | None = None,
    geo: dict | None = None,
    tech: dict | None = None,
    vix: dict | None = None,
    macro: dict | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    state = state or {}
    geo = geo or {}
    tech = tech or {}
    vix = vix or {}
    macro = macro or {}

    health = {
        "scenario": _source_health_at(
            state.get("evaluated_at"),
            stale_after_hours=24,
            weekend_grace_hours=72,
            now=now,
        ),
        "geopolitical": _source_health_at(
            geo.get("cached_at") or geo.get("last_scan"),
            stale_after_hours=8,
            weekend_grace_hours=72,
            now=now,
            news_count=len(geo.get("news_items") or []),
            active_alert_count=len(geo.get("active_alerts") or []),
            keyword_match_count=len(geo.get("keyword_matches") or []),
            assessment_error_count=len(geo.get("assessment_errors") or []),
        ),
        "technical": _source_health_at(
            tech.get("cached_at"),
            stale_after_hours=24,
            weekend_grace_hours=72,
            now=now,
        ),
        "vix": _source_health_at(vix.get("cached_at"), stale_after_hours=12, now=now),
        "macro": _source_health_at(macro.get("cached_at") or macro.get("as_of"), stale_after_hours=48, now=now),
    }
    health["has_stale_sources"] = any(
        isinstance(item, dict) and item.get("stale")
        for item in health.values()
    )
    health["has_collection_warnings"] = (
        health["geopolitical"].get("assessment_error_count", 0) > 0
        or health["geopolitical"].get("news_count", 0) == 0
    )
    return health


def _tail_text(text: str | bytes | None, limit: int = 4000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-limit:]


def _refresh_result_state(returncode: int | None, before: str | None, after: str | None) -> str:
    if returncode != 0:
        return "failed"
    if not after:
        return "warning"
    if before and after and before == after:
        return "warning"
    return "succeeded"


def _write_refresh_status(payload: dict) -> dict:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    atomic_write_json(REFRESH_STATUS_PATH, payload)
    return payload


def _get_refresh_status() -> dict:
    status = _load(REFRESH_STATUS_PATH.name, {})
    if is_locked(_SCENARIO_LOCK):
        status = {
            **status,
            "state": "running",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    return status


def _get_scenarios() -> dict:
    """シナリオ状態にプレイブック情報をマージして返す"""
    state = _load("scenario_state.json", {})
    playbook = _load("scenario_playbook.json", {})
    geo = _load("geopolitical_state.json", {})
    tech = _load("technical_state.json", {})
    vix = _load("vix_state.json", {})
    macro = _load("macro_state.json", {})

    scenarios_state = state.get("scenarios", {})
    raw_pb = playbook.get("scenarios", {})

    # リスト形式の場合は dict に変換
    if isinstance(raw_pb, list):
        scenarios_pb = {item["id"]: item for item in raw_pb if "id" in item}
    else:
        scenarios_pb = raw_pb

    # プレイブックの表示情報をマージ
    for key, pb in scenarios_pb.items():
        if key in scenarios_state:
            scenarios_state[key]["name"] = pb.get("name", key)
            scenarios_state[key]["icon"] = pb.get("icon", "🎯")
            scenarios_state[key]["color"] = pb.get("color", "#7C5CFC")
            scenarios_state[key]["description"] = pb.get("description", "")
            scenarios_state[key]["actions"] = pb.get("actions", {})
            scenarios_state[key]["priority"] = pb.get("priority", "medium")
        else:
            # エンジン未実行 → プレイブックからデフォルト状態を生成
            scenarios_state[key] = {
                "status": "dormant",
                "readiness": 0,
                "signals_met": 0,
                "signals_total": 0,
                "signal_details": [],
                "recommended_actions": {},
                "first_detected": None,
                "last_evaluated": None,
                "name": pb.get("name", key),
                "icon": pb.get("icon", "🎯"),
                "color": pb.get("color", "#7C5CFC"),
                "description": pb.get("description", ""),
                "actions": pb.get("actions", {}),
                "priority": pb.get("priority", "medium"),
            }

    state["scenarios"] = scenarios_state
    summary = build_scenario_summary(state)
    state["active_count"] = summary["active"]
    state["partial_count"] = summary["partial"]
    state["watching_count"] = summary["watching"]
    state["overall_alert_level"] = summary["alert_level"] or "calm"
    state["data_health"] = _build_data_health(state, geo, tech, vix, macro)
    state["refresh_status"] = _get_refresh_status()
    return state


def _get_indicators() -> dict:
    """全インジケーターの統合ビュー"""
    vix = _load("vix_state.json", {})
    tech = _load("technical_state.json", {})
    geo = _load("geopolitical_state.json", {})
    macro = _load("macro_state.json", {})
    state = _load("scenario_state.json", {})

    # vix_state.json はネストされた構造を持つ
    vix_obj = vix.get("vix", {}) if isinstance(vix.get("vix"), dict) else {}
    oil_obj = vix.get("oil", {}) if isinstance(vix.get("oil"), dict) else {}
    yields_obj = vix.get("yields", {}) if isinstance(vix.get("yields"), dict) else {}
    fg_obj = vix.get("fear_greed", {}) if isinstance(vix.get("fear_greed"), dict) else {}
    ts_obj = vix.get("vix_term_structure", {}) if isinstance(vix.get("vix_term_structure"), dict) else {}

    return {
        "data_health": _build_data_health(state, geo, tech, vix, macro),
        "vix": {
            "level": vix_obj.get("level") if vix_obj else vix.get("vix"),
            "classification": vix_obj.get("classification") if vix_obj else vix.get("vix_level"),
            "change_1d": vix_obj.get("change_1d") if vix_obj else vix.get("vix_change_1d"),
            "change_5d": vix_obj.get("change_5d") if vix_obj else vix.get("vix_change_5d"),
            "term_structure": ts_obj.get("structure") if ts_obj else None,
            "oil_price": oil_obj.get("price") if oil_obj else vix.get("oil_price"),
            "oil_change_5d": oil_obj.get("change_5d_pct") if oil_obj else vix.get("oil_change_5d_pct"),
            "yield_spread": yields_obj.get("spread_10y_3m") if yields_obj else vix.get("yield_spread"),
            "fear_greed_score": fg_obj.get("score") if fg_obj else vix.get("fear_greed_score"),
            "fear_greed_label": fg_obj.get("label") if fg_obj else vix.get("fear_greed_label"),
            "sector_flows": {
                k: {
                    "perf_5d": v.get("return_5d_pct", v.get("perf_5d", 0)),
                    "relative_to_spy": v.get("vs_spy_5d_pct", v.get("relative_to_spy", 0)),
                }
                for k, v in (vix.get("sector_flows") or {}).items()
                if isinstance(v, dict)
            },
            "cached_at": vix.get("cached_at"),
        },
        "technical": {
            "market_breadth": tech.get("market_breadth", {}),
            "tickers": tech.get("tickers", {}),
            "cached_at": tech.get("cached_at"),
        },
        "geopolitical": {
            "active_alerts": geo.get("active_alerts", []),
            "keyword_matches": geo.get("keyword_matches", []),
            "assessment_errors": geo.get("assessment_errors", []),
            "last_scan": geo.get("last_scan"),
            "news_summary": geo.get("news_summary"),
        },
        "macro": {
            "fed_rate": macro.get("fed_rate"),
            "yield_10y": macro.get("yield_10y"),
            "cpi_yoy": macro.get("cpi_yoy"),
            "unemp_rate": macro.get("unemp_rate"),
        },
    }


@router.get("/api/scenarios")
async def get_scenarios():
    return await asyncio.to_thread(_get_scenarios)


@router.get("/api/scenarios/indicators")
async def get_indicators():
    return await asyncio.to_thread(_get_indicators)


@router.get("/api/scenarios/refresh/status")
async def get_refresh_status():
    return await asyncio.to_thread(_get_refresh_status)


# P1-15: モジュール内 bool 排他 → file lock に置換（uvicorn reload / 複数プロセス耐性）
from utils import atomic_write_json, process_lock, is_locked, LockBusy  # noqa: E402

_SCENARIO_LOCK = "scenarios_refresh"
REFRESH_STATUS_PATH = BASE_DIR / "scenario_refresh_status.json"


@router.post("/api/scenarios/refresh")
async def refresh_scenarios():
    if is_locked(_SCENARIO_LOCK):
        return {"ok": False, "message": "既に実行中です", "refresh_status": _get_refresh_status()}

    queued_at = datetime.now(timezone.utc).isoformat()
    before_state = _load("scenario_state.json", {})
    before_evaluated_at = before_state.get("evaluated_at")
    _write_refresh_status({
        "state": "queued",
        "queued_at": queued_at,
        "started_at": None,
        "finished_at": None,
        "returncode": None,
        "scenario_evaluated_at_before": before_evaluated_at,
        "scenario_evaluated_at_after": None,
        "state_updated": False,
        "stdout_tail": "",
        "stderr_tail": "",
    })

    async def _run():
        try:
            with process_lock(_SCENARIO_LOCK):
                started_at = datetime.now(timezone.utc).isoformat()
                before = _load("scenario_state.json", {}).get("evaluated_at")
                _write_refresh_status({
                    "state": "running",
                    "queued_at": queued_at,
                    "started_at": started_at,
                    "finished_at": None,
                    "returncode": None,
                    "scenario_evaluated_at_before": before,
                    "scenario_evaluated_at_after": None,
                    "state_updated": False,
                    "stdout_tail": "",
                    "stderr_tail": "",
                })
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(BASE_DIR / "scenario_engine.py"), "--force",
                    cwd=str(BASE_DIR),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                after = _load("scenario_state.json", {}).get("evaluated_at")
                state_updated = bool(after and after != before)
                _write_refresh_status({
                    "state": _refresh_result_state(proc.returncode, before, after),
                    "queued_at": queued_at,
                    "started_at": started_at,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "returncode": proc.returncode,
                    "scenario_evaluated_at_before": before,
                    "scenario_evaluated_at_after": after,
                    "state_updated": state_updated,
                    "stdout_tail": _tail_text(stdout),
                    "stderr_tail": _tail_text(stderr),
                })
        except LockBusy:
            pass  # 別プロセスが先に取得
        except Exception as exc:
            _write_refresh_status({
                "state": "failed",
                "queued_at": queued_at,
                "started_at": None,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "returncode": None,
                "scenario_evaluated_at_before": before_evaluated_at,
                "scenario_evaluated_at_after": _load("scenario_state.json", {}).get("evaluated_at"),
                "state_updated": False,
                "stdout_tail": "",
                "stderr_tail": str(exc),
            })

    asyncio.create_task(_run())
    return {"ok": True, "message": "シナリオ再評価を開始しました", "refresh_status": _get_refresh_status()}

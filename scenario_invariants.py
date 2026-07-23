"""Fail-loud scenario pipeline invariants.

The checks in this module are intentionally pure and small so production jobs,
tests, and data-refresh scripts can share the same scenario/action semantics.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from insider_restrictions import is_restricted_ticker

BASE_DIR = Path(__file__).parent
INCONCLUSIVE_DETAIL = "データ未取得"

OBSERVE_ONLY_PROMOTION_CRITERIA: dict[str, str] = {
    "scenario_monitor": "scenario_promotion_summary.json: >=5 measured 20d catalyst outcome rows, hit_rate>=60%, mean excess return > 0",
    "disclosure_deterministic": ">=20 catalyst outcome rows per feature family, positive forward excess return, parser false-positive review",
    "screener_short": ">=20 catalyst outcome rows, positive short-side excess return after borrow/slippage",
    "screener_short_overheat": ">=20 catalyst outcome rows, positive short-side excess return after borrow/slippage (BULL過熱逆張りレーン)",
    "screener_short_event": ">=20 catalyst outcome rows, positive short-side excess return after borrow/slippage (触媒レーン: dilution/going_concern)",
    "screener_short_bear": ">=20 catalyst outcome rows, positive short-side excess return after borrow/slippage (弱気レジームレーン)",
    "screener_margin_long": ">=20 catalyst outcome rows, positive excess return after margin cost",
    "screener_pair": ">=20 catalyst outcome rows, spread return positive after costs",
    "screener_squeeze": ">=20 catalyst outcome rows, positive 10d excess return after slippage",
}


@dataclass(frozen=True)
class InvariantIssue:
    code: str
    message: str
    context: dict[str, Any]


@dataclass(frozen=True)
class ScenarioAction:
    scenario_id: str
    ticker: str
    action_type: str
    phase: str
    source: str
    observe_only: bool = False
    enabled_for_decision: bool = True
    currency: str | None = None
    allocation: float | None = None


def load_json_file(path: Path | str, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON: {p}: {exc}") from exc


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSONL: {p}:{lineno}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"malformed JSONL: {p}:{lineno}: row is not an object")
        rows.append(row)
    return rows


def _scenario_id(scenario: dict[str, Any], fallback: str = "") -> str:
    return str(scenario.get("id") or scenario.get("scenario_id") or fallback)


def _scenario_enabled(scenario: dict[str, Any]) -> bool:
    if bool(scenario.get("observe_only", False)):
        return False
    return scenario.get("enabled_for_decision", True) is not False


def _infer_action_type(entry: dict[str, Any], default: str) -> str:
    raw = " ".join(
        str(entry.get(k) or "")
        for k in ("action_type", "type", "action", "reason")
    ).lower()
    if any(tok in raw for tok in ("trim", "take_profit", "profit", "利確", "一部売", "削減")):
        return "trim"
    if any(tok in raw for tok in ("sell", "売却", "全売", "撤退")):
        return "sell"
    if any(tok in raw for tok in ("short_sell", "short", "空売")):
        return "short_sell"
    if any(tok in raw for tok in ("margin_buy", "信用買")):
        return "margin_buy"
    return default


def _allocation_and_currency(entry: dict[str, Any]) -> tuple[float | None, str | None]:
    if entry.get("allocation_jpy") is not None:
        return _to_float(entry.get("allocation_jpy")), "JPY"
    if entry.get("allocation_usd") is not None:
        return _to_float(entry.get("allocation_usd")), "USD"
    if entry.get("allocation_amount") is not None:
        return _to_float(entry.get("allocation_amount")), str(entry.get("currency") or "").upper() or None
    return None, str(entry.get("currency") or "").upper() or None


def extract_playbook_action_rows(
    playbook: dict[str, Any],
    *,
    decision_only: bool = False,
    include_sell_triggers: bool = True,
) -> list[ScenarioAction]:
    rows: list[ScenarioAction] = []
    scenarios = playbook.get("scenarios") or []
    if isinstance(scenarios, dict):
        iterable = scenarios.items()
    else:
        iterable = ((_scenario_id(sc), sc) for sc in scenarios if isinstance(sc, dict))

    for sid, scenario in iterable:
        if not isinstance(scenario, dict):
            continue
        sid = _scenario_id(scenario, str(sid))
        enabled = _scenario_enabled(scenario)
        observe_only = bool(scenario.get("observe_only", False))
        if decision_only and not enabled:
            continue
        seen_tickers: set[str] = set()
        actions = scenario.get("actions") or {}
        if not isinstance(actions, dict):
            continue
        for phase, phase_data in actions.items():
            if not isinstance(phase_data, dict):
                continue
            for bucket, default_type in (("buy", "buy"), ("sell", "sell")):
                entries = phase_data.get(bucket) or []
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict) or not entry.get("ticker"):
                        continue
                    ticker = str(entry["ticker"])
                    allocation, currency = _allocation_and_currency(entry)
                    rows.append(
                        ScenarioAction(
                            scenario_id=sid,
                            ticker=ticker,
                            action_type=_infer_action_type(entry, default_type),
                            phase=str(phase),
                            source=bucket,
                            observe_only=observe_only,
                            enabled_for_decision=enabled,
                            currency=currency,
                            allocation=allocation,
                        )
                    )
                    seen_tickers.add(ticker)
        if include_sell_triggers:
            triggers = actions.get("sell_on_trigger") or []
            if isinstance(triggers, list):
                for trigger in triggers:
                    ticker = str(trigger) if trigger else ""
                    if not ticker or ticker in seen_tickers:
                        continue
                    rows.append(
                        ScenarioAction(
                            scenario_id=sid,
                            ticker=ticker,
                            action_type="sell",
                            phase="sell_on_trigger",
                            source="sell_on_trigger",
                            observe_only=observe_only,
                            enabled_for_decision=enabled,
                        )
                    )
                    seen_tickers.add(ticker)
    return rows


def extract_state_action_rows(
    scenario_state: dict[str, Any],
    *,
    statuses: set[str] | None = None,
    include_sell_triggers: bool = True,
) -> list[ScenarioAction]:
    statuses = statuses or {"active"}
    raw = scenario_state.get("scenarios") if isinstance(scenario_state, dict) else None
    if isinstance(raw, dict):
        iterable = raw.items()
    elif isinstance(raw, list):
        iterable = ((_scenario_id(sc), sc) for sc in raw if isinstance(sc, dict))
    else:
        return []

    rows: list[ScenarioAction] = []
    for sid, scenario in iterable:
        if not isinstance(scenario, dict):
            continue
        if str(scenario.get("status") or "") not in statuses:
            continue
        sid = _scenario_id(scenario, str(sid))
        enabled = scenario.get("enabled_for_decision", True) is not False
        observe_only = bool(scenario.get("observe_only", False))
        recommended = scenario.get("recommended_actions") or {}
        if not isinstance(recommended, dict):
            continue
        seen_tickers: set[str] = set()
        for phase, entries in recommended.items():
            if phase == "sell_on_trigger":
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("ticker"):
                    continue
                ticker = str(entry["ticker"])
                allocation, currency = _allocation_and_currency(entry)
                default_type = "sell" if str(phase).startswith("sell") else "buy"
                rows.append(
                    ScenarioAction(
                        scenario_id=sid,
                        ticker=ticker,
                        action_type=_infer_action_type(entry, default_type),
                        phase=str(phase),
                        source="recommended_actions",
                        observe_only=observe_only,
                        enabled_for_decision=enabled,
                        currency=currency,
                        allocation=allocation,
                    )
                )
                seen_tickers.add(ticker)
        if include_sell_triggers:
            triggers = recommended.get("sell_on_trigger") or []
            if isinstance(triggers, list):
                for trigger in triggers:
                    ticker = str(trigger) if trigger else ""
                    if not ticker or ticker in seen_tickers:
                        continue
                    rows.append(
                        ScenarioAction(
                            scenario_id=sid,
                            ticker=ticker,
                            action_type="sell",
                            phase="sell_on_trigger",
                            source="sell_on_trigger",
                            observe_only=observe_only,
                            enabled_for_decision=enabled,
                        )
                    )
                    seen_tickers.add(ticker)
    return rows


def scenario_action_tickers_from_playbook(
    playbook: dict[str, Any],
    *,
    decision_only: bool = False,
) -> list[str]:
    return sorted(
        {
            row.ticker
            for row in extract_playbook_action_rows(playbook, decision_only=decision_only)
        }
    )


def active_scenario_action_tickers(scenario_state: dict[str, Any]) -> list[str]:
    return sorted({row.ticker for row in extract_state_action_rows(scenario_state)})


def check_action_tickers_in_universe(
    playbook: dict[str, Any],
    tickers: dict[str, Any],
) -> list[InvariantIssue]:
    universe = set(tickers.get("all") or [])
    issues: list[InvariantIssue] = []
    for row in extract_playbook_action_rows(playbook, decision_only=True):
        if row.ticker not in universe:
            issues.append(
                InvariantIssue(
                    code="scenario_action_ticker_missing_from_all",
                    message=f"{row.scenario_id} {row.phase} {row.action_type} ticker {row.ticker} is not in tickers.json['all']",
                    context=asdict(row),
                )
            )
    return issues


def check_restricted_tickers_not_in_playbook(playbook: dict[str, Any]) -> list[InvariantIssue]:
    issues: list[InvariantIssue] = []
    for row in extract_playbook_action_rows(playbook, decision_only=False):
        if is_restricted_ticker(row.ticker):
            issues.append(
                InvariantIssue(
                    code="scenario_restricted_ticker_in_playbook",
                    message=f"{row.scenario_id} {row.phase} {row.action_type} uses restricted ticker {row.ticker}",
                    context=asdict(row),
                )
            )
    return issues


def _load_scenario_states(base_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        load_json_file(base_dir / "vix_state.json", {}),
        load_json_file(base_dir / "macro_state.json", {}),
        load_json_file(base_dir / "technical_state.json", {}),
        load_json_file(base_dir / "market_snapshot.json", {}),
        load_json_file(base_dir / "regime_state.json", {}),
    )


def _is_signal_resolved(row: dict[str, Any]) -> bool:
    detail = str(row.get("detail") or "")
    if detail == INCONCLUSIVE_DETAIL or "データなし" in detail or detail.startswith("未対応条件"):
        return False
    if row.get("type") == "indicator" and row.get("value") is None:
        return False
    return True


def scenario_signal_resolvability(
    playbook: dict[str, Any],
    *,
    base_dir: Path = BASE_DIR,
    states: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    import scenario_engine

    vix_state, macro_state, tech_state, market_state, regime_state = states or _load_scenario_states(base_dir)
    rows: list[dict[str, Any]] = []
    for scenario in playbook.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        detect = scenario.get("detect") or {}
        indicator_rows = scenario_engine._eval_indicators(
            scenario, vix_state, macro_state, tech_state, market_state
        )
        technical_rows = scenario_engine._eval_technical(
            scenario, tech_state, market_state, regime_state
        )
        signal_rows = indicator_rows + technical_rows
        min_signals = detect.get("min_signals") or 0
        resolved = [row for row in signal_rows if _is_signal_resolved(row)]
        unresolved = [row for row in signal_rows if not _is_signal_resolved(row)]
        rows.append(
            {
                "scenario_id": _scenario_id(scenario),
                "min_signals": min_signals,
                "resolved_count": len(resolved),
                "unresolved_keys": [str(row.get("key")) for row in unresolved],
                "resolved_keys": [str(row.get("key")) for row in resolved],
            }
        )
    return rows


def check_signal_resolvability(
    playbook: dict[str, Any],
    *,
    base_dir: Path = BASE_DIR,
) -> list[InvariantIssue]:
    issues: list[InvariantIssue] = []
    for row in scenario_signal_resolvability(playbook, base_dir=base_dir):
        try:
            min_signals = int(row.get("min_signals") or 0)
        except (TypeError, ValueError):
            min_signals = 0
        if min_signals > 0 and row["resolved_count"] < min_signals:
            issues.append(
                InvariantIssue(
                    code="scenario_unresolvable_min_signals",
                    message=(
                        f"{row['scenario_id']} has {row['resolved_count']} resolvable signals "
                        f"but min_signals={min_signals}"
                    ),
                    context=row,
                )
            )
    return issues


def _detect_signal_keys(scenario: dict[str, Any]) -> set[str]:
    detect = scenario.get("detect") or {}
    keys: set[str] = set()
    for section in ("indicators", "technical"):
        rows = detect.get(section) or {}
        if isinstance(rows, dict):
            keys.update(str(key) for key in rows.keys())
    if detect.get("news_keywords"):
        keys.add("news_keywords")
    return keys


def check_required_signals_declared_in_detect(playbook: dict[str, Any]) -> list[InvariantIssue]:
    """Fail when required_signals names cannot be emitted by detect sections."""
    issues: list[InvariantIssue] = []
    scenarios = playbook.get("scenarios") or []
    if isinstance(scenarios, dict):
        iterable = scenarios.items()
    else:
        iterable = ((_scenario_id(sc), sc) for sc in scenarios if isinstance(sc, dict))

    for sid, scenario in iterable:
        if not isinstance(scenario, dict):
            continue
        sid = _scenario_id(scenario, str(sid))
        detect = scenario.get("detect") or {}
        required = detect.get("required_signals") or detect.get("required_signal_keys") or []
        if not isinstance(required, list):
            continue
        available = _detect_signal_keys(scenario)
        missing = sorted(str(key) for key in required if str(key) not in available)
        if missing:
            issues.append(
                InvariantIssue(
                    code="scenario_required_signal_missing_from_detect",
                    message=(
                        f"{sid} required_signals contains keys that are not emitted by detect: "
                        + ", ".join(missing)
                    ),
                    context={
                        "scenario_id": sid,
                        "required_signals": [str(key) for key in required],
                        "detect_signal_keys": sorted(available),
                        "missing_required_signals": missing,
                    },
                )
            )
    return issues


def check_observe_only_measurement_lanes(lane_registry: dict[str, Any]) -> list[InvariantIssue]:
    lanes = {
        str(lane.get("name")): lane
        for lane in lane_registry.get("lanes", [])
        if isinstance(lane, dict)
    }
    issues: list[InvariantIssue] = []
    for lane_name, criteria in OBSERVE_ONLY_PROMOTION_CRITERIA.items():
        lane = lanes.get(lane_name)
        if not lane:
            issues.append(
                InvariantIssue(
                    code="observe_only_lane_missing",
                    message=f"observe-only lane {lane_name} is missing from lane_registry.json",
                    context={"lane": lane_name, "promotion_criteria": criteria},
                )
            )
            continue
        if lane.get("status") != "measured" or not lane.get("measurement_path"):
            issues.append(
                InvariantIssue(
                    code="observe_only_lane_unmeasured",
                    message=f"observe-only lane {lane_name} lacks measured status or measurement_path",
                    context={"lane": lane_name, "lane_registry": lane, "promotion_criteria": criteria},
                )
            )
        if lane_name == "scenario_monitor" and not lane.get("promotion_path"):
            issues.append(
                InvariantIssue(
                    code="observe_only_lane_missing_promotion_path",
                    message="scenario_monitor lacks scenario-level promotion artifact path",
                    context={"lane": lane_name, "lane_registry": lane, "promotion_criteria": criteria},
                )
            )
    return issues


def _recommendation_type(row: dict[str, Any]) -> str | None:
    htype = str(row.get("hypothesis_type") or "")
    source = " ".join(str(x) for x in row.get("source_agents") or [])
    source += " " + str(row.get("primary_source_agent") or "")
    source_event = str(row.get("source_event_id") or "")
    haystack = " ".join((htype, source, source_event)).lower()
    if htype == "disclosure_catalyst" or "disclosure" in haystack:
        return "disclosure"
    if htype.startswith("scenario_") or htype == "bull_pullback" or "scenario:" in haystack:
        return "scenario"
    if htype.startswith("screener_") or "screener:" in haystack:
        return "screening"
    if "dca" in haystack:
        return "dca"
    return None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _default_horizon_days(row: dict[str, Any]) -> int:
    htype = str(row.get("hypothesis_type") or "")
    if htype.startswith("screener_margin_long"):
        return 20
    if htype.startswith("screener_"):
        return 10
    if htype.startswith("scenario_") or htype == "bull_pullback":
        return 20
    if htype == "disclosure_catalyst":
        return 20
    if "dca" in str(row.get("source_event_id") or "").lower():
        return 20
    return 0


def _is_outcome_due(row: dict[str, Any], *, as_of: date) -> bool:
    event_date = _parse_date(row.get("event_at") or row.get("analysis_date"))
    try:
        horizon_days = int(
            row.get("horizon_days")
            or row.get("time_horizon_days")
            or _default_horizon_days(row)
            or 0
        )
    except (TypeError, ValueError):
        horizon_days = 0
    if event_date is None or horizon_days <= 0:
        return True
    return event_date + timedelta(days=horizon_days) <= as_of


def check_outcome_log_coverage(
    hypothesis_rows: Iterable[dict[str, Any]],
    outcome_rows: Iterable[dict[str, Any]],
    *,
    required_types: set[str] | None = None,
    as_of: date | None = None,
) -> list[InvariantIssue]:
    required_types = required_types or {"scenario", "disclosure", "dca", "screening"}
    as_of = as_of or date.today()
    generated: dict[str, dict[str, dict[str, Any]]] = {typ: {} for typ in required_types}
    outcomes = {str(row.get("hypothesis_id")) for row in outcome_rows if row.get("hypothesis_id")}
    for row in hypothesis_rows:
        if row.get("event_type") not in (None, "generated"):
            continue
        typ = _recommendation_type(row)
        hid = str(row.get("hypothesis_id") or "")
        if typ in generated and hid:
            generated[typ][hid] = row

    issues: list[InvariantIssue] = []
    for typ, rows_by_id in generated.items():
        due_ids = {
            hid
            for hid, row in rows_by_id.items()
            if _is_outcome_due(row, as_of=as_of)
        }
        missing_due = due_ids - outcomes
        if missing_due:
            issues.append(
                InvariantIssue(
                    code="recommendation_type_missing_outcome_rows",
                    message=f"{typ} due generated hypotheses have no outcome rows",
                    context={
                        "recommendation_type": typ,
                        "due_count": len(due_ids),
                        "missing_due_count": len(missing_due),
                    },
                )
            )
    return issues


def _is_jp_ticker(ticker: str) -> bool:
    return ticker.endswith(".T") or (ticker.isdigit() and len(ticker) == 4)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_currency_constraints(playbook: dict[str, Any]) -> list[InvariantIssue]:
    issues: list[InvariantIssue] = []
    for row in extract_playbook_action_rows(playbook, decision_only=True):
        if row.currency:
            expected = "JPY" if _is_jp_ticker(row.ticker) else "USD"
            if row.currency != expected:
                issues.append(
                    InvariantIssue(
                        code="scenario_action_currency_mismatch",
                        message=f"{row.scenario_id} {row.ticker} uses {row.currency}, expected {expected}",
                        context=asdict(row),
                    )
                )
        if _is_jp_ticker(row.ticker) and row.allocation is not None and row.allocation < 50_000:
            issues.append(
                InvariantIssue(
                    code="scenario_jp_allocation_too_small",
                    message=f"{row.scenario_id} {row.ticker} JPY allocation is below a practical 100-share lot",
                    context=asdict(row),
                )
            )
    return issues


def check_shadow_book_retired(*, base_dir: Path = BASE_DIR) -> list[InvariantIssue]:
    issues: list[InvariantIssue] = []
    for rel in ("scenario_shadow_book.py", "tests/test_scenario_shadow_book.py"):
        path = base_dir / rel
        if path.exists():
            issues.append(
                InvariantIssue(
                    code="scenario_shadow_book_not_retired",
                    message=f"{rel} still exists after catalyst measurement consolidation",
                    context={"path": str(path)},
                )
            )
    crontab_path = base_dir / "crontab.proposed"
    if crontab_path.exists() and "scenario_shadow_book.py" in crontab_path.read_text(encoding="utf-8"):
        issues.append(
            InvariantIssue(
                code="scenario_shadow_book_cron_not_retired",
                message="crontab.proposed still references scenario_shadow_book.py",
                context={"path": str(crontab_path)},
            )
        )
    return issues


def run_invariants(*, base_dir: Path = BASE_DIR) -> dict[str, Any]:
    playbook = load_json_file(base_dir / "scenario_playbook.json", {})
    tickers = load_json_file(base_dir / "tickers.json", {})
    lane_registry = load_json_file(base_dir / "lane_registry.json", {})
    hypothesis_rows = read_jsonl(base_dir / "catalyst_hypothesis_log.jsonl")
    outcome_rows = read_jsonl(base_dir / "catalyst_outcome_log.jsonl")

    issues: list[InvariantIssue] = []
    issues.extend(check_action_tickers_in_universe(playbook, tickers))
    issues.extend(check_restricted_tickers_not_in_playbook(playbook))
    issues.extend(check_observe_only_measurement_lanes(lane_registry))
    issues.extend(check_required_signals_declared_in_detect(playbook))
    issues.extend(check_signal_resolvability(playbook, base_dir=base_dir))
    issues.extend(check_outcome_log_coverage(hypothesis_rows, outcome_rows))
    issues.extend(check_currency_constraints(playbook))
    issues.extend(check_shadow_book_retired(base_dir=base_dir))
    return {
        "ok": not issues,
        "issues": [asdict(issue) for issue in issues],
        "signal_resolvability": scenario_signal_resolvability(playbook, base_dir=base_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check scenario pipeline invariants")
    parser.add_argument("--base-dir", type=Path, default=BASE_DIR)
    args = parser.parse_args(argv)
    result = run_invariants(base_dir=args.base_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

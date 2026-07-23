"""Convert deterministic screener outputs into observe-only candidate packets."""

from __future__ import annotations

from typing import Any, Mapping

from .ids import compute_hypothesis_id
from .status import CandidateStatus

RULE_VERSION = "v1"
DEFAULT_TOP_N_PER_LANE = 3

LANE_CONFIG = {
    "short": {"action_type": "short_sell", "horizon_days": 10},
    "margin_long": {"action_type": "margin_buy", "horizon_days": 20},
    "pair": {"action_type": "pair", "horizon_days": 10},
    "squeeze": {"action_type": "buy", "horizon_days": 10},
}


def _score(row: Mapping[str, Any]) -> int:
    raw = (
        row.get("composite_score")
        or row.get("score")
        or abs(float(row.get("z_score") or 0.0)) * 20
        or float(row.get("short_pct_of_float") or 0.0) * 100
        or 50
    )
    try:
        return max(1, min(99, int(round(float(raw)))))
    except (TypeError, ValueError):
        return 50


_SHORT_SUB_LANES = ("overheat", "event", "bear")


def _short_lane_label(row: Mapping[str, Any]) -> str:
    """short 候補の 3レーン(overheat/event/bear)を lane ラベルに反映。

    candidate['lane'] が無い/未知なら後方互換で 'short' にフォールバック。
    """
    sub = str(row.get("lane") or "").strip().lower()
    return f"short_{sub}" if sub in _SHORT_SUB_LANES else "short"


def _packet(
    *,
    producer_lane: str,
    lane_label: str,
    ticker: str,
    action_type: str,
    horizon_days: int,
    event_key: str,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    hypothesis_type = f"screener_{lane_label}"
    source_event_id = f"screener:{lane_label}:{event_key}:{RULE_VERSION}"
    reason = (
        row.get("rationale")
        or row.get("reason")
        or f"{lane_label} screener candidate"
    )
    risk_controls = dict(row.get("risk_controls") or {})
    constraints = list(row.get("constraints") or [])
    risk_flags = list(row.get("risk_flags") or [])
    if producer_lane == "short":
        risk_controls.setdefault("observe_only_first", True)
        risk_controls.setdefault("human_execution_only", True)
        risk_controls.setdefault("requires_borrow_cost_check", True)
        risk_controls.setdefault("requires_squeeze_guard", True)
        risk_controls.setdefault("size_cap_pct_nav", 0.01)
        risk_controls.setdefault("stop_loss", "hard stop required before manual entry")
        # 3レーン分離: outcome/certify をレーン別に集計できるよう sub-lane を記録
        sub = lane_label.split("short_", 1)[1] if lane_label.startswith("short_") else None
        risk_controls["short_lane"] = sub
        constraints.extend([
            "observe_only_first",
            "human_execution_only",
            "borrow_cost_check_required",
            "squeeze_guard_required",
            "position_size_cap_required",
            "hard_stop_required",
        ])
        if row.get("squeeze_risk"):
            risk_flags.append(f"squeeze_risk:{row.get('squeeze_risk')}")
    return {
        "hypothesis_id": compute_hypothesis_id(
            ticker,
            action_type,
            hypothesis_type,
            horizon_days,
            source_event_id,
        ),
        "ticker": ticker,
        "action_type": action_type,
        "hypothesis_type": hypothesis_type,
        "time_horizon_days": horizon_days,
        "source_event_id": source_event_id,
        "source_agents": [f"screener:{lane_label}"],
        "confidence_pct": _score(row),
        "evidence_summary": str(reason)[:500],
        "invalidation_summary": "Signal no longer satisfies the originating screener rule.",
        "candidate_status": CandidateStatus.generated.value,
        "observe_only": True,
        "human_execution_only": True,
        "risk_controls": risk_controls,
        "constraints": constraints,
        "risk_flags": risk_flags,
        "execution_cost_model": dict(row.get("execution_cost_model") or {}),
        "tradeability": dict(row.get("tradeability") or {}),
    }


def extract_screener_packets(
    payloads: Mapping[str, Mapping[str, Any]] | None,
    *,
    analysis_date: str,
    top_n_per_lane: int = DEFAULT_TOP_N_PER_LANE,
) -> list[dict[str, Any]]:
    """Return at most ``top_n_per_lane`` screener candidates per producer lane."""
    packets: list[dict[str, Any]] = []
    for lane, config in LANE_CONFIG.items():
        payload = (payloads or {}).get(lane) or {}
        rows = payload.get("candidates") or payload.get("picks") or []
        if not isinstance(rows, list):
            continue
        for row in rows[: max(0, top_n_per_lane)]:
            if not isinstance(row, Mapping):
                continue
            if lane == "pair":
                pair = str(row.get("pair") or "")
                for leg, action_type in (
                    ("long", "buy"),
                    ("short", "short_sell"),
                ):
                    ticker = str(row.get(leg) or "").upper()
                    if ticker:
                        packets.append(
                            _packet(
                                producer_lane=lane,
                                lane_label=lane,
                                ticker=ticker,
                                action_type=action_type,
                                horizon_days=int(config["horizon_days"]),
                                event_key=f"{analysis_date}:{pair}:{leg}",
                                row=row,
                            )
                        )
                continue
            ticker = str(row.get("ticker") or "").upper()
            if not ticker:
                continue
            # short 生産レーンは候補の lane(overheat/event/bear)を反映して計測分離
            lane_label = _short_lane_label(row) if lane == "short" else lane
            packets.append(
                _packet(
                    producer_lane=lane,
                    lane_label=lane_label,
                    ticker=ticker,
                    action_type=str(config["action_type"]),
                    horizon_days=int(config["horizon_days"]),
                    event_key=f"{analysis_date}:{ticker}",
                    row=row,
                )
            )
    return packets

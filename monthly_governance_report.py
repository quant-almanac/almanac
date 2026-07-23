"""monthly_governance_report.py — レーン横断の昇格/維持/廃止ドラフトレポート。

既存の計測データ（agent_reliability.json / scenario_promotion_summary.json /
action_state.json の loss_harvest_sell エントリ）を横断集計し、各レーンについて
promote(昇格) / maintain(維持) / retire(廃止) / insufficient_data(判定保留)
のドラフト判定を出す。自動アクションは取らない — 人間レビュー前提の月次ドラフト。

対応済みレーン: analyst agents (agent_reliability.json) / scenario (scenario_promotion_summary.json)
                / tax_harvest (action_state.json 内 loss_harvest_sell エントリの実行率)
                / leveraged_decay (leveraged_decay_signals.json — 現状ポジション0件のため判定保留)
                / red_team (red_team_ledger.py の verdict save-rate)
                / disclosure_features (disclosure_feature_promotion.py — disclosure_type別のhit率/超過リターン)
                / swing_lane (swing_lane_kpi.py — 勝率/profit factor/平均保有日数/期待値/最大単発損失)
                / jp_event_drift (data/disclosure_shadow_book.json — live化ゲート判定, 設計: docs/design_jp_event_drift_2026_07.md)
                / screener_lane (data/screener_shadow_book.json — momentum screener戦略別の効き目, 設計: docs/design_screener_shadow_2026_07.md)
未対応レーン（構造化計測データが無くこのレポートでは判定不能、instrumentation 未整備と明記）:
                short_universe / screener系のうち long_term・margin_long・news
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).parent
AGENT_RELIABILITY_PATH = BASE_DIR / "agent_reliability.json"
SCENARIO_PROMOTION_PATH = BASE_DIR / "scenario_promotion_summary.json"
ACTION_STATE_PATH = BASE_DIR / "action_state.json"
LEVERAGED_DECAY_PATH = BASE_DIR / "leveraged_decay_signals.json"
DISCLOSURE_SHADOW_BOOK_PATH = BASE_DIR / "data" / "disclosure_shadow_book.json"
SCREENER_SHADOW_BOOK_PATH = BASE_DIR / "data" / "screener_shadow_book.json"
DEFAULT_REPORT_DIR = BASE_DIR / "reports"

# レーン共通の昇格しきい値（scenario_promotion_summary.json の既存基準を流用し一貫させる）
MIN_MEASURED_N = 5
MIN_WIN_RATE = 0.6
MIN_MEAN_EXCESS_RETURN_BPS = 0.0

# レバレッジ商品「解除トリガー」(攻めバックログ 2026-07 項目6): 現状ポジション0件のため
# 監視のみ。将来ポジションを持った場合に、この件数×方向一致率で判定する。
LEVERAGED_DECAY_MIN_HOLD_DAYS_OBSERVED = 60
LEVERAGED_DECAY_MIN_DIRECTION_MATCH_RATE = 0.6

# JPイベントドリフト live化ゲート (攻めバックログ項目4 / docs/design_jp_event_drift_2026_07.md
# Phase A)。判定は JP銘柄のみの成績で行う — シャドーブックはUS銘柄が82/83を占めるため、
# 全体成績でJPレーンを昇格させると根拠のない live化 になる。
JP_EVENT_DRIFT_HORIZONS = (5, 20)   # バックログの「5-20営業日」
JP_EVENT_DRIFT_MIN_N_PROMOTE = 30
JP_EVENT_DRIFT_MIN_HIT_RATE = 0.55
JP_EVENT_DRIFT_MIN_MEAN_NET = 0.01  # after-cost +1.0%

# スクリーナー戦略別の効き目判定 (攻めバックログ項目5後半 / docs/design_screener_shadow_2026_07.md)。
# screener候補のobserve_onlyフォワードリターンを戦略別に判定。20営業日を主 horizon にする。
SCREENER_PRIMARY_HORIZON = 20
SCREENER_MIN_N_PROMOTE = 30
SCREENER_MIN_HIT_RATE = 0.55
SCREENER_MIN_MEAN_NET = 0.0     # after-cost がプラスであること
SCREENER_MIN_N_RETIRE = 50
SCREENER_MAX_MEAN_NET_RETIRE = 0.0
JP_EVENT_DRIFT_MIN_N_RETIRE = 50

LANES_WITHOUT_INSTRUMENTATION = [
    "short_universe",
    "screener (long_term/margin_long/news)",
]


def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _verdict_from_metrics(*, measured_n: int, win_rate: Optional[float],
                           mean_excess_return_bps: Optional[float]) -> tuple[str, str]:
    """(verdict, reason) を返す。verdict: promote/maintain/retire/insufficient_data"""
    if measured_n < MIN_MEASURED_N:
        return "insufficient_data", f"measured_n={measured_n} < {MIN_MEASURED_N}"
    if win_rate is None or mean_excess_return_bps is None:
        return "insufficient_data", "win_rate/mean_excess_return_bps が未計測"
    if win_rate >= MIN_WIN_RATE and mean_excess_return_bps >= MIN_MEAN_EXCESS_RETURN_BPS:
        return "promote", f"win_rate={win_rate:.2f} >= {MIN_WIN_RATE}, excess={mean_excess_return_bps:.1f}bps >= 0"
    if win_rate < 0.45:
        return "retire", f"win_rate={win_rate:.2f} < 0.45（コイン投げ以下）"
    return "maintain", f"win_rate={win_rate:.2f}（昇格・廃止いずれの基準も未達）"


def _analyst_agents_section(data: Optional[dict]) -> dict:
    if data is None:
        return {"available": False, "reason": "agent_reliability.json が見つかりません"}
    rows = []
    for agent, groups in (data.get("agents") or {}).items():
        if not isinstance(groups, dict):
            continue
        for group_name, metrics in groups.items():
            if not isinstance(metrics, dict):
                continue
            measured_n = int(metrics.get("measured_n") or 0)
            verdict, reason = _verdict_from_metrics(
                measured_n=measured_n,
                win_rate=metrics.get("win_rate"),
                mean_excess_return_bps=metrics.get("mean_excess_return_bps"),
            )
            rows.append({
                "agent": agent,
                "group": group_name,
                "n": metrics.get("n"),
                "measured_n": measured_n,
                "win_rate": metrics.get("win_rate"),
                "mean_excess_return_bps": metrics.get("mean_excess_return_bps"),
                "verdict": verdict,
                "reason": reason,
            })
    return {"available": True, "as_of": data.get("as_of"), "rows": rows}


def _scenario_section(data: Optional[dict]) -> dict:
    if data is None:
        return {"available": False, "reason": "scenario_promotion_summary.json が見つかりません"}
    rows = []
    for scenario_id, s in (data.get("by_scenario") or {}).items():
        measured = int(s.get("measured_episodes") or 0)
        if measured < MIN_MEASURED_N:
            verdict, reason = "insufficient_data", f"measured_episodes={measured} < {MIN_MEASURED_N}"
        elif s.get("promotion_ready"):
            verdict, reason = "promote", "promotion_ready=true（既存判定を継承）"
        elif s.get("hit_rate") is not None and s["hit_rate"] < 0.45:
            verdict, reason = "retire", f"hit_rate={s['hit_rate']:.2f} < 0.45"
        else:
            verdict, reason = "maintain", "observe_only を継続"
        rows.append({
            "scenario_id": scenario_id,
            "measured_episodes": measured,
            "hit_rate": s.get("hit_rate"),
            "mean_excess_return_bps": s.get("mean_excess_return_bps"),
            "auto_decision_stage": s.get("auto_decision_stage"),
            "verdict": verdict,
            "reason": reason,
        })
    return {"available": True, "as_of": data.get("as_of"), "rows": rows}


def _tax_harvest_section(action_state: Optional[dict]) -> dict:
    if action_state is None:
        return {"available": False, "reason": "action_state.json が見つかりません"}
    entries = [
        a for a in (action_state.get("actions") or {}).values()
        if isinstance(a, dict) and a.get("action_type") == "loss_harvest_sell"
    ]
    if not entries:
        return {
            "available": True,
            "n_total": 0,
            "verdict": "insufficient_data",
            "reason": "loss_harvest_sell の recommendation がまだ0件（2026-07-12統合直後のため今後蓄積）",
        }
    n_total = len(entries)
    n_filled = sum(1 for e in entries if e.get("status") == "filled")
    n_cancelled = sum(1 for e in entries if e.get("status") == "cancelled")
    n_pending = sum(1 for e in entries if e.get("status") == "pending")
    n_resolved = n_filled + n_cancelled
    execution_rate = (n_filled / n_resolved) if n_resolved > 0 else None
    if n_resolved < MIN_MEASURED_N:
        verdict, reason = "insufficient_data", f"resolved={n_resolved} < {MIN_MEASURED_N}"
    elif execution_rate is not None and execution_rate < 0.2:
        verdict, reason = "retire", f"execution_rate={execution_rate:.2f} < 0.2（候補が提示されても実行されていない）"
    else:
        verdict, reason = "maintain", f"execution_rate={execution_rate:.2f}" if execution_rate is not None else "resolved件数不足"
    return {
        "available": True,
        "n_total": n_total,
        "n_pending": n_pending,
        "n_filled": n_filled,
        "n_cancelled": n_cancelled,
        "execution_rate": round(execution_rate, 3) if execution_rate is not None else None,
        "verdict": verdict,
        "reason": reason,
    }


def _leveraged_decay_section(data: Optional[dict]) -> dict:
    """攻めバックログ項目6: レバレッジ専用エンジンの解除トリガー監視。

    現状ポジション0件のため判定不能。将来ポジションを持った場合、
    flagged 判定と実測方向の一致率を60営業日・60%基準で評価する
    (leveraged_decay_monitor.py 自体は decay を都度直接計算するため、
    ここでは「継続監視に足る件数が溜まっているか」だけを見る)。
    """
    if data is None:
        return {"available": False, "reason": "leveraged_decay_signals.json が見つかりません"}
    positions_total = int(data.get("positions_total") or 0)
    if positions_total == 0:
        return {
            "available": True,
            "positions_total": 0,
            "verdict": "insufficient_data",
            "reason": "現状レバレッジ保有0件（保有判断が先、エンジン先行開発はしない方針）",
        }
    positions_flagged = int(data.get("positions_flagged") or 0)
    return {
        "available": True,
        "positions_total": positions_total,
        "positions_flagged": positions_flagged,
        "verdict": "insufficient_data",
        "reason": (
            f"{positions_total}件保有中だが解除トリガー判定には"
            f"{LEVERAGED_DECAY_MIN_HOLD_DAYS_OBSERVED}営業日の追跡と"
            f"方向一致率{LEVERAGED_DECAY_MIN_DIRECTION_MATCH_RATE:.0%}基準の集計が別途必要"
        ),
    }


def _red_team_section() -> dict:
    """攻めバックログ項目2: RedTeamのreject判断がsave-rate的に的確かを判定する。

    save-rate = rejectしたのに実際に下落した割合 (高いほど的確に止めている)。
    save-rate >= 55% (n>=20) -> promote (自動反映重み拡大の検討材料)
    save-rate < 45% (n>=30) -> retire (役割簡素化を検討)
    """
    try:
        from red_team_ledger import aggregate_save_rate
    except Exception as e:
        return {"available": False, "reason": f"red_team_ledger 読み込み失敗: {e}"}

    stats = aggregate_save_rate()
    n = stats["n_reject_measured"]
    save_rate = stats["save_rate"]

    if n < MIN_MEASURED_N:
        verdict, reason = "insufficient_data", f"n_reject_measured={n} < {MIN_MEASURED_N}"
    elif n >= 20 and save_rate is not None and save_rate >= 0.55:
        verdict, reason = "promote", f"save_rate={save_rate:.2f} >= 0.55 (n={n})"
    elif n >= 30 and save_rate is not None and save_rate < 0.45:
        verdict, reason = "retire", f"save_rate={save_rate:.2f} < 0.45 (n={n})"
    else:
        verdict, reason = "maintain", f"save_rate={save_rate}" if save_rate is not None else "計測不足"

    return {"available": True, "verdict": verdict, "reason": reason, **stats}


def _disclosure_feature_section() -> dict:
    """攻めバックログ項目3: 開示特徴量タイプ別の昇格/維持/廃止ドラフト判定。"""
    try:
        from disclosure_feature_promotion import aggregate_by_disclosure_type, promotion_verdicts
    except Exception as e:
        return {"available": False, "reason": f"disclosure_feature_promotion 読み込み失敗: {e}"}

    try:
        agg = aggregate_by_disclosure_type()
        verdicts = promotion_verdicts(agg)
    except Exception as e:
        return {"available": False, "reason": f"集計失敗: {e}"}

    rows = [
        {"disclosure_type": dtype, **stats}
        for dtype, stats in sorted(verdicts.items())
    ]
    return {"available": True, "rows": rows}


def _jp_event_drift_section(data: Optional[dict]) -> dict:
    """攻めバックログ項目4 Phase A: 開示シャドーブックの live化ゲート判定。

    disclosure_shadow_book.py (平日17:10 LaunchAgent) の観測結果を horizon 別に集計し、
    JP銘柄のみの成績でバックログ基準 (n>=30, hit>=55%, after-cost平均>=+1.0%) を判定する。
    """
    if data is None:
        return {"available": False, "reason": "data/disclosure_shadow_book.json が見つかりません"}

    trades = [
        t for t in (data.get("trades") or [])
        if isinstance(t, dict) and not t.get("untradeable") and t.get("net_return") is not None
    ]
    rows = []
    jp_best: Optional[dict] = None
    for horizon in JP_EVENT_DRIFT_HORIZONS:
        hs = [t for t in trades if t.get("horizon_days") == horizon]
        for scope, subset in (("all", hs), ("JP", [t for t in hs if t.get("market") == "JP"])):
            n = len(subset)
            hit_rate = (sum(1 for t in subset if t["net_return"] > 0) / n) if n else None
            mean_net = (sum(t["net_return"] for t in subset) / n) if n else None
            row = {
                "horizon_days": horizon,
                "scope": scope,
                "n": n,
                "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
                "mean_net_return": round(mean_net, 6) if mean_net is not None else None,
            }
            rows.append(row)
            if scope == "JP" and (jp_best is None or n > jp_best["n"]):
                jp_best = row

    jp_n = jp_best["n"] if jp_best else 0
    if jp_best and jp_n >= JP_EVENT_DRIFT_MIN_N_PROMOTE \
            and (jp_best["hit_rate"] or 0) >= JP_EVENT_DRIFT_MIN_HIT_RATE \
            and (jp_best["mean_net_return"] or 0) >= JP_EVENT_DRIFT_MIN_MEAN_NET:
        verdict = "promote"
        reason = (
            f"JP n={jp_n}, hit={jp_best['hit_rate']:.0%}, "
            f"net={jp_best['mean_net_return']:+.2%} — live化検討条件成立 (人間承認+¥250k/イベント上限が別途必要)"
        )
    elif jp_best and jp_n >= JP_EVENT_DRIFT_MIN_N_RETIRE and (jp_best["mean_net_return"] or 0) <= 0:
        verdict = "retire"
        reason = f"JP n={jp_n} で after-cost平均 {jp_best['mean_net_return']:+.2%} <= 0"
    else:
        verdict = "insufficient_data"
        reason = (
            f"JP n={jp_n} < {JP_EVENT_DRIFT_MIN_N_PROMOTE}"
            "（JPシグナル枯渇 — TDnet body=タイトルのみが根因、Phase B参照）"
            if jp_n < JP_EVENT_DRIFT_MIN_N_PROMOTE
            else f"JP hit/net が基準未達 (hit={jp_best['hit_rate']}, net={jp_best['mean_net_return']})"
        )

    return {
        "available": True,
        "generated_at": data.get("generated_at"),
        "trade_count_total": len(trades),
        "rows": rows,
        "verdict": verdict,
        "reason": reason,
    }


def _swing_lane_section() -> dict:
    """攻めバックログ項目5(前半): Swingレーンのサイズ昇格ラダー判定材料。

    swing_lane_kpi.compute_swing_kpis() は独自の verdict (promote/maintain/demote/
    insufficient_data) を持つため、ここでは横流しするだけで閾値ロジックは持たない。
    """
    try:
        from swing_lane_kpi import compute_swing_kpis
    except Exception as e:
        return {"available": False, "reason": f"swing_lane_kpi 読み込み失敗: {e}"}

    try:
        stats = compute_swing_kpis()
    except Exception as e:
        return {"available": False, "reason": f"集計失敗: {e}"}

    return {"available": True, **stats}


def _screener_lane_section(book: Optional[dict]) -> dict:
    """攻めバックログ項目5後半: screener戦略別のobserve_only効き目判定。

    screener_shadow_book.py の観測結果を戦略(モメンタム/逆張り/ギャップダウン等)別に
    集計し、20営業日 after-cost成績で promote/maintain/retire を判定する。
    手動タグ付けに依存せず、screener の発見の質そのものを測る。
    """
    if book is None:
        return {"available": False, "reason": "data/screener_shadow_book.json が見つかりません（未計測）"}
    try:
        from screener_shadow_book import aggregate_by_strategy
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"screener_shadow_book 読み込み失敗: {e}"}

    agg = aggregate_by_strategy(book)
    h = str(SCREENER_PRIMARY_HORIZON)
    rows = []
    for strategy, by_h in sorted(agg.items()):
        stats = by_h.get(h) or {}
        n = int(stats.get("n") or 0)
        hit_rate = stats.get("hit_rate")
        mean_net = stats.get("mean_net_return")
        if n < SCREENER_MIN_N_PROMOTE:
            verdict, reason = "insufficient_data", f"n={n} < {SCREENER_MIN_N_PROMOTE}（計測蓄積中）"
        elif (hit_rate or 0) >= SCREENER_MIN_HIT_RATE and (mean_net or 0) > SCREENER_MIN_MEAN_NET:
            verdict = "promote"
            reason = f"hit={hit_rate:.0%}, net={mean_net:+.2%} — この戦略にサイズを寄せる材料"
        elif n >= SCREENER_MIN_N_RETIRE and (mean_net or 0) <= SCREENER_MAX_MEAN_NET_RETIRE:
            verdict = "retire"
            reason = f"n={n} で after-cost平均 {mean_net:+.2%} <= 0 — screener から外す検討"
        else:
            verdict = "maintain"
            reason = f"hit={hit_rate}, net={mean_net}"
        rows.append({
            "strategy": strategy,
            "horizon_days": SCREENER_PRIMARY_HORIZON,
            "n": n,
            "hit_rate": hit_rate,
            "mean_net_return": mean_net,
            "horizon_5d": by_h.get("5"),
            "verdict": verdict,
            "reason": reason,
        })
    return {
        "available": True,
        "generated_at": book.get("generated_at"),
        "measured_return_count": book.get("measured_return_count"),
        "pending_episode_count": book.get("pending_episode_count"),
        "rows": rows,
    }


def generate_report() -> dict:
    """レーン横断の月次ガバナンスドラフトを生成する。自動アクションは取らない。"""
    agent_data = _load_json(AGENT_RELIABILITY_PATH)
    scenario_data = _load_json(SCENARIO_PROMOTION_PATH)
    action_state = _load_json(ACTION_STATE_PATH)
    leveraged_decay_data = _load_json(LEVERAGED_DECAY_PATH)
    shadow_book_data = _load_json(DISCLOSURE_SHADOW_BOOK_PATH)
    screener_shadow_data = _load_json(SCREENER_SHADOW_BOOK_PATH)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "人間レビュー前提のドラフト判定。自動昇格・自動廃止は行わない。",
        "analyst_agents": _analyst_agents_section(agent_data),
        "scenarios": _scenario_section(scenario_data),
        "tax_harvest": _tax_harvest_section(action_state),
        "leveraged_decay": _leveraged_decay_section(leveraged_decay_data),
        "red_team": _red_team_section(),
        "disclosure_features": _disclosure_feature_section(),
        "swing_lane": _swing_lane_section(),
        "jp_event_drift": _jp_event_drift_section(shadow_book_data),
        "screener_lane": _screener_lane_section(screener_shadow_data),
        "lanes_without_instrumentation": LANES_WITHOUT_INSTRUMENTATION,
        "thresholds": {
            "min_measured_n": MIN_MEASURED_N,
            "min_win_rate": MIN_WIN_RATE,
            "min_mean_excess_return_bps": MIN_MEAN_EXCESS_RETURN_BPS,
        },
    }


def format_text_summary(report: dict) -> str:
    lines = [f"月次ガバナンスドラフト ({report['generated_at'][:10]})", "=" * 40]

    def _fmt_rows(title: str, section: dict, id_key: str) -> None:
        lines.append(f"\n## {title}")
        if not section.get("available"):
            lines.append(f"  データなし: {section.get('reason')}")
            return
        rows = section.get("rows")
        if rows is None:
            verdict = section.get("verdict", "?")
            reason = section.get("reason", "")
            lines.append(f"  [{verdict}] {reason}")
            return
        if not rows:
            lines.append("  対象なし")
        for row in rows:
            lines.append(f"  [{row['verdict']}] {row.get(id_key)}: {row['reason']}")

    _fmt_rows("分析エージェント", report["analyst_agents"], "agent")
    _fmt_rows("シナリオ", report["scenarios"], "scenario_id")
    _fmt_rows("開示特徴量", report["disclosure_features"], "disclosure_type")

    lines.append("\n## 損出しスキャナ")
    th = report["tax_harvest"]
    if th.get("available"):
        lines.append(f"  [{th['verdict']}] {th.get('reason')}")
    else:
        lines.append(f"  データなし: {th.get('reason')}")

    lines.append("\n## レバレッジ商品(解除トリガー監視)")
    ld = report["leveraged_decay"]
    if ld.get("available"):
        lines.append(f"  [{ld['verdict']}] {ld.get('reason')}")
    else:
        lines.append(f"  データなし: {ld.get('reason')}")

    lines.append("\n## RedTeam save-rate")
    rt = report["red_team"]
    if rt.get("available"):
        lines.append(f"  [{rt['verdict']}] {rt.get('reason')}")
    else:
        lines.append(f"  データなし: {rt.get('reason')}")

    lines.append("\n## Swingレーン")
    sw = report["swing_lane"]
    if sw.get("available"):
        lines.append(f"  [{sw['verdict']}] {sw.get('reason')}")
    else:
        lines.append(f"  データなし: {sw.get('reason')}")

    lines.append("\n## JPイベントドリフト(開示シャドーブック)")
    jd = report["jp_event_drift"]
    if jd.get("available"):
        lines.append(f"  [{jd['verdict']}] {jd.get('reason')}")
    else:
        lines.append(f"  データなし: {jd.get('reason')}")

    lines.append("\n## スクリーナー戦略別(observe_only)")
    sc = report["screener_lane"]
    if sc.get("available"):
        if not sc.get("rows"):
            lines.append("  対象なし")
        for row in sc["rows"]:
            lines.append(f"  [{row['verdict']}] {row['strategy']}: {row['reason']}")
    else:
        lines.append(f"  データなし: {sc.get('reason')}")

    lines.append("\n## 計測未整備レーン（今回は判定対象外）")
    for lane in report["lanes_without_instrumentation"]:
        lines.append(f"  - {lane}")

    return "\n".join(lines)


def main() -> int:
    report = generate_report()
    DEFAULT_REPORT_DIR.mkdir(exist_ok=True)
    out_path = DEFAULT_REPORT_DIR / f"governance_{datetime.now().strftime('%Y%m')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(format_text_summary(report))
    print(f"\n[saved] {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
behavior_coverage_report.py — Action pipeline coverage analysis.

action_stage_log.jsonl を読み込み、以下を層別に集計する:
  1. raw / policy / final / executed の action type 分布
  2. eligible 機会に対する発火率
  3. policy / post-filter の拒否理由分布（action type 別）
  4. run 単位・notional 単位の buy/sell 比率
  5. 同一 ticker・同方向の連日再掲数
  6. DCA / scenario の「評価未実施」vs「発火なし」
  7. source freshness と欠損

期待ゼロを異常扱いしない例（レポートに注記）:
  - stop_loss disabled
  - short 建玉なしの cover
  - DCA 条件未達
  - leverage/policy で禁止された short/margin_buy

CLIから実行:
  python behavior_coverage_report.py [--days N] [--json] [--since YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent

# ── 方向セット（action_stage_log.py と同一定義） ────────────
_BUY_TYPES  = {"buy", "add", "dca", "margin_buy", "cover"}
_SELL_TYPES = {"sell", "trim", "reduce", "stop_loss", "take_profit", "short"}

# 無効化設定で件数ゼロが正常な action type
_CONDITIONALLY_ZERO_TYPES = {
    "stop_loss":   "disable_stop_loss_recommendations=true で禁止 (正常)",
    "cover":       "short 建玉がある時のみ eligible",
    "short":       "leverage/VIX/policy 条件を満たす時のみ",
    "margin_buy":  "leverage/VIX/policy 条件を満たす時のみ",
    "dca":         "tranche 発火条件を満たす時のみ",
}


def _load_log(since_iso: Optional[str] = None, days: Optional[int] = None) -> list[dict]:
    from action_stage_log import read_entries, LOG_PATH
    _since = since_iso
    if _since is None and days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        _since = cutoff.date().isoformat()
    return read_entries(LOG_PATH, since_iso=_since)


def _load_dca_state() -> dict:
    p = BASE_DIR / "bottom_fishing_signals.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_scenario_state() -> dict:
    p = BASE_DIR / "scenario_state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── セクション別集計 ─────────────────────────────────────────

def _type_distribution(entries: list[dict]) -> dict[str, dict[str, int]]:
    """stage → {action_type: count}"""
    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in entries:
        result[e["stage"]][e.get("canonical_action_type", "unknown")] += 1
    return {stage: dict(counts) for stage, counts in result.items()}


def _direction_ratio(entries: list[dict]) -> dict[str, dict]:
    """stage × analysis_id 単位の buy/sell 比率。"""
    by_stage: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: {"buy": 0, "sell": 0, "neutral": 0}))
    for e in entries:
        d = e.get("direction", "neutral")
        by_stage[e["stage"]][e.get("analysis_id", "?")][d] += 1

    out: dict[str, dict] = {}
    for stage, runs in by_stage.items():
        total_buy = sum(r["buy"] for r in runs.values())
        total_sell = sum(r["sell"] for r in runs.values())
        run_count = len(runs)
        out[stage] = {
            "total_buy": total_buy,
            "total_sell": total_sell,
            "run_count": run_count,
            "buy_pct": round(100 * total_buy / max(total_buy + total_sell, 1), 1),
            "sell_pct": round(100 * total_sell / max(total_buy + total_sell, 1), 1),
        }
    return out


def _policy_reject_reasons(entries: list[dict]) -> dict[str, list[dict]]:
    """action_type → [{rule, reason, count}]"""
    counter: dict[str, Counter] = defaultdict(Counter)
    for e in entries:
        if e.get("stage") != "policy_rejected":
            continue
        atype = e.get("canonical_action_type", "unknown")
        rule = e.get("filter_rule") or "unknown_rule"
        counter[atype][rule] += 1

    out: dict[str, list[dict]] = {}
    for atype, rules in counter.items():
        out[atype] = [{"rule": r, "count": c} for r, c in rules.most_common()]
    return out


def _ordered_run_ids(entries: list[dict]) -> list[str]:
    """analysis_id を、その run の最大 as_of で時系列順に並べて返す。
    Codex re-review #7: 同一 as_of の run 順が入力順に依存して不定にならないよう、
    (as_of, 最古 logged_at, analysis_id) の複合キーで安定ソートする。"""
    run_as_of: dict[str, str] = {}
    run_logged: dict[str, str] = {}
    for e in entries:
        aid = e.get("analysis_id", "")
        if not aid or aid == "execution":
            continue
        if e.get("as_of", "") > run_as_of.get(aid, ""):
            run_as_of[aid] = e.get("as_of", "")
        lg = e.get("logged_at", "")
        if lg and (aid not in run_logged or lg < run_logged[aid]):
            run_logged[aid] = lg
    return sorted(
        run_as_of.keys(),
        key=lambda aid: (run_as_of[aid], run_logged.get(aid, ""), aid),
    )


def _consecutive_repeats(entries: list[dict]) -> list[dict]:
    """
    同一 ticker × 同方向が「連続する run」で再掲された回数を検出する。
    単なる再出現ではなく、run 順で直前 run にも存在した場合だけ連続としてカウント。
    unique_runs と max_streak の両方を返す。
    """
    finals = [e for e in entries if _is_surviving_post_filter_final(e)]
    ordered = _ordered_run_ids(finals)
    if not ordered:
        return []
    run_index = {aid: i for i, aid in enumerate(ordered)}

    # key → そのキーが出現した run index の集合
    key_runs: dict[tuple, set] = defaultdict(set)
    for e in finals:
        aid = e.get("analysis_id", "")
        if aid not in run_index:
            continue
        key = (e.get("ticker", ""), e.get("direction", ""))
        key_runs[key].add(run_index[aid])

    out = []
    for key, idxs in key_runs.items():
        sorted_idx = sorted(idxs)
        # 連続遷移数（直前 run にも存在した回数）と最長連続ストリーク
        consecutive_transitions = 0
        max_streak = 1
        cur_streak = 1
        for a, b in zip(sorted_idx, sorted_idx[1:]):
            if b == a + 1:
                consecutive_transitions += 1
                cur_streak += 1
                max_streak = max(max_streak, cur_streak)
            else:
                cur_streak = 1
        if consecutive_transitions >= 1:
            out.append({
                "ticker": key[0],
                "direction": key[1],
                "unique_runs": len(idxs),
                "consecutive_transitions": consecutive_transitions,
                "max_consecutive_streak": max_streak,
            })
    out.sort(key=lambda x: x["consecutive_transitions"], reverse=True)
    return out[:20]


def _notional_ratio(entries: list[dict]) -> dict:
    """executed ステージの estimated_notional_jpy を方向別に集計（金額ベース比率）。"""
    buy_jpy = 0.0
    sell_jpy = 0.0
    counted = 0
    for e in entries:
        if e.get("stage") != "executed":
            continue
        n = e.get("estimated_notional_jpy")
        if not isinstance(n, (int, float)):
            continue
        counted += 1
        if e.get("direction") == "buy":
            buy_jpy += float(n)
        elif e.get("direction") == "sell":
            sell_jpy += float(n)
    total = buy_jpy + sell_jpy
    return {
        "executed_with_notional": counted,
        "buy_notional_jpy": round(buy_jpy),
        "sell_notional_jpy": round(sell_jpy),
        "buy_notional_pct": round(100 * buy_jpy / total, 1) if total > 0 else None,
        "sell_notional_pct": round(100 * sell_jpy / total, 1) if total > 0 else None,
    }


def _is_surviving_post_filter_final(entry: dict) -> bool:
    return (
        entry.get("stage") == "post_filter_final"
        and not entry.get("filtered_reason")
        and entry.get("eligible") is not False
    )


def _post_filter_drop(entries: list[dict]) -> dict:
    """opus_raw → post_filter_final の脱落を run 横断で集計（policy 以外の後段除外可視化）。"""
    raw_by_run: dict[str, set] = defaultdict(set)
    final_by_run: dict[str, set] = defaultdict(set)
    for e in entries:
        aid = e.get("analysis_id", "")
        key = (e.get("ticker", ""), e.get("canonical_action_type", ""))
        if e.get("stage") == "opus_raw":
            raw_by_run[aid].add(key)
        elif _is_surviving_post_filter_final(e):
            final_by_run[aid].add(key)
    dropped = 0
    survived = 0
    for aid, raw_keys in raw_by_run.items():
        finals = final_by_run.get(aid, set())
        dropped += len(raw_keys - finals)
        survived += len(raw_keys & finals)
    return {
        "raw_to_final_dropped": dropped,
        "raw_to_final_survived": survived,
        "survival_pct": round(100 * survived / (dropped + survived), 1) if (dropped + survived) else None,
    }


def _candidate_to_final_rate(entries: list[dict]) -> dict:
    """
    Codex re-review round3 #4: tier_generated の候補が最終推奨 (post_filter_final)
    まで到達した割合を方向別に集計する。
    分母は tier_generated 全件 (hard policy reject も含む) なので「eligible 率」では
    なく candidate→final の通過率。eligible 率を出すには policy_accepted を分母に
    する必要があり、それは別指標 (_post_filter_drop / policy_reject_reasons で可視化)。
    run × ticker × direction を1候補として数える。
    """
    gen: set = set()
    fired: set = set()
    for e in entries:
        aid = e.get("analysis_id", "")
        key = (aid, e.get("ticker", ""), e.get("direction", ""))
        if e.get("stage") == "tier_generated":
            gen.add(key)
        elif _is_surviving_post_filter_final(e):
            fired.add(key)
    if not gen:
        return {"candidates": 0, "reached_final": 0, "rate_pct": None, "by_direction": {}}
    by_dir: dict[str, dict] = {}
    for direction in ("buy", "sell"):
        g = {k for k in gen if k[2] == direction}
        f = {k for k in g if k in fired}
        by_dir[direction] = {
            "candidates": len(g),
            "reached_final": len(f),
            "rate_pct": round(100 * len(f) / len(g), 1) if g else None,
        }
    reached_total = len(gen & fired)
    return {
        "candidates": len(gen),
        "reached_final": reached_total,
        "rate_pct": round(100 * reached_total / len(gen), 1),
        "by_direction": by_dir,
    }


def _stage_transition_rates(entries: list[dict]) -> dict[str, dict]:
    """
    stage 間の通過率を run × ticker × direction で集計する。
    action type は buy/add や sell/trim で変わり得るため、direction 単位で追跡する。
    """
    stages = ("tier_generated", "opus_raw", "policy_accepted", "post_filter_final")
    by_stage: dict[str, dict[str, set[tuple[str, str, str]]]] = {
        stage: {"buy": set(), "sell": set()} for stage in stages
    }
    for e in entries:
        stage = e.get("stage", "")
        if stage not in by_stage:
            continue
        if stage == "post_filter_final" and not _is_surviving_post_filter_final(e):
            continue
        aid = e.get("analysis_id", "")
        if not aid or aid == "execution":
            continue
        direction = e.get("direction", "neutral")
        if direction not in ("buy", "sell"):
            continue
        by_stage[stage][direction].add((aid, e.get("ticker", ""), direction))

    out: dict[str, dict] = {}
    for src, dst in zip(stages, stages[1:]):
        key = f"{src}_to_{dst}"
        by_direction: dict[str, dict] = {}
        for direction in ("buy", "sell"):
            source_keys = by_stage[src][direction]
            dest_keys = by_stage[dst][direction]
            passed = len(source_keys & dest_keys)
            total = len(source_keys)
            by_direction[direction] = {
                "from": total,
                "to": passed,
                "rate_pct": round(100 * passed / total, 1) if total else None,
            }
        out[key] = {"by_direction": by_direction}
    return out


def _source_freshness(dca_state: dict, scenario_state: dict, entries: list[dict]) -> dict:
    """各 source の鮮度（評価日 vs 当日）と stage log の最新時刻を報告。"""
    from datetime import date
    today = date.today().isoformat()
    dca_date = dca_state.get("freshness_date") or str(dca_state.get("evaluated_at") or "")[:10]
    scn_eval = scenario_state.get("evaluated_at", "")
    latest_log = max((e.get("as_of", "") for e in entries), default="")
    return {
        "today": today,
        "dca_freshness_date": dca_date,
        "dca_is_fresh": (dca_date == today),
        "scenario_evaluated_at": scn_eval,
        "scenario_is_fresh": (str(scn_eval)[:10] == today),
        "latest_stage_log_as_of": latest_log,
    }


def _analysis_run_summary(entries: list[dict]) -> dict[str, dict]:
    """analysis_id → {as_of, stages_present, counts}"""
    runs: dict[str, dict] = defaultdict(lambda: {"as_of": "", "stages": set(), "by_stage": defaultdict(int)})
    for e in entries:
        aid = e.get("analysis_id", "?")
        runs[aid]["stages"].add(e.get("stage", ""))
        runs[aid]["by_stage"][e.get("stage", "")] += 1
        if e.get("as_of", "") > runs[aid]["as_of"]:
            runs[aid]["as_of"] = e.get("as_of", "")
    return {
        aid: {
            "as_of": v["as_of"],
            "stages_present": sorted(v["stages"]),
            "counts": dict(v["by_stage"]),
        }
        for aid, v in sorted(runs.items(), key=lambda x: x[1]["as_of"])
    }


def _scenario_coverage(state: dict) -> list[dict]:
    """シナリオ別のステータスと observe_only フラグを列挙。"""
    out = []
    for sid, info in state.get("scenarios", {}).items():
        out.append({
            "scenario_id": sid,
            "name": info.get("name", sid),
            "status": info.get("status", "?"),
            "readiness": info.get("readiness"),
            "observe_only": info.get("observe_only", False),
            "enabled_for_decision": info.get("enabled_for_decision", True),
            "missing_required_signals": info.get("missing_required_signals", []),
        })
    return sorted(out, key=lambda x: x.get("readiness") or 0, reverse=True)


def _dca_coverage(dca_state: dict) -> dict:
    """DCA の発火状態と reasons を要約。"""
    return {
        "active_tranche": dca_state.get("active_tranche"),
        "evaluated_at": dca_state.get("evaluated_at"),
        "recommended_buys_count": len(dca_state.get("recommended_buys", [])),
        "annual_remaining_pct": (dca_state.get("state") or {}).get("annual_remaining_pct"),
        "tranche_status": {
            tid: {
                "met": (dca_state.get("evaluations") or {}).get(tid, {}).get("met"),
                "reasons": (dca_state.get("evaluations") or {}).get(tid, {}).get("reasons", []),
            }
            for tid in ("T1", "T2", "T3")
        },
    }


# ── メインレポート ────────────────────────────────────────────

def generate_report(
    since_iso: Optional[str] = None,
    days: int = 14,
    include_dca: bool = True,
    include_scenarios: bool = True,
) -> dict:
    entries = _load_log(since_iso=since_iso, days=days)

    # round3 #7: 固定ラベル "execution" は約定ログ用で分析 run ではないため除外する。
    analysis_ids = {
        e.get("analysis_id") for e in entries
        if e.get("analysis_id") and e.get("analysis_id") != "execution"
    }
    unique_runs = len(analysis_ids)

    type_dist = _type_distribution(entries)
    dir_ratio  = _direction_ratio(entries)
    reject_reasons = _policy_reject_reasons(entries)
    repeats = _consecutive_repeats(entries)
    run_summary = _analysis_run_summary(entries)
    notional = _notional_ratio(entries)
    post_filter = _post_filter_drop(entries)
    cand_to_final = _candidate_to_final_rate(entries)
    stage_transition = _stage_transition_rates(entries)
    dca_state = _load_dca_state()
    scenario_state = _load_scenario_state()
    freshness = _source_freshness(dca_state, scenario_state, entries)

    # action type 別: zero は警告しない条件を注記
    all_types = set()
    for stage_counts in type_dist.values():
        all_types.update(stage_counts.keys())
    zero_notes = {t: note for t, note in _CONDITIONALLY_ZERO_TYPES.items() if t not in all_types}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": days,
        "since_iso": since_iso,
        "total_entries": len(entries),
        "unique_analysis_runs": unique_runs,
        "type_distribution_by_stage": type_dist,
        "direction_ratio_by_stage": dir_ratio,
        "policy_reject_reasons_by_type": reject_reasons,
        "consecutive_same_direction_repeats": repeats,
        "notional_ratio": notional,
        "post_filter_drop": post_filter,
        "candidate_to_final_rate": cand_to_final,
        "stage_transition_rates": stage_transition,
        "source_freshness": freshness,
        "conditionally_zero_types_note": zero_notes,
        "run_summary": run_summary,
    }

    if include_dca:
        report["dca_coverage"] = _dca_coverage(dca_state)

    if include_scenarios:
        report["scenario_coverage"] = _scenario_coverage(scenario_state)

    return report


def print_report(report: dict) -> None:
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  ALMANAC Action Coverage Report  ({report['generated_at'][:10]})")
    print(f"  期間: 過去 {report['period_days']}d  |  ラン数: {report['unique_analysis_runs']}  |  エントリ: {report['total_entries']}")
    print(sep)

    stages = (
        "tier_generated", "opus_raw", "policy_accepted", "policy_rejected",
        "post_filter_final", "post_filter_deferred", "post_filter_rejected", "executed",
    )
    print("\n■ Action Type 分布 (stage 別)")
    for stage in stages:
        counts = report["type_distribution_by_stage"].get(stage, {})
        if not counts:
            continue
        total = sum(counts.values())
        row = "  ".join(f"{t}:{n}" for t, n in sorted(counts.items(), key=lambda x: -x[1]))
        print(f"  [{stage}] total={total}  {row}")

    print("\n■ Buy/Sell 比率 (stage 別, run 集計 = 件数ベース)")
    for stage in stages:
        r = report["direction_ratio_by_stage"].get(stage)
        if not r:
            continue
        print(f"  [{stage}] buy={r['total_buy']} ({r['buy_pct']}%)  sell={r['total_sell']} ({r['sell_pct']}%)  runs={r['run_count']}")

    nz = report.get("notional_ratio", {})
    if nz.get("executed_with_notional"):
        print("\n■ Buy/Sell 比率 (executed, notional 金額ベース)")
        print(f"  buy=¥{nz['buy_notional_jpy']:,} ({nz['buy_notional_pct']}%)  "
              f"sell=¥{nz['sell_notional_jpy']:,} ({nz['sell_notional_pct']}%)  "
              f"n={nz['executed_with_notional']}")

    pf = report.get("post_filter_drop", {})
    if pf and (pf.get("raw_to_final_dropped") or pf.get("raw_to_final_survived")):
        print("\n■ opus_raw → final 生存率 (policy 含む全後段)")
        print(f"  survived={pf['raw_to_final_survived']}  dropped={pf['raw_to_final_dropped']}  "
              f"survival={pf['survival_pct']}%")

    cf = report.get("candidate_to_final_rate", {})
    if cf.get("candidates"):
        print("\n■ 候補 → 最終推奨 通過率 (tier_generated→final, policy reject 含む)")
        print(f"  全体: {cf['reached_final']}/{cf['candidates']} = {cf['rate_pct']}%")
        for _d, _v in (cf.get("by_direction") or {}).items():
            if _v.get("candidates"):
                print(f"    {_d}: {_v['reached_final']}/{_v['candidates']} = {_v['rate_pct']}%")

    tr = report.get("stage_transition_rates", {})
    if tr:
        print("\n■ Stage 通過率 (run×ticker×direction)")
        for name, data in tr.items():
            label = name.replace("_to_", " → ")
            parts = []
            for direction in ("buy", "sell"):
                v = (data.get("by_direction") or {}).get(direction) or {}
                if v.get("from"):
                    parts.append(f"{direction}: {v['to']}/{v['from']} = {v['rate_pct']}%")
            if parts:
                print(f"  {label}: " + "  ".join(parts))

    fr = report.get("source_freshness", {})
    if fr:
        print("\n■ Source 鮮度")
        _dca_mark = "✅" if fr.get("dca_is_fresh") else "⚠️stale"
        _scn_mark = "✅" if fr.get("scenario_is_fresh") else "⚠️stale"
        print(f"  DCA: {fr.get('dca_freshness_date')} {_dca_mark}  |  "
              f"scenario: {str(fr.get('scenario_evaluated_at'))[:10]} {_scn_mark}")

    if report.get("policy_reject_reasons_by_type"):
        print("\n■ Policy 拒否理由 (action_type 別)")
        for atype, rules in sorted(report["policy_reject_reasons_by_type"].items()):
            for r in rules[:3]:
                print(f"  {atype}: [{r['rule']}] ×{r['count']}")

    if report.get("consecutive_same_direction_repeats"):
        print("\n■ 連日同方向再掲 (≥2 run)")
        for item in report["consecutive_same_direction_repeats"][:10]:
            print(
                f"  {item['ticker']} {item['direction']}: "
                f"連続遷移={item.get('consecutive_transitions', 0)}  "
                f"最長={item.get('max_consecutive_streak', 0)}run  "
                f"出現={item.get('unique_runs', 0)}run"
            )

    if report.get("conditionally_zero_types_note"):
        print("\n■ 件数ゼロが正常な action type")
        for t, note in report["conditionally_zero_types_note"].items():
            print(f"  {t}: {note}")

    dca = report.get("dca_coverage", {})
    if dca:
        print(f"\n■ DCA  tranche={dca.get('active_tranche') or 'なし'}  buys={dca.get('recommended_buys_count', 0)}  "
              f"remaining={dca.get('annual_remaining_pct', '?')}")
        for tid, st in (dca.get("tranche_status") or {}).items():
            mark = "✅" if st.get("met") else "❌"
            reasons = "; ".join((st.get("reasons") or [])[:2])
            print(f"  {mark} {tid}: {reasons[:80]}")

    scenarios = report.get("scenario_coverage", [])
    if scenarios:
        print("\n■ シナリオ状態")
        for sc in scenarios[:8]:
            obs = " [observe_only]" if sc.get("observe_only") else ""
            no_dec = " [no_decision]" if not sc.get("enabled_for_decision", True) else ""
            miss = f" missing={sc['missing_required_signals']}" if sc.get("missing_required_signals") else ""
            pct = f"{sc['readiness'] * 100:.0f}%" if sc.get("readiness") is not None else "?"
            print(f"  {sc['status']:10s} {pct:4s}  {sc['scenario_id']}{obs}{no_dec}{miss}")

    print(f"\n{sep}\n")


# ── CLI ──────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="ALMANAC action coverage report")
    parser.add_argument("--days", type=int, default=14, help="過去 N 日を集計 (default: 14)")
    parser.add_argument("--since", type=str, default=None, help="開始日 YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="JSON で出力")
    args = parser.parse_args()

    report = generate_report(since_iso=args.since, days=args.days)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()

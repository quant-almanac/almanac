#!/usr/bin/env python
"""
compare_harness.py — ALMANAC スクリーナー AI ハーネス A/B 勝率比較

signal_history.json から ai_source フィールドでハーネス別に集計し、
legacy（Sonnet×3 ディベート）vs deepseek（DeepSeek V4 + Sonnet 第二意見）の
勝率・平均リターンを比較する。

判定基準:
  - BUY 5d 勝率の差分が ±5pt 以内 → deepseek 採用継続（コスト削減効果＞品質劣化）
  - 差分が -5pt 超（deepseek が大幅低下）→ legacy へロールバック推奨
  - サンプル不足（各 n<10）→ 「データ不足」と表示

使い方:
  python compare_harness.py --days 7      # 直近 7 日
  python compare_harness.py --days 30     # 直近 30 日
  python compare_harness.py --json        # JSON 出力（CI / Telegram 連携用）
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
HISTORY  = BASE_DIR / "signal_history.json"

# 採用判定のしきい値（BUY 5d 勝率差分、pt）
ACCEPT_BAND_PT = 5.0


def load_history() -> list:
    if not HISTORY.exists():
        return []
    try:
        return json.loads(HISTORY.read_text())
    except Exception:
        return []


def filter_recent(history: list, days: int) -> list:
    """直近 days 日以内のレコードのみ抽出。"""
    if days <= 0:
        return history
    cutoff = (datetime.now().date() - timedelta(days=days)).isoformat()
    out = []
    for r in history:
        date = r.get("date") or r.get("generated_at") or ""
        if date and date[:10] >= cutoff:
            out.append(r)
    return out


def _classify_source(rec: dict) -> str:
    """ai_source / ai_debate / source フィールドからハーネス種別を分類。"""
    src = (rec.get("ai_source") or "").lower()
    if "deepseek" in src:
        return "deepseek"
    if "legacy_sonnet" in src or "sonnet_debate" in src:
        return "legacy"
    if "haiku_fallback" in src:
        return "haiku_fallback"
    if "sonnet_demoted" in src:
        return "deepseek"  # second opinion 介入も deepseek 系に集計
    # ai_debate に bull/bear/macro 構造があれば deepseek 系（新ハーネス）
    debate = rec.get("ai_debate")
    if isinstance(debate, dict) and ("bull" in debate or "bear" in debate or "macro" in debate):
        return "deepseek"
    # 旧 source 列との互換
    legacy_marker = (rec.get("source") or "").lower()
    if "sonnet" in legacy_marker:
        return "legacy"
    return "unknown"


def _signal_metrics(records: list, signal: str) -> dict:
    """指定 signal (BUY/WATCH/SKIP) の勝率/平均リターン/件数。outcome_5d 必須。"""
    subset = [r for r in records if r.get("signal") == signal and r.get("outcome_5d") is not None]
    n = len(subset)
    if n == 0:
        return {"count": 0, "win_rate": None, "avg_return": None, "median_return": None}
    returns = [r["outcome_5d"] for r in subset]
    wins = [x for x in returns if x > 0]
    return {
        "count":         n,
        "win_rate":      round(len(wins) / n * 100, 1),
        "avg_return":    round(sum(returns) / n, 2),
        "median_return": round(statistics.median(returns), 2),
    }


def compare(history: list, days: int) -> dict:
    recent = filter_recent(history, days)

    by_harness: dict[str, list] = {"legacy": [], "deepseek": [], "haiku_fallback": [], "unknown": []}
    for r in recent:
        by_harness[_classify_source(r)].append(r)

    out = {
        "period_days":    days,
        "total_records":  len(recent),
        "by_harness":     {},
        "diff":           {},
        "verdict":        "",
        "verdict_reason": "",
    }
    for harness, recs in by_harness.items():
        out["by_harness"][harness] = {
            "total_records": len(recs),
            "BUY":   _signal_metrics(recs, "BUY"),
            "WATCH": _signal_metrics(recs, "WATCH"),
            "SKIP":  _signal_metrics(recs, "SKIP"),
        }

    # 差分判定
    legacy_buy   = out["by_harness"]["legacy"]["BUY"]
    deepseek_buy = out["by_harness"]["deepseek"]["BUY"]
    if legacy_buy["count"] >= 10 and deepseek_buy["count"] >= 10:
        diff = deepseek_buy["win_rate"] - legacy_buy["win_rate"]
        out["diff"] = {
            "buy_win_rate_diff_pt": round(diff, 1),
            "buy_avg_return_diff":  round(deepseek_buy["avg_return"] - legacy_buy["avg_return"], 2),
        }
        if abs(diff) <= ACCEPT_BAND_PT:
            out["verdict"]        = "adopt_deepseek"
            out["verdict_reason"] = f"BUY 勝率差 {diff:+.1f}pt は許容範囲 (±{ACCEPT_BAND_PT}pt) — DeepSeek 継続"
        elif diff > ACCEPT_BAND_PT:
            out["verdict"]        = "deepseek_better"
            out["verdict_reason"] = f"DeepSeek が legacy を {diff:+.1f}pt 上回る — DeepSeek 採用継続"
        else:
            out["verdict"]        = "rollback_to_legacy"
            out["verdict_reason"] = f"DeepSeek が legacy を {-diff:.1f}pt 下回る (>{ACCEPT_BAND_PT}pt) — legacy にロールバック検討"
    else:
        out["verdict"]        = "insufficient_data"
        out["verdict_reason"] = (
            f"サンプル不足 (legacy n={legacy_buy['count']}, deepseek n={deepseek_buy['count']}, "
            "両方 ≥10 必要)"
        )

    return out


def print_text_report(report: dict) -> None:
    print(f"\n===== ALMANAC ハーネス A/B 比較 (直近 {report['period_days']} 日) =====")
    print(f"対象レコード: {report['total_records']} 件\n")
    for harness in ("legacy", "deepseek", "haiku_fallback", "unknown"):
        h = report["by_harness"].get(harness, {})
        n = h.get("total_records", 0)
        if n == 0:
            print(f"  [{harness:16s}] レコードなし")
            continue
        buy   = h["BUY"]
        watch = h["WATCH"]
        skip  = h["SKIP"]
        print(f"  [{harness:16s}] 総 {n} 件")
        if buy["count"] > 0:
            print(f"      BUY   n={buy['count']:3d}  勝率 {buy['win_rate']:.1f}%  平均 {buy['avg_return']:+.2f}%  中央値 {buy['median_return']:+.2f}%")
        if watch["count"] > 0:
            print(f"      WATCH n={watch['count']:3d}  勝率 {watch['win_rate']:.1f}%  平均 {watch['avg_return']:+.2f}%")
        if skip["count"] > 0:
            print(f"      SKIP  n={skip['count']:3d}  勝率 {skip['win_rate']:.1f}%  平均 {skip['avg_return']:+.2f}%")

    print()
    if report.get("diff"):
        d = report["diff"]
        print(f"BUY 勝率差分:    {d['buy_win_rate_diff_pt']:+.1f} pt")
        print(f"BUY 平均RET差分: {d['buy_avg_return_diff']:+.2f} %")
    print(f"\n判定: {report['verdict']}")
    print(f"理由: {report['verdict_reason']}\n")


def main() -> int:
    p = argparse.ArgumentParser(description="ALMANAC AI harness A/B comparison")
    p.add_argument("--days", type=int, default=7, help="集計期間（日数, 0 で全期間）")
    p.add_argument("--json", action="store_true", help="JSON 出力（テキスト無し）")
    args = p.parse_args()

    history = load_history()
    if not history:
        print("ERROR: signal_history.json が空 or 読み込み失敗", file=sys.stderr)
        return 1
    report = compare(history, args.days)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

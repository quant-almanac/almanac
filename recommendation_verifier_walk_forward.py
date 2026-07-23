"""
v5.1: recommendation_verifier の Walk-Forward + Shortfall 控除後勝率
=================================================================

既存 recommendation_verifier.py は「単一スナップショット」の勝率テーブルを返す。
v5.1 では以下を追加:

1. walk_forward_summary(): 推奨を月次バケットで分割し、勝率の推移を返す
   → 「AI の判断は時間と共に改善しているか？」を可視化
2. shortfall_adjusted_summary(): action_executions.json の shortfall_bps を
   outcome_pct から差し引いた「執行コスト控除後の真の勝率」を返す
   → 「指値が刺さらなかった時の取り逃しまで含めた現実的な収益性」

どちらも既存コードに副作用を与えず、追加 API として提供する。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
LOG_PATH = BASE_DIR / "ai_recommendation_log.json"
EXEC_PATH = BASE_DIR / "action_executions.json"


# ============================================================
# Walk-Forward windowed win-rate
# ============================================================

def _load_verified_entries() -> list[dict]:
    """ai_recommendation_log.json から outcome_pct が埋まった検証済みエントリを返す"""
    if not LOG_PATH.exists():
        return []
    try:
        log = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = log if isinstance(log, list) else log.get("entries", []) or log.get("recommendations", [])
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("outcome_pct") is None:
            continue
        out.append(e)
    return out


def _bucket_month(date_str: str) -> Optional[str]:
    if not date_str:
        return None
    try:
        return date_str[:7]  # YYYY-MM
    except Exception:
        return None


def walk_forward_summary(months_lookback: int = 6) -> dict:
    """
    過去 months_lookback ヶ月分を月次バケットに分け、各バケットの勝率を返す。

    Returns:
        {
            "windows": [
                {"month": "2026-01", "n": 12, "win_rate": 0.58, "ci_upper95": 0.81},
                ...
            ],
            "trend": "improving" | "stable" | "degrading",
            "latest_n": 整数,
            "latest_win_rate": float | None,
        }
    """
    from scipy.stats import beta as _beta

    entries = _load_verified_entries()
    if not entries:
        return {"windows": [], "trend": "no_data", "latest_n": 0, "latest_win_rate": None}

    buckets: dict[str, list[bool]] = defaultdict(list)
    for e in entries:
        bucket = _bucket_month(
            e.get("recommendation_date") or e.get("date") or e.get("as_of") or ""
        )
        if not bucket:
            continue
        outcome = e.get("outcome_pct")
        action_type = (e.get("type") or e.get("action_type") or "buy").lower()
        if outcome is None:
            continue
        # 簡易勝敗判定（recommendation_verifier._is_profitable と整合）
        if action_type in ("buy", "dca", "add"):
            win = outcome > 0
        elif action_type in ("sell", "trim", "short"):
            bench = e.get("benchmark_outcome_pct")
            if bench is not None:
                win = outcome < bench - 0.5
            else:
                win = outcome < -2.0
        elif action_type == "stop_loss":
            win = outcome < -2.0
        elif action_type == "take_profit":
            win = outcome >= 0
        else:
            continue
        buckets[bucket].append(win)

    today_bucket = datetime.now().strftime("%Y-%m")
    sorted_months = sorted(buckets.keys())[-months_lookback:]

    windows = []
    for m in sorted_months:
        results = buckets[m]
        if not results:
            continue
        wins = sum(1 for x in results if x)
        n = len(results)
        wr = wins / n if n else 0.0
        # Beta(1+wins, 1+losses) の 95% 信頼区間上限
        a, b = 1 + wins, 1 + (n - wins)
        ci_upper = float(_beta.ppf(0.95, a, b))
        windows.append({
            "month": m,
            "n": n,
            "wins": wins,
            "win_rate": round(wr, 4),
            "ci_upper95": round(ci_upper, 4),
            "is_current": (m == today_bucket),
        })

    # トレンド判定: 直近3窓の平均 vs それ以前の平均
    trend = "stable"
    if len(windows) >= 4:
        recent = sum(w["win_rate"] for w in windows[-3:]) / 3
        prior = sum(w["win_rate"] for w in windows[:-3]) / max(1, len(windows) - 3)
        if recent - prior > 0.08:
            trend = "improving"
        elif prior - recent > 0.08:
            trend = "degrading"

    latest = windows[-1] if windows else None
    return {
        "windows": windows,
        "trend": trend,
        "latest_n": latest["n"] if latest else 0,
        "latest_win_rate": latest["win_rate"] if latest else None,
    }


# ============================================================
# Shortfall-adjusted win rate
# ============================================================

def _load_executions() -> list[dict]:
    if not EXEC_PATH.exists():
        return []
    try:
        data = json.loads(EXEC_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return data
    return data.get("executions", [])


def _shortfall_lookup(execs: list[dict]) -> dict[str, float]:
    """ticker × decision_ts -> shortfall_bps の最新値マップ"""
    out: dict[str, float] = {}
    for ex in execs:
        sf = ex.get("shortfall_bps")
        if sf is None:
            continue
        ticker = ex.get("ticker")
        dt = (ex.get("decision_ts") or ex.get("saved_at") or "")[:10]
        if not ticker or not dt:
            continue
        key = f"{ticker}@{dt}"
        out[key] = float(sf)
    return out


def shortfall_adjusted_summary() -> dict:
    """
    検証済み推奨に対して shortfall_bps を outcome_pct から控除し、
    生勝率と shortfall 控除後勝率を比較。
    """
    entries = _load_verified_entries()
    if not entries:
        return {"n": 0, "raw_win_rate": None, "net_win_rate": None}

    sf_map = _shortfall_lookup(_load_executions())

    raw_wins = 0
    net_wins = 0
    n = 0
    total_shortfall_pct = 0.0
    matched_shortfalls = 0

    for e in entries:
        outcome = e.get("outcome_pct")
        if outcome is None:
            continue
        action_type = (e.get("type") or e.get("action_type") or "buy").lower()
        if action_type not in ("buy", "dca", "add", "sell", "trim", "short"):
            continue

        ticker = e.get("ticker")
        rec_date = (e.get("recommendation_date") or e.get("as_of") or "")[:10]
        sf_bps = sf_map.get(f"{ticker}@{rec_date}")
        sf_pct = (sf_bps / 100.0) if sf_bps is not None else 0.0
        if sf_bps is not None:
            total_shortfall_pct += sf_pct
            matched_shortfalls += 1

        # raw outcome
        raw_outcome = float(outcome)
        # shortfall は買いの場合「不利な分だけアウトカムが下振れ」、売りも同方向
        # （buy で約定価格が高い → リターンは shortfall_pct 分減る）
        net_outcome = raw_outcome - sf_pct

        def _win(o: float) -> bool:
            if action_type in ("buy", "dca", "add"):
                return o > 0
            if action_type in ("sell", "trim", "short"):
                bench = e.get("benchmark_outcome_pct")
                if bench is not None:
                    return o < bench - 0.5
                return o < -2.0
            return False

        if _win(raw_outcome):
            raw_wins += 1
        if _win(net_outcome):
            net_wins += 1
        n += 1

    if n == 0:
        return {"n": 0, "raw_win_rate": None, "net_win_rate": None}

    avg_sf = (total_shortfall_pct / matched_shortfalls) if matched_shortfalls else None

    return {
        "n":                 n,
        "matched_with_shortfall": matched_shortfalls,
        "raw_win_rate":      round(raw_wins / n, 4),
        "net_win_rate":      round(net_wins / n, 4),
        "win_rate_delta":    round((net_wins - raw_wins) / n, 4),
        "avg_shortfall_pct": round(avg_sf, 4) if avg_sf is not None else None,
    }


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    print("=== Walk-Forward Summary ===")
    wf = walk_forward_summary()
    print(json.dumps(wf, ensure_ascii=False, indent=2))
    print("\n=== Shortfall-Adjusted Summary ===")
    sa = shortfall_adjusted_summary()
    print(json.dumps(sa, ensure_ascii=False, indent=2))

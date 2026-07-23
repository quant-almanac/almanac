"""red_team_ledger.py — RedTeam攻撃案へのOpus採否(adopt/partial/reject)を記録し、
事後にforward returnを測定する。

攻めバックログ 2026-07 項目2: 「RedTeamが本当に悪いトレードを止めているか」を
測定可能にする。特に reject（不採用）は現状どこにも記録が残らず、
「止めたのが正しかったか」を検証する手段が無い。

既存の almanac.observability (catalyst_hypothesis_log 等) とは別の軽量ログにする理由:
  write_catalyst_hypothesis_generated は catalyst_score/scenario_readiness/
  priced_in_penalty/surprise_score が必須だが、これらはニュース/イベント駆動
  カタリスト固有の概念で、RedTeamの「攻撃的な代替テーゼ」には意味的に対応しない。
  無理に0埋めするとカタリスト層自体の集計統計を汚す。
  価格取得だけ outcome_updater.YFinancePriceProvider を再利用する。

使い方:
    from red_team_ledger import record_verdict, measure_outcomes
    record_verdict(ticker="AVGO", action="buy 5株", verdict="reject",
                   verdict_reason="流動性懸念", model="deepseek")
    measure_outcomes()  # 経過済みのverdictにforward returnを付与 (平日cron想定)
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
VERDICT_LOG_PATH = BASE_DIR / "red_team_verdict_log.jsonl"
OUTCOME_LOG_PATH = BASE_DIR / "red_team_outcome_log.jsonl"
HORIZON_DAYS = 20
VALID_VERDICTS = ("adopt", "partial", "reject")


def _make_red_team_id(ticker: str, action: str, analysis_date: str, model: str) -> str:
    raw = f"{ticker}|{action}|{analysis_date}|{model}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_verdict(
    *,
    ticker: str,
    action: str,
    verdict: str,
    verdict_reason: str,
    model: str,
    analysis_date: Optional[str] = None,
    log_path: Optional[Path] = None,
) -> str:
    """RedTeam攻撃案1件へのOpus採否を記録する。冪等 (同一内容+同日は同じID)。"""
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r} (allowed: {VALID_VERDICTS})")
    if not ticker or not action:
        raise ValueError("ticker と action は必須です")

    resolved_date = analysis_date or date.today().isoformat()
    red_team_id = _make_red_team_id(ticker, action, resolved_date, model)
    path = Path(log_path) if log_path is not None else VERDICT_LOG_PATH

    row = {
        "red_team_id": red_team_id,
        "ticker": ticker,
        "action": action,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "model": model,
        "analysis_date": resolved_date,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_jsonl(path, row)
    return red_team_id


def measure_outcomes(
    *,
    horizon_days: int = HORIZON_DAYS,
    price_provider=None,
    verdict_log_path: Optional[Path] = None,
    outcome_log_path: Optional[Path] = None,
    as_of: Optional[date] = None,
) -> dict:
    """horizon_days 経過済み・未測定の verdict に forward return を付けて記録する。

    reject/partial も含め全 verdict を測定対象にする — 「止めたのが正しかったか」
    (reject後に下落していれば save、上昇していれば false-reject) を判定するため。
    """
    from almanac.observability.outcome_updater import (
        YFinancePriceProvider,
        is_unmeasurable_primary_ticker,
    )

    provider = price_provider or YFinancePriceProvider()
    v_path = Path(verdict_log_path) if verdict_log_path is not None else VERDICT_LOG_PATH
    o_path = Path(outcome_log_path) if outcome_log_path is not None else OUTCOME_LOG_PATH
    today = as_of or date.today()

    verdicts = _read_jsonl(v_path)
    measured_keys = {
        (row.get("red_team_id"), row.get("horizon_days")) for row in _read_jsonl(o_path)
    }

    newly_measured = 0
    skipped_unmeasurable = 0
    skipped_no_price = 0

    for row in verdicts:
        rtid = row.get("red_team_id")
        if not rtid or (rtid, horizon_days) in measured_keys:
            continue
        try:
            analysis_date = date.fromisoformat(str(row.get("analysis_date")))
        except Exception:
            continue
        if (today - analysis_date).days < horizon_days:
            continue  # まだ経過待ち

        ticker = str(row.get("ticker") or "")
        if is_unmeasurable_primary_ticker(ticker):
            skipped_unmeasurable += 1
            continue

        p0 = provider.price_on_or_after(ticker, analysis_date)
        p1 = provider.price_on_or_after(ticker, analysis_date + timedelta(days=horizon_days))
        if p0 is None or p1 is None or p0 <= 0:
            skipped_no_price += 1
            continue

        outcome_row = {
            "red_team_id": rtid,
            "horizon_days": horizon_days,
            "return_pct": round((p1 / p0) - 1.0, 6),
            "measured_at": datetime.now(timezone.utc).isoformat(),
        }
        _append_jsonl(o_path, outcome_row)
        measured_keys.add((rtid, horizon_days))
        newly_measured += 1

    return {
        "newly_measured": newly_measured,
        "skipped_unmeasurable": skipped_unmeasurable,
        "skipped_no_price": skipped_no_price,
    }


def aggregate_save_rate(
    *,
    horizon_days: int = HORIZON_DAYS,
    verdict_log_path: Optional[Path] = None,
    outcome_log_path: Optional[Path] = None,
) -> dict:
    """reject 判定の save-rate (その後マイナスだった割合) を集計する。

    save-rate が高い = RedTeamのreject判断が的確 (悪い案を正しく止めている)。
    """
    v_path = Path(verdict_log_path) if verdict_log_path is not None else VERDICT_LOG_PATH
    o_path = Path(outcome_log_path) if outcome_log_path is not None else OUTCOME_LOG_PATH

    verdicts = {row["red_team_id"]: row for row in _read_jsonl(v_path) if row.get("red_team_id")}
    outcomes_by_id: dict[str, dict] = {}
    for row in _read_jsonl(o_path):
        if row.get("horizon_days") != horizon_days:
            continue
        outcomes_by_id[row["red_team_id"]] = row

    reject_returns: list[float] = []
    adopt_returns: list[float] = []
    for rtid, v in verdicts.items():
        outcome = outcomes_by_id.get(rtid)
        if outcome is None:
            continue
        ret = outcome.get("return_pct")
        if ret is None:
            continue
        if v.get("verdict") == "reject":
            reject_returns.append(ret)
        elif v.get("verdict") in ("adopt", "partial"):
            adopt_returns.append(ret)

    n_reject_measured = len(reject_returns)
    saves = sum(1 for r in reject_returns if r < 0)  # rejectしたのに下落 = 正しく止めた
    false_rejects = sum(1 for r in reject_returns if r > 0)  # rejectしたのに上昇 = 逸失
    save_rate = (saves / n_reject_measured) if n_reject_measured > 0 else None

    return {
        "n_reject_measured": n_reject_measured,
        "n_adopt_measured": len(adopt_returns),
        "saves": saves,
        "false_rejects": false_rejects,
        "save_rate": round(save_rate, 4) if save_rate is not None else None,
        "adopt_mean_return_pct": (
            round(sum(adopt_returns) / len(adopt_returns), 4) if adopt_returns else None
        ),
    }


if __name__ == "__main__":
    import json as _json
    import sys as _sys

    if len(_sys.argv) > 1 and _sys.argv[1] == "measure":
        print(_json.dumps(measure_outcomes(), ensure_ascii=False, indent=2))
    elif len(_sys.argv) > 1 and _sys.argv[1] == "report":
        print(_json.dumps(aggregate_save_rate(), ensure_ascii=False, indent=2))
    else:
        print("Usage: python red_team_ledger.py [measure|report]")

"""contribution_schedule.py — 定期積立 (standing orders) の定義

Modified Dietz TWR は external cash_flow を controlled-out する。これらの定期積立を
ledger に cash_flow として記録しないと、入金が運用成績として誤計上される (excess α 汚染)。

公開版には個人の積立額・日程を含めない。実運用では
``ALMANAC_CONTRIBUTION_SCHEDULE_JSON`` に JSON 配列を設定する。
未設定・不正値の場合は空配列に fail-closed する。

用途:
  - cash_flow_ledger_status() の expected_count 算出 (excess α 再解禁ゲート)
  - generate_transactions() で cash_transactions.json を生成 → backfill で ledger 反映

⚠️ `day_of_month` は nominal。実約定日が判明したら置換推奨 (TWR への日付ズレ影響は軽微)。
   amount/cadence は確定値なので変更しないこと。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta
from typing import List, Tuple

def _load_contributions() -> list[dict]:
    raw = os.getenv("ALMANAC_CONTRIBUTION_SCHEDULE_JSON", "")
    if not raw.strip():
        return []
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [
        row for row in rows
        if isinstance(row, dict)
        and row.get("id")
        and row.get("cadence") in {"monthly", "weekly"}
        and float(row.get("amount") or 0) > 0
    ]


CONTRIBUTIONS = _load_contributions()


def occurrences(date_from: str, date_to: str) -> List[Tuple[date, dict]]:
    """[date_from, date_to] 内の各積立の発生日を (date, contribution) で列挙。"""
    d0 = date.fromisoformat(str(date_from)[:10])
    d1 = date.fromisoformat(str(date_to)[:10])
    out: List[Tuple[date, dict]] = []
    for c in CONTRIBUTIONS:
        if c["cadence"] == "monthly":
            y, m = d0.year, d0.month
            while True:
                day = min(int(c.get("day_of_month", 1)), 28)
                occ = date(y, m, day)
                if occ > d1:
                    break
                if occ >= d0:
                    out.append((occ, c))
                m += 1
                if m > 12:
                    m, y = 1, y + 1
        elif c["cadence"] == "weekly":
            cur = d0
            while cur.weekday() != int(c.get("weekday", 0)):
                cur += timedelta(days=1)
            while cur <= d1:
                out.append((cur, c))
                cur += timedelta(days=7)
    return sorted(out, key=lambda x: x[0])


def expected_count(date_from: str, date_to: str) -> int:
    """期間内に発生が見込まれる積立件数。"""
    return len(occurrences(date_from, date_to))


def generate_transactions(date_from: str, date_to: str) -> List[dict]:
    """cash_transactions.json 互換の transaction dict を schedule から生成。

    deterministic な id を付けるので backfill / 再生成しても重複しない。
    すべて deposit (入金 = external in)。
    """
    txs = []
    for occ, c in occurrences(date_from, date_to):
        tx_id = "sched_" + hashlib.sha256(f"{c['id']}|{occ.isoformat()}".encode()).hexdigest()[:16]
        txs.append({
            "id": tx_id,
            "timestamp": occ.isoformat(),
            "type": "deposit",
            "amount": c["amount"],
            "currency": c["currency"],
            "broker": c["broker"],
            "description": f"{c['label']} 定期積立 (schedule-derived, nominal date)",
            "source": "recurring_schedule",
            "provisional_date": True,
        })
    return txs


if __name__ == "__main__":
    import sys
    a = sys.argv[1] if len(sys.argv) > 1 else "2026-05-25"
    b = sys.argv[2] if len(sys.argv) > 2 else "2026-06-30"
    print(json.dumps({
        "expected_count": expected_count(a, b),
        "transactions": generate_transactions(a, b),
    }, ensure_ascii=False, indent=2))

"""contribution_recorder.py — 定期積立を cash_flow として ledger に自動記録 (週次 cron)。

Modified Dietz TWR / excess α は external cash_flow を controlled-out する。定期積立
(持株会 / 楽天クレカ / 妻SBI) が ledger に入り続けないと、入金が運用成績として誤計上され、
かつ cash_flow_ledger_status が ok=False のまま → excess α が永久に解禁されない。

本スクリプトは contribution_schedule の発生日のうち、過去 lookback 日以内に「発生済み」
のものを cash_transactions.json に追記 (deterministic id で冪等) し、
cash_transactions_backfill 経由で event_ledger に反映する。

- 未来日付の積立は記録しない (lookback は today までで打ち切り)。
- deterministic id + backfill の冪等性により、毎週走っても重複しない (週跨ぎ overlap も安全)。
- 暫定 (nominal) 日付ベース。実ブローカー CSV 運用に切り替えたら broker_reconcile に委譲可。

使い方:
  python contribution_recorder.py            # 過去 lookback 日を記録 (apply)
  python contribution_recorder.py --dry-run  # 追記内容のみ表示 (DB 書込なし)
  python contribution_recorder.py --lookback 14
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
CASH_TX_FILE = BASE_DIR / "cash_transactions.json"


def record(*, lookback_days: int = 8, apply: bool = True) -> dict:
    from contribution_schedule import generate_transactions

    today = date.today()
    start = (today - timedelta(days=lookback_days)).isoformat()
    end = today.isoformat()  # 未来日は記録しない
    gen = generate_transactions(start, end)

    # cash_transactions.json に dedup 追記 (id ベース)
    data = {"transactions": []}
    if CASH_TX_FILE.exists():
        try:
            data = json.loads(CASH_TX_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {"transactions": []}
    if not isinstance(data.get("transactions"), list):
        data["transactions"] = []

    existing = {t.get("id") for t in data["transactions"] if isinstance(t, dict)}
    added = [t for t in gen if t.get("id") not in existing]

    result: dict = {
        "window": f"{start}..{end}",
        "generated": len(gen),
        "added_to_json": len(added),
        "added_ids": [t["id"] for t in added],
        "dry_run": not apply,
    }
    if not apply:
        result["planned_transactions"] = added

    if added and apply:
        data["transactions"].extend(added)
        CASH_TX_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    if apply:
        from cash_transactions_backfill import backfill
        r = backfill(apply=True)
        result["backfill"] = {
            "inserted": r.get("inserted"),
            "duplicates": r.get("duplicates"),
            "skipped": r.get("skipped"),
        }

    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description="定期積立 cash_flow の自動記録")
    parser.add_argument("--lookback", type=int, default=8, help="過去何日分を対象にするか (default 8)")
    parser.add_argument("--dry-run", action="store_true", help="DB / JSON を書かずに対象のみ表示")
    args = parser.parse_args()

    r = record(lookback_days=args.lookback, apply=not args.dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))

    # P2-9: watchdog ヘルスチェック用ハートビート
    try:
        from utils import heartbeat
        heartbeat("contribution_recorder", "ok", extra={"added": r.get("added_to_json")})
    except Exception:
        pass


if __name__ == "__main__":
    _main()

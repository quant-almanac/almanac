"""opening_balance_backfill_9432.py — 9432.T の開始残高を event_ledger へ backfill する。

背景 (2026-07-12 調査):
  9432.T (NTT) 100株・特定口座は 2026-05-28 の楽天CSV保有同期で holdings.json に
  取り込まれた既存保有で、trade_history.csv / event_ledger のいずれにも対応する
  BUY が存在しない。2026-07-08 に全数売却され、tax_lot.build_lots("9432.T") が
  「同一口座の lot で賄えません」で失敗する (nisa_migration_planner の
  data_quality_issues で表面化)。

  他の CSV 同期由来ポジション19件は event_ledger_backfill.py (trade_history.csv
  取込) 済みの実取引履歴で正しくカバーされており、build_lots() が個別に成功する
  ことを確認済み。この問題は 9432.T 固有 (このポジションだけ trade_history.csv
  にも一切記録が無い)。

  正確な取得日は不明。CSV同期日 (2026-05-28) を代理値として明記し、原価は
  同期スナップショットの entry_price (¥152.25) をそのまま使う。

使い方:
  python opening_balance_backfill_9432.py            # dry-run
  python opening_balance_backfill_9432.py --apply     # 実際に event_ledger へ書き込む
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent

TICKER = "9432.T"
ACCOUNT = "特定"
SHARES = 100.0
ENTRY_PRICE_JPY = 152.25
# CSV同期日を「取得日不明」の代理値として使用 (実売却日 2026-07-08 より前であることのみ保証)。
OCCURRED_AT = "2026-05-28T00:00:00"
NOTE = (
    "opening balance backfill (2026-07-12 investigation): "
    "楽天CSV保有同期 2026-05-28 で holdings.json に取り込まれた既存保有。"
    "trade_history.csv / event_ledger に対応する BUY 記録なし。"
    "実際の取得日は不明 — occurred_at は CSV 同期日を代理値として使用。"
    "原価はCSV同期スナップショットの entry_price をそのまま採用。"
)


def _make_event_id() -> str:
    raw = f"opening_balance|{TICKER}|{ACCOUNT}|{SHARES}|{ENTRY_PRICE_JPY}|{OCCURRED_AT}"
    return "backfill_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def backfill(*, apply: bool = False, db_path=None) -> dict:
    from tax_lot import build_lots

    event_id = _make_event_id()
    payload = {
        "event_type": "trade",
        "occurred_at": OCCURRED_AT,
        "ticker": TICKER,
        "direction": "buy",
        "quantity": SHARES,
        "price": ENTRY_PRICE_JPY,
        "currency": "JPY",
        "account": ACCOUNT,
        "source": "backfill",
        "note": NOTE,
        "event_id": event_id,
        "db_path": db_path,
        "raw_payload": {
            "reason": "opening_balance_no_trade_history",
            "csv_sync_date": "2026-05-28",
            "true_acquisition_date": "unknown",
        },
    }

    # 事前チェック: 現状は本当に失敗するはずであることを確認 (前提が崩れていないか)
    pre_status = "unknown"
    try:
        build_lots(TICKER, db_path=db_path)
        pre_status = "already_ok_no_backfill_needed"
    except Exception as e:
        pre_status = f"fails_as_expected: {e}"

    if not apply:
        return {"dry_run": True, "would_insert": payload, "pre_check": pre_status}

    from event_ledger import append_event
    result = append_event(**payload)

    # 事後チェック: backfill 後に build_lots が成功するか検証
    post_status = "unknown"
    try:
        build_lots(TICKER, db_path=db_path)
        post_status = "ok"
    except Exception as e:
        post_status = f"still_fails: {e}"

    return {"dry_run": False, "inserted": result, "pre_check": pre_status, "post_check": post_status}


def _main() -> None:
    parser = argparse.ArgumentParser(description="9432.T opening balance backfill")
    parser.add_argument("--apply", action="store_true", help="実際に event_ledger へ書き込む (default: dry-run)")
    args = parser.parse_args()
    result = backfill(apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _main()

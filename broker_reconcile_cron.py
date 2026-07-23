"""
broker_reconcile_cron.py — 週次照合の cron wrapper

役割:
  - 楽天/SBI の取引履歴 CSV (週次で `~/Downloads/broker_csv/` 等に置く) を読み、
    内部 event_ledger と突合する。
  - 不一致 (only_in_broker / only_in_ledger / mismatched) があれば Telegram 通知。
  - clean なら heartbeat だけ更新して quiet exit。

設計判断:
  - 楽天/SBI 実 CSV のフォーマットは broker_reconcile.py の汎用 parser (HEADER_HINTS) で
    現状ある程度処理可能。完全な mapping は実 CSV サンプル待ち。
  - cron からは ~/Downloads/broker_csv/{rakuten,sbi}_*.csv の最新ファイルを自動検出する。
  - CSV が無い週はエラーにせず skip (ユーザーが週次でダウンロードする運用を強制しない)。

使い方:
  python broker_reconcile_cron.py                   # dry-run + print
  python broker_reconcile_cron.py --notify          # 不一致なら Telegram 通知
  python broker_reconcile_cron.py --from 2026-04-01 --to 2026-05-17 --notify

crontab (週次):
  0 9 * * 1 cd ~/portfolio-bot && venv/bin/python broker_reconcile_cron.py --notify >> logs/reconcile.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
DEFAULT_CSV_DIR = Path.home() / "Downloads" / "broker_csv"


def _find_latest_csv(broker: str, csv_dir: Path) -> Optional[Path]:
    """{broker}_*.csv の中で mtime が最新のものを返す。無ければ None。"""
    if not csv_dir.exists():
        return None
    candidates = sorted(
        [
            p for p in csv_dir.glob(f"{broker}_*.csv")
            if p.is_file() and "position" not in p.name.lower()
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _find_latest_position_csv(csv_dir: Path) -> Optional[Path]:
    if not csv_dir.exists():
        return None
    candidates = []
    for pattern in ("assetbalance*.csv", "rakuten_positions_*.csv"):
        candidates.extend(p for p in csv_dir.glob(pattern) if p.is_file())
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def reconcile_tax_cost_basis(position_csv: Path) -> dict:
    from broker_position_import import parse_rakuten_positions
    from broker_reconcile import compare_tax_cost_basis

    report = compare_tax_cost_basis(parse_rakuten_positions(position_csv))
    return {"position_csv": str(position_csv), **report}


def _send_telegram(message: str) -> bool:
    """alert.send_telegram に委譲。失敗時は False を返す (raise しない)。"""
    try:
        from alert import send_telegram
        return bool(send_telegram(message))
    except Exception as e:
        print(f"[broker_reconcile_cron] Telegram 送信失敗: {e}", file=sys.stderr)
        return False


def _format_report(broker: str, report) -> str:
    lines = [f"📊 {broker} reconcile (matched={report.matched_count})"]
    if report.only_in_broker:
        lines.append(f"❌ only_in_broker: {len(report.only_in_broker)} 件")
        for t in report.only_in_broker[:5]:
            lines.append(f"  • {t.get('trade_date','')} {t.get('ticker','')} {t.get('direction','')} {t.get('quantity','')}@{t.get('price','')}")
        if len(report.only_in_broker) > 5:
            lines.append(f"  ... 他 {len(report.only_in_broker) - 5} 件")
    if report.only_in_ledger:
        lines.append(f"❌ only_in_ledger: {len(report.only_in_ledger)} 件")
        for t in report.only_in_ledger[:5]:
            lines.append(f"  • {t.get('trade_date','')} {t.get('ticker','')} {t.get('direction','')} {t.get('quantity','')}@{t.get('price','')}")
    if report.mismatched:
        lines.append(f"⚠️ mismatched: {len(report.mismatched)} 件")
        for m in report.mismatched[:5]:
            b = m.broker_trade
            lines.append(f"  • {b.get('trade_date','')} {b.get('ticker','')}: {' / '.join(m.differences[:2])}")
    return "\n".join(lines)


def _format_parse_skips(broker: str, parse_report) -> str:
    lines = [
        f"⚠️ {broker} reconcile parse skipped: {parse_report.skipped} "
        f"/ {parse_report.rows_total} rows"
    ]
    for reason in parse_report.skip_reasons[:5]:
        lines.append(f"  • row {reason.get('row')}: {reason.get('reason')}")
    if parse_report.skipped > 5:
        lines.append(f"  ... 他 {parse_report.skipped - 5} 件")
    return "\n".join(lines)


def reconcile_broker(
    broker: str,
    csv_path: Path,
    *,
    date_from: str,
    date_to: str,
    notify: bool = False,
) -> dict:
    """1 broker の CSV を内部 ledger と突合する。"""
    # Codex re-review #12: 定期経路も parse 件数/skip 可視化 (ParseReport) と broker scope を効かせる。
    from broker_reconcile import parse_csv_with_report, compare_to_ledger

    trades, parse_report = parse_csv_with_report(csv_path, broker)
    report = compare_to_ledger(trades, date_from=date_from, date_to=date_to, broker=broker)
    summary = {
        "broker":           broker,
        "csv":              str(csv_path),
        "trade_count":      len(trades),
        "parsed":           parse_report.parsed,
        "skipped":          parse_report.skipped,
        "skip_reasons":     parse_report.skip_reasons[:10],
        "scope":            report.scope,
        "matched_count":    report.matched_count,
        "only_in_broker":   len(report.only_in_broker),
        "only_in_ledger":   len(report.only_in_ledger),
        "mismatched":       len(report.mismatched),
        # parse skip も差分扱いにして通知漏れを防ぐ
        "has_discrepancy":  report.has_discrepancy or parse_report.skipped > 0,
    }

    if summary["has_discrepancy"] and notify:
        parts = []
        if parse_report.skipped > 0:
            parts.append(_format_parse_skips(broker, parse_report))
        if report.has_discrepancy:
            parts.append(_format_report(broker, report))
        message = "\n\n".join(parts)
        # ALMANAC: telegram disabled — ai_analysis only
        # ok = _send_telegram(message)
        ok = False
        summary["telegram_notified"] = ok

    return summary


def _main() -> None:
    parser = argparse.ArgumentParser(description="Weekly broker reconcile cron wrapper")
    parser.add_argument("--csv-dir", default=str(DEFAULT_CSV_DIR),
                        help=f"CSV 配置ディレクトリ (default: {DEFAULT_CSV_DIR})")
    parser.add_argument("--from", dest="date_from", default=None,
                        help="YYYY-MM-DD (default: 7 日前)")
    parser.add_argument("--to",   dest="date_to",   default=None,
                        help="YYYY-MM-DD (default: 今日)")
    parser.add_argument("--brokers", default="rakuten,sbi", help="カンマ区切り")
    parser.add_argument("--notify", action="store_true",
                        help="不一致時に Telegram 通知")
    parser.add_argument("--cost-basis-only", action="store_true",
                        help="楽天保有 CSV の取得単価照合だけを実行")
    args = parser.parse_args()

    today = date.today()
    date_to   = args.date_to   or today.isoformat()
    date_from = args.date_from or (today - timedelta(days=7)).isoformat()
    csv_dir = Path(args.csv_dir)

    results = []
    if not args.cost_basis_only:
        for broker in [b.strip() for b in args.brokers.split(",") if b.strip()]:
            latest = _find_latest_csv(broker, csv_dir)
            if latest is None:
                results.append({"broker": broker, "skipped": "no CSV found"})
                continue
            try:
                r = reconcile_broker(
                    broker, latest,
                    date_from=date_from, date_to=date_to, notify=args.notify,
                )
                results.append(r)
            except Exception as e:
                err = {"broker": broker, "csv": str(latest), "error": str(e)}
                results.append(err)
                if args.notify:
                    # ALMANAC: telegram disabled — ai_analysis only
                    # _send_telegram(f"broker_reconcile_cron {broker}: {e}")
                    pass

    position_csv = _find_latest_position_csv(csv_dir)
    if position_csv is None:
        results.append({"scope": "rakuten_taxable_cost_basis", "skipped": "no position CSV found"})
    else:
        try:
            cost_report = reconcile_tax_cost_basis(position_csv)
            results.append(cost_report)
            if cost_report["has_discrepancy"] and args.notify:
                # ALMANAC: telegram disabled — ai_analysis only
                # _send_telegram(
                #     "Rakuten tax cost basis mismatch: "
                #     f"{len(cost_report['discrepancies'])} discrepancies / "
                #     f"{len(cost_report['missing_internal'])} missing internal"
                # )
                pass
        except Exception as e:
            results.append({"scope": "rakuten_taxable_cost_basis", "error": str(e)})

    print(json.dumps({
        "date_from": date_from,
        "date_to":   date_to,
        "results":   results,
    }, ensure_ascii=False, indent=2, default=str))

    # heartbeat
    try:
        from utils import heartbeat
        ok = all(("error" not in r and not r.get("has_discrepancy", False)) for r in results)
        heartbeat("broker_reconcile_cron", "ok" if ok else "warn",
                  extra={"brokers": [r.get("broker") for r in results]})
    except Exception:
        pass


if __name__ == "__main__":
    _main()

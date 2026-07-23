"""
portfolio_integrity.py — 内部台帳の整合性監査

SQLite への全面移行前でも、account / holdings / cash_transactions /
action_executions / event_ledger のズレを検知するための fail-loud な監査層。

目的:
  - account.json と holdings の現金ミラー不一致を検出
  - cash_transactions.json の入出金が event_ledger に無い状態を検出
  - portfolio_applied 済み execution の ledger event 欠落を検出
  - event_ledger の amount_jpy 欠落や符号異常を検出

使い方:
  python portfolio_integrity.py check
  python portfolio_integrity.py check --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from almanac.runtime_config import resolve_db_path
from utils import load_json_strict

BASE_DIR = Path(__file__).parent

CASH_MIRRORS = (
    ("account.balance", "CASH_JPY", "shares", 1.0),
    ("account.usd_balance", "CASH_USD", "shares", 0.01),
)

AMOUNT_REQUIRED_EVENT_TYPES = {
    "trade",
    "cash_flow",
    "dividend",
    "tax",
    "fee",
    "fx_conversion",
}

TRADE_NOTIONAL_CRITICAL_JPY = 20_000_000
DOMESTIC_FUND_PREFIXES = ("SLIM_", "IFREE_", "NOMURA_", "MNXACT")


def _issue(severity: str, check: str, message: str, **extra) -> dict:
    return {"severity": severity, "check": check, "message": message, **extra}


def _load_dict(path: Path, label: str, issues: list[dict]) -> dict:
    try:
        data = load_json_strict(path)
    except Exception as e:
        issues.append(_issue("critical", "json_load", f"{label} の読み込みに失敗: {e}", file=str(path)))
        return {}
    if not isinstance(data, dict):
        issues.append(_issue("critical", "json_schema", f"{label} が dict ではありません", file=str(path)))
        return {}
    return data


def _ledger_rows_by_event_id(db_path: Path) -> dict[str, dict]:
    from event_ledger import init_schema

    init_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM ledger_events").fetchall()
    finally:
        conn.close()
    return {str(r["event_id"]): dict(r) for r in rows}


def _check_cash_mirror(account: dict, holdings: dict, issues: list[dict]) -> None:
    for account_key, holding_key, holding_field, tolerance in CASH_MIRRORS:
        _, key = account_key.split(".", 1)
        expected = float(account.get(key, 0) or 0)
        h = holdings.get(holding_key)
        if not isinstance(h, dict):
            issues.append(_issue("critical", "cash_mirror", f"holdings に {holding_key} がありません"))
            continue
        actual = float(h.get(holding_field, 0) or 0)
        if abs(expected - actual) > tolerance:
            issues.append(_issue(
                "critical",
                "cash_mirror",
                f"{account_key} と holdings.{holding_key}.{holding_field} が不一致",
                expected=expected,
                actual=actual,
                tolerance=tolerance,
            ))


def _check_account_cash_derived_totals(account: dict, issues: list[dict]) -> None:
    derived_keys = ("jpy_equivalent_usd", "total_cash")
    if not any(key in account for key in derived_keys):
        return
    try:
        jpy = float(account.get("balance", 0) or 0)
        usd = float(account.get("usd_balance", 0) or 0)
        fx = float(account.get("fx_rate_usdjpy", 0) or 0)
    except (TypeError, ValueError):
        issues.append(_issue(
            "critical",
            "account_cash_numeric",
            "account.json の現金・FXフィールドが数値として解釈できません",
        ))
        return
    if fx <= 0:
        issues.append(_issue(
            "critical",
            "account_fx_rate",
            "account.fx_rate_usdjpy が正の数ではありません",
            fx_rate_usdjpy=account.get("fx_rate_usdjpy"),
        ))
        return

    expected_usd_jpy = int(round(usd * fx))
    if "jpy_equivalent_usd" in account:
        actual = float(account.get("jpy_equivalent_usd") or 0)
        if abs(actual - expected_usd_jpy) > 1:
            issues.append(_issue(
                "critical",
                "account_jpy_equivalent_usd",
                "account.jpy_equivalent_usd が usd_balance × fx_rate_usdjpy と不一致",
                expected=expected_usd_jpy,
                actual=actual,
                usd_balance=usd,
                fx_rate_usdjpy=fx,
            ))

    expected_total = int(round(jpy + usd * fx))
    if "total_cash" in account:
        actual = float(account.get("total_cash") or 0)
        if abs(actual - expected_total) > 1:
            issues.append(_issue(
                "critical",
                "account_total_cash",
                "account.total_cash が balance + usd_balance × fx_rate_usdjpy と不一致",
                expected=expected_total,
                actual=actual,
                balance=jpy,
                usd_balance=usd,
                fx_rate_usdjpy=fx,
            ))


def _check_cash_tx_ledger_links(cash_tx: dict, ledger_by_event_id: dict[str, dict], issues: list[dict]) -> None:
    txs = cash_tx.get("transactions", [])
    if not isinstance(txs, list):
        issues.append(_issue("critical", "cash_transactions_schema", "transactions が list ではありません"))
        return
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        tx_id = tx.get("id")
        if not tx_id:
            issues.append(_issue("high", "cash_tx_id", "cash transaction に id がありません", transaction=tx))
            continue
        ev = ledger_by_event_id.get(str(tx_id))
        if ev is None:
            issues.append(_issue("high", "cash_tx_ledger_link", "cash transaction に対応する ledger event がありません", tx_id=tx_id))
            continue
        if ev.get("event_type") != "cash_flow":
            issues.append(_issue(
                "critical",
                "cash_tx_ledger_type",
                "cash transaction の event_id が cash_flow 以外を指しています",
                tx_id=tx_id,
                event_type=ev.get("event_type"),
            ))


def _execution_is_externally_reconciled(rec: dict) -> bool:
    """Broker CSV 等の外部正本で holdings/account 反映済みを確認した実行記録。"""
    return bool(
        rec.get("externally_reconciled")
        or rec.get("portfolio_reconciled")
        or rec.get("portfolio_reconciled_externally")
        or rec.get("portfolio_application_status") == "externally_reconciled"
    )


def _execution_reconcile_source(rec: dict) -> str:
    return str(
        rec.get("external_reconcile_source")
        or rec.get("portfolio_reconcile_source")
        or rec.get("reconcile_source")
        or ""
    ).strip()


def _execution_is_applied(rec: dict) -> bool:
    return bool(
        rec.get("portfolio_applied")
        or rec.get("portfolio_updated")
        or _execution_is_externally_reconciled(rec)
    )


def _is_legacy_applied_without_event_id(rec: dict) -> bool:
    """event_ledger 導入前に portfolio_updated=true で反映済みになった旧レコード。"""
    return (
        bool(rec.get("portfolio_updated"))
        and not rec.get("event_id")
        and not rec.get("portfolio_applied_at")
    )


def _check_execution_ledger_links(executions: dict, ledger_by_event_id: dict[str, dict], issues: list[dict], summary: dict) -> None:
    records = executions.get("executions", [])
    if not isinstance(records, list):
        issues.append(_issue("critical", "executions_schema", "executions が list ではありません"))
        return

    legacy_unlinked = 0
    unapplied_executed = 0
    pending_application = 0
    externally_reconciled = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "").lower()
        if status not in {"executed", "partial", "filled", "done"}:
            continue
        if not _execution_is_applied(rec):
            if (
                rec.get("portfolio_application_status") == "pending"
                or rec.get("portfolio_application_pending") is True
            ):
                pending_application += 1
                issues.append(_issue(
                    "advisory",
                    "execution_portfolio_application_pending",
                    "約定事実は保存済みですが、ポートフォリオ適用先の解決待ちです",
                    execution_id=rec.get("id"),
                    ticker=rec.get("ticker"),
                    direction=rec.get("direction"),
                    reasons=rec.get("portfolio_application_reasons") or [],
                    candidate_position_keys=rec.get("candidate_position_keys") or [],
                    saved_at=rec.get("saved_at"),
                ))
                continue
            unapplied_executed += 1
            issues.append(_issue(
                "critical",
                "execution_portfolio_not_applied",
                "executed の実行記録が holdings/account に未反映です",
                execution_id=rec.get("id"),
                ticker=rec.get("ticker"),
                direction=rec.get("direction"),
                quantity=rec.get("quantity"),
                price=rec.get("price"),
                saved_at=rec.get("saved_at"),
            ))
            continue

        event_id = rec.get("event_id")

        if not event_id and _execution_is_externally_reconciled(rec):
            source = _execution_reconcile_source(rec)
            if not source:
                issues.append(_issue(
                    "high",
                    "execution_external_reconcile_source_missing",
                    "外部照合済み execution に reconcile source がありません",
                    execution_id=rec.get("id"),
                ))
            else:
                externally_reconciled += 1
            continue

        if not event_id and _is_legacy_applied_without_event_id(rec):
            legacy_unlinked += 1
            continue
        if not event_id:
            exec_id = rec.get("id")
            issues.append(_issue(
                "high",
                "execution_event_id_missing",
                "portfolio_applied 済み execution に event_id がありません",
                execution_id=exec_id,
                expected_event_id=(f"exec_{exec_id}" if exec_id else None),
            ))
            continue

        ev = ledger_by_event_id.get(str(event_id))
        if ev is None:
            issues.append(_issue(
                "critical",
                "execution_ledger_missing",
                "portfolio_applied 済み execution の ledger event がありません",
                execution_id=rec.get("id"),
                event_id=event_id,
            ))
            continue
        if ev.get("event_type") != "trade":
            issues.append(_issue(
                "critical",
                "execution_ledger_type",
                "execution の event_id が trade 以外を指しています",
                execution_id=rec.get("id"),
                event_id=event_id,
                event_type=ev.get("event_type"),
            ))

    summary["legacy_executions_without_event_id"] = legacy_unlinked
    summary["unapplied_executed_count"] = unapplied_executed
    summary["pending_portfolio_application_count"] = pending_application
    summary["externally_reconciled_executions"] = externally_reconciled


def _check_ledger_amounts(db_path: Path, issues: list[dict]) -> None:
    from event_ledger import init_schema

    init_schema(db_path)
    placeholders = ",".join("?" for _ in AMOUNT_REQUIRED_EVENT_TYPES)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Codex P1 #7: quantity/price IS NOT NULL の絞り込みは event_ledger と同じ盲点。
        # quantity/price が欠落した amount 必須 event (amount_jpy=NULL) を見逃すため、
        # amount_jpy IS NULL のみで検出する。
        missing = conn.execute(
            f"""
            SELECT event_id, event_type, ticker, direction, currency
              FROM ledger_events
             WHERE event_type IN ({placeholders})
               AND amount_jpy IS NULL
            """,
            tuple(AMOUNT_REQUIRED_EVENT_TYPES),
        ).fetchall()
        sign_bad = conn.execute(
            """
            SELECT event_id, event_type, direction, amount_jpy
              FROM ledger_events
             WHERE amount_jpy IS NOT NULL
               AND (
                    (direction IN ('buy', 'margin_buy', 'cover', 'out') AND amount_jpy > 0)
                 OR (direction IN ('sell', 'short', 'in') AND amount_jpy < 0)
               )
            """
        ).fetchall()
        notional_outliers = conn.execute(
            """
            SELECT event_id, event_type, ticker, direction, quantity, price,
                   currency, fx_rate_usdjpy, amount_jpy, source
              FROM ledger_events
             WHERE event_type = 'trade'
               AND amount_jpy IS NOT NULL
               AND ABS(amount_jpy) > ?
            """,
            (TRADE_NOTIONAL_CRITICAL_JPY,),
        ).fetchall()
        fund_price_not_normalized = conn.execute(
            """
            SELECT event_id, ticker, direction, quantity, price, currency,
                   amount_jpy, source
              FROM ledger_events
             WHERE event_type = 'trade'
               AND price IS NOT NULL
               AND price >= 100
               AND (
                    ticker LIKE 'SLIM\\_%' ESCAPE '\\'
                 OR ticker LIKE 'IFREE\\_%' ESCAPE '\\'
                 OR ticker LIKE 'NOMURA\\_%' ESCAPE '\\'
                 OR ticker LIKE 'MNXACT%'
               )
            """
        ).fetchall()
        # Codex re-review #3: 訂正イベント (raw_payload.supersedes) に置換された旧行は
        # active-event view から外れる。integrity も同じ view で見ないと、修正済みの旧行
        # (例: FX reprice 前の暫定符号) を critical 検出して policy を不要にブロックする。
        superseded_rows = conn.execute(
            "SELECT raw_payload FROM ledger_events WHERE raw_payload LIKE '%supersedes%'"
        ).fetchall()
    finally:
        conn.close()

    superseded: set = set()
    for r in superseded_rows:
        rp = r["raw_payload"]
        if not rp:
            continue
        try:
            sup = json.loads(rp).get("supersedes")
        except Exception:
            sup = None
        if sup:
            superseded.add(sup)

    for row in missing:
        if row["event_id"] in superseded:
            continue
        issues.append(_issue(
            "critical",
            "ledger_amount_missing",
            "JPY 換算必須 event の amount_jpy が NULL です",
            **dict(row),
        ))
    for row in sign_bad:
        if row["event_id"] in superseded:
            continue
        issues.append(_issue(
            "critical",
            "ledger_amount_sign",
            "ledger amount_jpy の符号が direction と矛盾しています",
            **dict(row),
        ))
    for row in notional_outliers:
        if row["event_id"] in superseded:
            continue
        issues.append(_issue(
            "critical",
            "ledger_trade_notional_outlier",
            "trade event の JPY notional が監査上限を超えています",
            threshold_jpy=TRADE_NOTIONAL_CRITICAL_JPY,
            **dict(row),
        ))
    for row in fund_price_not_normalized:
        if row["event_id"] in superseded:
            continue
        issues.append(_issue(
            "critical",
            "ledger_fund_price_not_normalized",
            "国内投信 trade の価格が 1万口NAVのまま保存されている可能性があります",
            expected="ledger price should be NAV / 10000",
            **dict(row),
        ))


def run_integrity_check(
    *,
    base_dir: Path = BASE_DIR,
    db_path: Optional[Path] = None,
) -> dict:
    """内部台帳の整合性を検査し、issue list と summary を返す。"""
    base_dir = Path(base_dir)
    db = Path(db_path) if db_path is not None else resolve_db_path(base_dir)
    issues: list[dict] = []
    summary: dict = {}

    account = _load_dict(base_dir / "account.json", "account.json", issues)
    holdings = _load_dict(base_dir / "holdings.json", "holdings.json", issues)
    cash_tx = _load_dict(base_dir / "cash_transactions.json", "cash_transactions.json", issues)
    executions = _load_dict(base_dir / "action_executions.json", "action_executions.json", issues)

    ledger_by_event_id = _ledger_rows_by_event_id(db)
    summary["ledger_event_count"] = len(ledger_by_event_id)

    if account and holdings:
        _check_cash_mirror(account, holdings, issues)
    if account:
        _check_account_cash_derived_totals(account, issues)
    if cash_tx:
        _check_cash_tx_ledger_links(cash_tx, ledger_by_event_id, issues)
    if executions:
        _check_execution_ledger_links(executions, ledger_by_event_id, issues, summary)
    _check_ledger_amounts(db, issues)

    blocking = [i for i in issues if i["severity"] in {"critical", "high"}]
    return {
        "ok": len(blocking) == 0,
        "issue_count": len(issues),
        "blocking_issue_count": len(blocking),
        "issues": issues,
        "summary": summary,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="ALMANAC portfolio integrity checker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check")
    check.add_argument("--json", action="store_true", help="JSON で出力")
    args = parser.parse_args()

    if args.cmd == "check":
        result = run_integrity_check()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[portfolio_integrity] ok={result['ok']} issues={result['issue_count']}")
            for issue in result["issues"]:
                print(f"- [{issue['severity']}] {issue['check']}: {issue['message']}")
        sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    _main()

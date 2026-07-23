"""Scheduled, human-in-the-loop tax-loss harvesting scanner."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import action_state_tracker as ast
from almanac.observability.append_only_log import append_jsonl_safe
from tax_lot import portfolio_lot_snapshot, recommend_sell_lots
from tax_optimizer import (
    TAX_IPPAN,
    TAX_TOKUTEI,
    _load_substitutes,
    _lookup_substitutes,
)

TAX_HARVEST_ACTION_TYPE = "loss_harvest_sell"

BASE_DIR = Path(__file__).parent
DEFAULT_REPORT_PATH = BASE_DIR / "data" / "tax_harvest_reports.jsonl"
REPURCHASE_WARNING = (
    "同日中に同一銘柄を買い戻すと、特定口座では一日の取引終了後に総平均法に準ずる方法で"
    "取得価額が再計算され、想定した損失確定額が小さくなる場合があります。"
    "買い戻しは翌営業日以降を推奨します。"
)


def _default_price_provider(ticker: str, currency: str) -> tuple[Optional[float], Optional[float]]:
    from portfolio_manager import get_current_price

    price = get_current_price(ticker, currency)
    fx = None
    if currency.upper() == "USD":
        from utils import get_fx_rate_cached

        fx, _ = get_fx_rate_cached()
    return price, fx


def scan_tax_harvest(
    *,
    min_loss_jpy: float = 30_000,
    lots_snapshot: Optional[dict] = None,
    price_provider: Optional[
        Callable[[str, str], tuple[Optional[float], Optional[float]]]
    ] = None,
    recommend_func: Callable[..., dict] = recommend_sell_lots,
    db_path: Optional[Path] = None,
) -> dict:
    """Return tax-loss candidates. No orders are created or submitted."""

    snapshot = lots_snapshot if lots_snapshot is not None else portfolio_lot_snapshot(db_path=db_path)
    provider = price_provider or _default_price_provider
    substitutes = _load_substitutes()
    candidates: list[dict] = []

    for ticker, lots in (snapshot.get("lots") or {}).items():
        if not lots:
            continue
        currency = str(lots[0].get("currency") or ("JPY" if ticker.endswith(".T") else "USD"))
        current_price, fx = provider(ticker, currency)
        if current_price is None or current_price <= 0:
            continue
        current_jpy = current_price * (fx or 1.0) if currency.upper() == "USD" else current_price
        accounts = sorted({lot.get("account") for lot in lots}, key=str)
        for account in accounts:
            if not account or "NISA" in str(account):
                continue
            account_lots = [lot for lot in lots if lot.get("account") == account]
            losing = [
                lot for lot in account_lots
                if current_jpy < float(lot.get("cost_per_share_jpy") or 0)
            ]
            quantity = sum(float(lot.get("remaining_qty") or 0) for lot in losing)
            estimated_loss = sum(
                float(lot.get("remaining_qty") or 0)
                * (current_jpy - float(lot.get("cost_per_share_jpy") or 0))
                for lot in losing
            )
            if quantity <= 0 or estimated_loss > -abs(min_loss_jpy):
                continue
            plan = recommend_func(
                ticker,
                quantity,
                current_price=current_price,
                current_fx=fx,
                currency=currency,
                mode="loss_harvest",
                account_filter=account,
                db_path=db_path,
            )
            tax_rate = TAX_TOKUTEI if "特定" in str(account) else TAX_IPPAN
            is_japan = ticker.endswith(".T")
            estimated_tax_saving_jpy = round(abs(estimated_loss) * tax_rate)

            # 候補ごとに action_state_tracker へ登録し、recommendation_id を発行する。
            # 既存の pending/filled/cancelled/expired ライフサイクル・dedup をそのまま流用し、
            # 損出し専用の別台帳は作らない。
            act_for_tracking = {
                "ticker": ticker,
                "type": TAX_HARVEST_ACTION_TYPE,
                "urgency": "medium" if abs(estimated_loss) >= min_loss_jpy * 3 else "low",
                "action": f"損出し候補: {ticker} {round(quantity, 2)}株 ({account})",
                "reason": f"含み損¥{estimated_loss:,.0f}・推定節税¥{estimated_tax_saving_jpy:,.0f}",
                "account": account,
            }
            ast.record_recommendations([act_for_tracking], source="tax_harvest_scanner")
            bucket = ast.account_bucket_for_action(act_for_tracking)
            recommendation_id = ast._make_id(
                ticker, TAX_HARVEST_ACTION_TYPE, datetime.now().isoformat(), bucket,
            )

            candidates.append({
                "ticker": ticker,
                "account": account,
                "quantity": round(quantity, 6),
                "current_price": current_price,
                "currency": currency,
                "estimated_loss_jpy": round(estimated_loss),
                "estimated_tax_saving_jpy": estimated_tax_saving_jpy,
                "substitutes": _lookup_substitutes(ticker, is_japan, substitutes)[:3],
                "lot_plan": plan.get("plan", []),
                "human_execution_only": True,
                "recommendation_id": recommendation_id,
            })

    candidates.sort(key=lambda row: row["estimated_loss_jpy"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "total_estimated_loss_jpy": sum(row["estimated_loss_jpy"] for row in candidates),
        "total_estimated_tax_saving_jpy": sum(
            row["estimated_tax_saving_jpy"] for row in candidates
        ),
        "warning": REPURCHASE_WARNING,
        "execution": "display_and_notify_only",
    }


def format_telegram(report: dict) -> str:
    if not report.get("candidates"):
        return "損出し定期スキャン: 対象候補はありません。"
    lines = [
        "損出し候補（確認・手動実行のみ）",
        f"推定節税余地: ¥{report['total_estimated_tax_saving_jpy']:,.0f}",
    ]
    for row in report["candidates"][:8]:
        subs = ", ".join(row.get("substitutes") or []) or "代替なし"
        lines.append(
            f"{row['ticker']} {row['account']}: 損失 ¥{row['estimated_loss_jpy']:,.0f} / "
            f"代替 {subs}"
        )
    lines.append(REPURCHASE_WARNING)
    return "\n".join(lines)


def run_scan(
    *,
    report_path: Path | str = DEFAULT_REPORT_PATH,
    notify: bool = False,
    send: Optional[Callable[[str], None]] = None,
    **scan_kwargs,
) -> dict:
    report = scan_tax_harvest(**scan_kwargs)
    append_jsonl_safe(Path(report_path), report)
    if notify:
        sender = send
        if sender is None:
            from telegram_bot import _send

            sender = _send
        sender(format_telegram(report))
    return report


def main(argv: Optional[list[str]] = None) -> dict:
    parser = argparse.ArgumentParser(description="損出し候補を表示・通知する（自動執行なし）")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--min-loss", type=float, default=30_000)
    args = parser.parse_args(argv)
    report = run_scan(notify=args.notify, min_loss_jpy=args.min_loss)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    main()

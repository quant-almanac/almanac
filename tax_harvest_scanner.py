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


def compute_tax_harvest(
    *,
    min_loss_jpy: float = 30_000,
    lots_snapshot: Optional[dict] = None,
    price_provider: Optional[
        Callable[[str, str], tuple[Optional[float], Optional[float]]]
    ] = None,
    recommend_func: Callable[..., dict] = recommend_sell_lots,
    db_path: Optional[Path] = None,
) -> dict:
    """純粋: 損出し候補を計算するだけ。action_state への登録・通知・
    ファイル永続化のいずれも行わない (Stage 5A の副作用分離)。

    候補には recommendation_id がまだ付いていない — register_actions() が
    action_state_tracker への登録と同時に付与する。
    """

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

            candidates.append({
                "ticker": ticker,
                "account": account,
                "quantity": round(quantity, 6),
                "current_price": current_price,
                "currency": currency,
                "estimated_loss_jpy": round(estimated_loss),
                "estimated_tax_saving_jpy": estimated_tax_saving_jpy,
                "urgency": "medium" if abs(estimated_loss) >= min_loss_jpy * 3 else "low",
                "substitutes": _lookup_substitutes(ticker, is_japan, substitutes)[:3],
                "lot_plan": plan.get("plan", []),
                "human_execution_only": True,
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


def register_actions(report: dict) -> dict:
    """report の各 candidate を action_state_tracker へ登録し、
    recommendation_id を付与した新しい report を返す (元の report は
    変更しない — 副作用はこの関数1箇所に集約する)。

    既存の pending/filled/cancelled/expired ライフサイクル・dedup を
    そのまま流用し、損出し専用の別台帳は作らない。
    """
    import copy

    report = copy.deepcopy(report)
    for row in report.get("candidates", []):
        act_for_tracking = {
            "ticker": row["ticker"],
            "type": TAX_HARVEST_ACTION_TYPE,
            "urgency": row.get("urgency", "low"),
            "action": f"損出し候補: {row['ticker']} {round(row['quantity'], 2)}株 ({row['account']})",
            "reason": (
                f"含み損¥{row['estimated_loss_jpy']:,.0f}・"
                f"推定節税¥{row['estimated_tax_saving_jpy']:,.0f}"
            ),
            "account": row["account"],
        }
        ast.record_recommendations([act_for_tracking], source="tax_harvest_scanner")
        bucket = ast.account_bucket_for_action(act_for_tracking)
        row["recommendation_id"] = ast._make_id(
            row["ticker"], TAX_HARVEST_ACTION_TYPE, datetime.now().isoformat(), bucket,
        )
    return report


def scan_tax_harvest(**kwargs) -> dict:
    """後方互換 wrapper: compute_tax_harvest() + register_actions() を1回で行う。

    既存の呼び出し元 (cron 経由の main()、既存テスト) は
    「計算すると同時に action_state へ登録される」動作に依存しているため、
    この関数のシグネチャ・副作用は変更しない。新しい呼び出し元は
    compute_tax_harvest() だけを呼んで登録タイミングを自分で制御できる。
    """
    report = compute_tax_harvest(**kwargs)
    return register_actions(report)


def publish_report(report: dict, *, report_path: Path | str = DEFAULT_REPORT_PATH) -> None:
    """report を JSONL へ追記する。副作用はこれだけに限定する。"""
    append_jsonl_safe(Path(report_path), report)


def send_notification(report: dict, *, send: Optional[Callable[[str], None]] = None) -> None:
    """report を Telegram へ送る。副作用はこれだけに限定する。"""
    sender = send
    if sender is None:
        from telegram_bot import _send

        sender = _send
    sender(format_telegram(report))


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
    """既存の cron 呼び出し (main() 経由、--notify 付き) を変えない
    thin wrapper。実体は compute→register→publish→notify の4ステップに
    分離済み (Stage 5A)。"""
    report = scan_tax_harvest(**scan_kwargs)
    publish_report(report, report_path=report_path)
    if notify:
        send_notification(report, send=send)
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

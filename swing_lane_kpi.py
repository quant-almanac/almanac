"""swing_lane_kpi.py — Swingレーンのクローズ済みトレードKPI集計。

攻めバックログ 2026-07 項目5(前半)。investment_type=='swing' として建てた
クローズ済みトレードの 勝率/profit factor/平均保有日数/期待値/最大単発損失 を
集計し、サイズ昇格ラダー(tier0→tier1→tier2)の判定材料にする。

既知の制約 (2026-07-12 investigation, 9432.T backfillと同種のギャップ):
  investment_type は holdings.json の「現在保有」スナップショットにしか残らず、
  ポジションを全数決済すると holdings.json からエントリごと消える。
  event_ledger / action_state のどちらにも investment_type フィールドは
  無いため、クローズ済みトレードをレーン別に自動判定する手段が無い。
  このため対象ティッカーは明示リストで手動管理する
  (新しい swing トレードを建てたら SWING_TICKERS に追記が必要)。

実際にtierを上げ下げする操作は本モジュールの責務外 — ここはKPI計測と
判定材料の提示のみ行い、人間が (例: tunable_params.json 経由で) 反映する。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

# 手動管理: 2026-07-12時点で判明している全swingトレード (TXN/ANETは両方クローズ済み)。
SWING_TICKERS = frozenset({"TXN", "ANET"})

MIN_CLOSED_TRADES_FOR_VERDICT = 20
MIN_PROFIT_FACTOR_PROMOTE = 1.3
MAX_PROFIT_FACTOR_DEMOTE = 0.8

# サイズ昇格ラダー (バックログ案): tier0=現状 → tier1 → tier2 (JPシナリオ上限と同額で頭打ち)
SIZE_LADDER_JPY = (100_000, 250_000, 500_000)


def _lot_purchase_date_by_id(lots) -> dict[str, str]:
    return {lot.lot_id: lot.purchase_date for lot in lots}


def compute_swing_kpis(
    *,
    tickers: Optional[frozenset] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """SWING_TICKERS (または明示指定) のクローズ済みトレードからKPIを集計する。"""
    from tax_lot import build_lots

    target_tickers = tickers if tickers is not None else SWING_TICKERS
    trades: list[dict] = []

    for ticker in sorted(target_tickers):
        try:
            state = build_lots(ticker, db_path=db_path)
        except Exception:
            continue
        purchase_dates = _lot_purchase_date_by_id(state.open_lots)
        for rt in state.realized_trades:
            purchase_date = purchase_dates.get(rt.lot_id)
            hold_days = None
            if purchase_date:
                try:
                    hold_days = (
                        date.fromisoformat(rt.sell_date) - date.fromisoformat(purchase_date)
                    ).days
                except Exception:
                    hold_days = None
            trades.append({
                "ticker": rt.ticker,
                "sell_date": rt.sell_date,
                "realized_jpy": rt.realized_jpy,
                "cost_basis_jpy": rt.cost_basis_jpy,
                "hold_days": hold_days,
            })

    n = len(trades)
    if n == 0:
        return {
            "n_closed": 0,
            "verdict": "insufficient_data",
            "reason": "クローズ済みswingトレードが0件",
            "size_ladder_jpy": list(SIZE_LADDER_JPY),
        }

    wins = [t for t in trades if t["realized_jpy"] > 0]
    losses = [t for t in trades if t["realized_jpy"] < 0]
    win_rate = len(wins) / n
    gross_win = sum(t["realized_jpy"] for t in wins)
    gross_loss = abs(sum(t["realized_jpy"] for t in losses))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = None  # 損失0件は profit factor 未定義 (無限大を騙るより誠実)

    hold_days_values = [t["hold_days"] for t in trades if t["hold_days"] is not None]
    avg_hold_days = (sum(hold_days_values) / len(hold_days_values)) if hold_days_values else None
    expected_value_jpy = sum(t["realized_jpy"] for t in trades) / n
    # 損失トレードが無い場合は 0 (「まだ損失なし」) — 最小の勝ちトレードを損失と誤表示しない
    max_single_loss_jpy = min((t["realized_jpy"] for t in losses), default=0)

    if n < MIN_CLOSED_TRADES_FOR_VERDICT:
        verdict = "insufficient_data"
        reason = f"n_closed={n} < {MIN_CLOSED_TRADES_FOR_VERDICT}"
    elif profit_factor is not None and profit_factor >= MIN_PROFIT_FACTOR_PROMOTE and expected_value_jpy > 0:
        verdict = "promote"
        reason = f"profit_factor={profit_factor:.2f} >= {MIN_PROFIT_FACTOR_PROMOTE}, EV=¥{expected_value_jpy:,.0f}"
    elif profit_factor is not None and profit_factor < MAX_PROFIT_FACTOR_DEMOTE:
        verdict = "demote"
        reason = f"profit_factor={profit_factor:.2f} < {MAX_PROFIT_FACTOR_DEMOTE}"
    else:
        verdict = "maintain"
        reason = f"profit_factor={profit_factor}" if profit_factor is not None else "損失トレード0件で判定不能"

    return {
        "n_closed": n,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "avg_hold_days": round(avg_hold_days, 1) if avg_hold_days is not None else None,
        "expected_value_jpy": round(expected_value_jpy),
        "max_single_loss_jpy": round(max_single_loss_jpy),
        "verdict": verdict,
        "reason": reason,
        "size_ladder_jpy": list(SIZE_LADDER_JPY),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(compute_swing_kpis(), ensure_ascii=False, indent=2))

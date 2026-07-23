"""
rebalance_planner: 集約リバランスプラン生成ユーティリティ。

LLM が「毎日 1 株ずつ」推奨で細切れ発注になるのを防ぐため、
コード側で「総株数 / 何回に分割 / 1 回あたり株数」を一括計算する。

Phase 2 (2026-04-28) — ALMANAC 細かいリバランス抑制プラン B-2 / 項目 5。
"""
from __future__ import annotations

import math
from typing import Literal

from instrument_metadata import quantity_label_for_ticker, trading_unit_for_ticker


# 手数料効率の目安（SBI 海外株式: 0.495%, max $22 = 約 ¥3,300）
# $4,400 (≒ ¥660,000) 以上にロットを集約すれば手数料率が頭打ちになる。
US_FEE_CAP_JPY = 660_000

def _is_jp(ticker: str) -> bool:
    return ticker.endswith(".T")


def _is_fund(ticker: str) -> bool:
    return ticker in {"SLIM_SP500", "SLIM_ORCAN", "MNXACT",
                      "IFREE_FANGPLUS", "NOMURA_SEMI"}


def compute_full_trim_plan(
    ticker:      str,
    current_pct: float,
    target_pct:  float,
    total_jpy:   float,
    share_price: float,
    currency:    Literal["JPY", "USD"] = "USD",
    fx_rate:     float = 150.0,
    max_splits:  int   = 2,
) -> dict:
    """
    現在比率 → 目標比率まで動かすために必要な「総株数 / 分割回数 / 各回の株数」を返す。

    分割方針:
      - 米国株 (currency=USD): 1 回あたり ¥660K 相当を上限に集約。1〜max_splits 回。
      - 日本株 (.T): 銘柄別のJPX売買単位で丸めた上で分割。
      - 投信   (SLIM_*, IFREE_*, MNXACT, NOMURA_SEMI): 円ベース、最低 ¥30 万 / 回。
      - 1 回で済むなら必ず 1 回（手数料効率最優先）。

    Returns:
      {
        'ticker': str,
        'direction': 'trim' | 'add' | 'hold',
        'diff_pct': float,
        'abs_jpy_needed': float,
        'total_shares': int,           # 投信の場合は -1
        'total_jpy': float,
        'splits': int,
        'shares_per_split': list[int], # 投信の場合は []
        'jpy_per_split':    list[int], # 投信用
        'reason': str,
      }
    """
    diff_pct  = target_pct - current_pct
    direction = "add" if diff_pct > 0 else ("trim" if diff_pct < 0 else "hold")
    abs_jpy   = abs(diff_pct) * float(total_jpy or 0)

    base = {
        "ticker":           ticker,
        "quantity_label":   quantity_label_for_ticker(ticker),
        "direction":        direction,
        "diff_pct":         round(diff_pct, 4),
        "abs_jpy_needed":   round(abs_jpy, 0),
        "total_shares":     0,
        "total_jpy":        0,
        "splits":           0,
        "shares_per_split": [],
        "jpy_per_split":    [],
        "reason":           "",
    }
    if direction == "hold" or abs_jpy < 1.0:
        base["reason"] = "diff < 0.1% — hold"
        return base

    # ── 投信: 円建てで分割 (最低 ¥30 万 / 回) ─────────────────────
    if _is_fund(ticker):
        per_min = 300_000
        # ¥30 万単位で何回分入れられるか。max_splits を超えない最小の分割数を探る。
        target_per = max(per_min, math.ceil(abs_jpy / max_splits))
        splits = max(1, min(max_splits, math.ceil(abs_jpy / target_per)))
        per    = math.ceil(abs_jpy / splits)
        # ¥10K 単位に切り上げ（実際の積立は万円単位が扱いやすい）
        per    = math.ceil(per / 10_000) * 10_000
        amounts = [per] * splits
        base.update({
            "total_shares":     -1,
            "total_jpy":        per * splits,
            "splits":           splits,
            "shares_per_split": [],
            "jpy_per_split":    amounts,
            "reason":           f"投信 {direction}: ¥{per:,}/回 × {splits}回（最低¥30万/回）",
        })
        return base

    # ── 米国株 / 日本株: 株数ベース ─────────────────────────────
    if share_price <= 0:
        base["reason"] = "share_price 不明 — 計算不能"
        return base

    price_in_jpy = share_price * (fx_rate if currency == "USD" else 1.0)
    raw_shares   = abs_jpy / price_in_jpy

    if _is_jp(ticker):
        lot = trading_unit_for_ticker(ticker)
        # 0.5 単元未満は切り上げ・以上は四捨五入で銘柄別単位へ丸める。
        units = max(1, round(raw_shares / lot))
        total_shares = units * lot
        # 分割は単元数で考え、1 単元しか動かないなら 1 回。
        if units <= 1:
            splits = 1
        else:
            # 単元を max_splits で割る — ただし手数料効率を見て大きい単元を優先。
            splits = min(max_splits, units)
        per_units = [units // splits] * splits
        for i in range(units % splits):
            per_units[i] += 1
        shares_per_split = [u * lot for u in per_units]
    else:
        # 米国株: 1 株単位。1 株分しかなくても 1 回。手数料効率上限を超えるなら分割検討。
        total_shares = max(1, math.ceil(raw_shares))
        per_jpy = total_shares * price_in_jpy
        if per_jpy <= US_FEE_CAP_JPY or total_shares <= 5:
            # 一回でまとめる（ロット小 or 効率上限内）
            splits = 1
        else:
            # 上限 ¥660K / 回を目安に splits 決定（ただし max_splits まで）
            splits = min(max_splits, max(1, math.ceil(per_jpy / US_FEE_CAP_JPY)))
        per_share = total_shares // splits
        rem       = total_shares % splits
        shares_per_split = [per_share + (1 if i < rem else 0) for i in range(splits)]

    total_jpy_actual = total_shares * price_in_jpy
    quantity_label = quantity_label_for_ticker(ticker)
    base.update({
        "total_shares":     int(total_shares),
        "total_jpy":        round(total_jpy_actual, 0),
        "splits":           int(splits),
        "shares_per_split": [int(s) for s in shares_per_split],
        "jpy_per_split":    [int(round(s * price_in_jpy, 0)) for s in shares_per_split],
        "reason": (
            f"{direction}: 総 {total_shares}{quantity_label} (¥{total_jpy_actual/10_000:.1f}万) を "
            f"{splits} 回に分割。{'単元' if _is_jp(ticker) else '手数料効率'}考慮。"
        ),
    })
    return base


def summarize_plans(plans: list) -> str:
    """複数銘柄の plan を表形式テキストにまとめる（Opus プロンプト or strategy ページ用）。"""
    if not plans:
        return ""
    lines = ["| 銘柄 | 方向 | 総量 | 分割 | 内訳 |", "|---|---|---|---|---|"]
    for p in plans:
        if p["direction"] == "hold":
            continue
        if p["total_shares"] == -1:
            qty = f"¥{p['total_jpy']:,}"
            split = " + ".join(f"¥{x:,}" for x in p["jpy_per_split"])
        else:
            label = str(p.get("quantity_label") or "株")
            qty = f"{p['total_shares']}{label}"
            split = " + ".join(f"{x}{label}" for x in p["shares_per_split"])
        lines.append(f"| {p['ticker']} | {p['direction']} | {qty} | {p['splits']}回 | {split} |")
    return "\n".join(lines)


if __name__ == "__main__":
    # 自己テスト
    import json
    cases = [
        # (ticker, current_pct, target_pct, total_jpy, share_price, currency)
        ("NVDA",    0.219, 0.10,  31_000_000, 950.0, "USD"),  # 大幅 trim
        ("META",    0.05,  0.04,  31_000_000, 660.0, "USD"),  # 1 株程度
        ("GLD",     0.135, 0.05,  31_000_000, 320.0, "USD"),  # 数十株 trim
        ("AVGO",    0.114, 0.08,  31_000_000, 1450.0, "USD"), # 数株
        ("1489.T",  0.02,  0.05,  31_000_000, 5800.0, "JPY"), # 単元買い
        ("6762.T",  0.04,  0.06,  31_000_000, 1200.0, "JPY"), # 単元買い
        ("SLIM_ORCAN", 0.0, 0.05, 31_000_000, 0, "JPY"),       # 投信 buy
    ]
    for c in cases:
        plan = compute_full_trim_plan(*c)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    print("\n--- summary ---")
    print(summarize_plans([compute_full_trim_plan(*c) for c in cases]))

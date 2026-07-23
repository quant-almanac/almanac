"""
recommendation_verifier.py
AI推奨事後検証エンジン。ai_recommendation_log.jsonの未検証エントリを
yfinanceで一括検証し、アクション種別×緊急度の勝率テーブルを生成する。

呼び出し方:
    from recommendation_verifier import verify_recommendations, format_accuracy_context
    stats = verify_recommendations()
    context_text = format_accuracy_context(stats)
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

from pseudo_tickers import is_pseudo_market_ticker
from utils import atomic_write_json

BASE_DIR = Path(__file__).parent
LOG_PATH = BASE_DIR / "ai_recommendation_log.json"

VERIFICATION_HORIZONS = [5, 20, 60]  # 短期・中期・長期の検証期間（営業日）

# sell/trim/short はベンチマーク相対で評価する。BULL レジームでは市場全体の
# ドリフトが銘柄リターンを押し上げるため絶対値での「下落したか」は構造的に
# 勝率を歪める。SPY を基準に「銘柄が SPY を TRIM_BENCHMARK_BUFFER_PCT 以上
# アンダーパフォームしたら trim 正解」と定義し直す。
BENCHMARK_TICKER = "SPY"
TRIM_BENCHMARK_BUFFER_PCT = 0.5  # 相対パフォーマンスの有意閾値（ノイズ除去）

_EMPTY_RESULT = {
    "stats": {},
    "total_verified": 0,
    "total_pending": 0,
    "total_newly_verified": 0,
}


def _is_profitable(
    action_type: str,
    outcome_pct: float,
    benchmark_pct: float | None = None,
) -> bool | None:
    """アクション種別と結果から勝敗を判定する。Noneは集計対象外。

    sell/trim/short は benchmark_pct が与えられた場合 SPY 相対で判定する:
      銘柄リターン < SPY リターン - TRIM_BENCHMARK_BUFFER_PCT → WIN
    benchmark_pct=None のフォールバックでは絶対 -2% 閾値を使う（保守的）。
    """
    t = action_type.lower()
    if t == "rebalance":
        return None
    if t in ("buy", "dca", "add"):
        return outcome_pct > 0
    if t == "stop_loss":
        # stop_lossは「さらなる下落を防いだか」で評価。2%以上下落が続けば正しい判断
        return outcome_pct < -2.0
    if t in ("sell", "trim", "short"):
        if benchmark_pct is not None:
            return outcome_pct < benchmark_pct - TRIM_BENCHMARK_BUFFER_PCT
        # Fallback: ベンチマーク未取得時のみ絶対閾値
        return outcome_pct < -2.0
    if t == "take_profit":
        return outcome_pct >= 0
    return None


def _beta_posterior(wins: int, losses: int) -> tuple[float, float]:
    """Beta(1+wins, 1+losses) 事後の (平均, 95%信頼区間上限) を返す。

    低サンプルでの誤警告を抑制するための指標。wins=0/losses=3 では 95%CI上限が
    約0.53と高く「まだわからない」、wins=0/losses=22 では約0.13と低く「確実に下振れ」。
    """
    from scipy.stats import beta

    a = 1 + wins
    b = 1 + losses
    mean = a / (a + b)
    upper = float(beta.ppf(0.95, a, b))
    return mean, upper


def verify_recommendations() -> dict:
    """
    ai_recommendation_log.json の未検証エントリを検証し、勝率統計を返す。

    Returns:
        {
            "stats": {"buy(high)": {"win_rate": float, "wins": int, "losses": int, "total": int}, ...},
            "total_verified": int,
            "total_pending": int,
            "total_newly_verified": int,
        }
    """
    try:
        if not LOG_PATH.exists():
            return _EMPTY_RESULT

        with open(LOG_PATH, "r", encoding="utf-8") as f:
            entries: list[dict] = json.load(f)

        now = datetime.now(tz=timezone.utc)
        max_horizon_days = max(VERIFICATION_HORIZONS) + 10  # Calendar days buffer
        cutoff = now - timedelta(days=max_horizon_days)

        # 最大ホライズン以上前かつ未検証のエントリを抽出（5日ホライズンのフォールバックのため5日も対象）
        cutoff_5d = now - timedelta(days=5)
        to_verify = []
        for entry in entries:
            if entry.get("verified"):
                continue
            as_of_str = entry.get("as_of", "")
            if not as_of_str:
                continue
            try:
                as_of = datetime.fromisoformat(as_of_str)
                # tzなしの場合はUTCとみなす
                if as_of.tzinfo is None:
                    as_of = as_of.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if as_of <= cutoff_5d:
                to_verify.append((entry, as_of))

        # 既検証エントリで benchmark_horizons が未記録のものを back-fill 対象に
        to_backfill: list[tuple[dict, datetime]] = []
        for entry in entries:
            if not entry.get("verified"):
                continue
            if "benchmark_horizons" in entry:
                continue
            as_of_str = entry.get("as_of", "")
            if not as_of_str:
                continue
            try:
                as_of = datetime.fromisoformat(as_of_str)
                if as_of.tzinfo is None:
                    as_of = as_of.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            to_backfill.append((entry, as_of))

        total_newly_verified = 0

        if to_verify or to_backfill:
            # ユニークティッカー収集（SPY はベンチマーク用に常に同梱）
            unique_tickers = list({
                e["ticker"]
                for e, _ in to_verify
                if e.get("ticker") and not is_pseudo_market_ticker(e.get("ticker"))
            })
            tickers_to_download = list(set(unique_tickers + [BENCHMARK_TICKER]))

            # 一括取得
            raw = yf.download(
                tickers_to_download,
                period="3mo",
                auto_adjust=True,
                progress=False,
                threads=True,
            )

            # MultiIndex対応
            if hasattr(raw.columns, "levels"):
                close_df = raw["Close"]
            else:
                # 単一ティッカーの場合は列名がフラット
                close_df = raw[["Close"]]
                if tickers_to_download:
                    close_df.columns = tickers_to_download

            # SPY 系列を抽出（ベンチマーク horizon 計算用）
            spy_series = None
            if BENCHMARK_TICKER in close_df.columns:
                _s = close_df[BENCHMARK_TICKER].dropna()
                if not _s.empty:
                    spy_series = _s

            def _compute_benchmark_horizons(as_of_dt: datetime) -> dict | None:
                if spy_series is None:
                    return None
                as_of_date = as_of_dt.date()
                s = spy_series[spy_series.index.date >= as_of_date]
                if s.empty:
                    return None
                base = float(s.iloc[0])
                out: dict[str, float] = {}
                for h in VERIFICATION_HORIZONS:
                    if len(s) > h:
                        out[f"{h}d"] = round((float(s.iloc[h]) - base) / base * 100, 2)
                    elif h == 5 and len(s) >= 3:
                        out[f"{h}d"] = round(
                            (float(s.iloc[min(5, len(s) - 1)]) - base) / base * 100, 2
                        )
                return out or None

            for entry, as_of in to_verify:
                ticker = entry.get("ticker")
                if not ticker:
                    continue

                # ティッカーの価格系列を取得
                if ticker not in close_df.columns:
                    continue

                series = close_df[ticker].dropna()
                if series.empty:
                    continue

                # as_of日以降のデータに絞る
                as_of_date = as_of.date()
                series_after = series[series.index.date >= as_of_date]
                if series_after.empty:
                    continue

                # 推奨日の始値相当（as_of日以降最初の終値）
                price_at_rec_date = float(series_after.iloc[0])

                # price_at_rec が null の場合は上記で代替
                if entry.get("price_at_rec") is None:
                    base_price = price_at_rec_date
                else:
                    base_price = float(entry["price_at_rec"])

                # マルチホライズン検証
                horizons_result = {}
                for horizon in VERIFICATION_HORIZONS:
                    if len(series_after) > horizon:
                        price_hd = float(series_after.iloc[horizon])
                        outcome_pct_h = (price_hd - base_price) / base_price * 100
                        horizons_result[f"{horizon}d"] = {
                            "price": round(price_hd, 4),
                            "outcome_pct": round(outcome_pct_h, 2),
                        }
                    elif horizon == 5 and len(series_after) >= 3:
                        # Fallback: use latest available for 5d
                        price_hd = float(series_after.iloc[min(5, len(series_after) - 1)])
                        outcome_pct_h = (price_hd - base_price) / base_price * 100
                        horizons_result[f"{horizon}d"] = {
                            "price": round(price_hd, 4),
                            "outcome_pct": round(outcome_pct_h, 2),
                        }

                if "5d" in horizons_result:
                    entry["verified"] = True
                    entry["verified_at"] = now.isoformat()
                    entry["price_verified"] = horizons_result["5d"]["price"]
                    entry["outcome_pct"] = horizons_result["5d"]["outcome_pct"]
                    entry["horizons"] = horizons_result
                    bench_h = _compute_benchmark_horizons(as_of)
                    if bench_h:
                        entry["benchmark_horizons"] = bench_h
                        entry["benchmark_outcome_pct"] = bench_h.get("5d")
                    total_newly_verified += 1

            # 既検証エントリへ benchmark_horizons を back-fill
            for entry, as_of in to_backfill:
                bench_h = _compute_benchmark_horizons(as_of)
                if bench_h:
                    entry["benchmark_horizons"] = bench_h
                    entry["benchmark_outcome_pct"] = bench_h.get("5d")

            # アトミック保存
            atomic_write_json(LOG_PATH, entries)

        # 全検証済みエントリで勝率集計
        stats: dict[str, dict] = {}
        total_verified = 0
        total_pending = 0

        for entry in entries:
            if entry.get("verified"):
                total_verified += 1
                action_type = entry.get("type", "")
                urgency = entry.get("urgency", "")
                outcome_pct = entry.get("outcome_pct")
                if outcome_pct is None:
                    continue

                bench_pct = entry.get("benchmark_outcome_pct")
                profitable = _is_profitable(
                    action_type, float(outcome_pct), bench_pct if bench_pct is not None else None
                )
                if profitable is None:
                    continue

                key = f"{action_type}({urgency})"
                if key not in stats:
                    stats[key] = {"wins": 0, "losses": 0, "total": 0}
                stats[key]["total"] += 1
                if profitable:
                    stats[key]["wins"] += 1
                else:
                    stats[key]["losses"] += 1
            else:
                total_pending += 1

        # win_rate 計算
        for key, v in stats.items():
            v["win_rate"] = round(v["wins"] / v["total"], 4) if v["total"] > 0 else 0.0

        # Multi-horizon stats
        horizon_stats: dict[str, dict[str, dict]] = {}
        for entry in entries:
            if not entry.get("verified"):
                continue
            horizons = entry.get("horizons", {})
            action_type = entry.get("type", "")
            urgency = entry.get("urgency", "")
            key = f"{action_type}({urgency})"

            bench_horizons = entry.get("benchmark_horizons", {}) or {}
            for h_key, h_data in horizons.items():
                if h_key not in horizon_stats:
                    horizon_stats[h_key] = {}
                if key not in horizon_stats[h_key]:
                    horizon_stats[h_key][key] = {"wins": 0, "losses": 0, "total": 0}

                bench_pct = bench_horizons.get(h_key)
                profitable = _is_profitable(
                    action_type, float(h_data["outcome_pct"]), bench_pct
                )
                if profitable is None:
                    continue
                horizon_stats[h_key][key]["total"] += 1
                if profitable:
                    horizon_stats[h_key][key]["wins"] += 1
                else:
                    horizon_stats[h_key][key]["losses"] += 1

        # Calculate win_rates for each horizon
        for h_key, h_stats in horizon_stats.items():
            for key, v in h_stats.items():
                v["win_rate"] = round(v["wins"] / v["total"], 4) if v["total"] > 0 else 0.0

        return {
            "stats": stats,
            "total_verified": total_verified,
            "total_pending": total_pending,
            "total_newly_verified": total_newly_verified,
            "horizon_stats": horizon_stats,
        }

    except Exception:
        return _EMPTY_RESULT


def format_accuracy_context(accuracy_data: dict) -> str:
    """
    勝率統計を Opus プロンプト注入用テキストに整形する。

    Args:
        accuracy_data: verify_recommendations() の戻り値

    Returns:
        整形済み文字列（検証数不足時は空文字列）
    """
    total_verified = accuracy_data.get("total_verified", 0)
    if total_verified < 5:
        return ""

    stats: dict[str, dict] = accuracy_data.get("stats", {})

    # total >= 3 のエントリのみ、アルファベット順
    filtered = {k: v for k, v in stats.items() if v.get("total", 0) >= 3}
    if not filtered:
        return ""

    lines = [
        f"【過去の推奨精度（検証済み{total_verified}件）】",
        "  ※ sell/trim/short は SPY相対（銘柄-SPY リターン差）で WIN を判定",
    ]
    for key in sorted(filtered.keys()):
        v = filtered[key]
        pct = int(round(v["win_rate"] * 100))
        suffix = "（データ不足）" if v["total"] < 10 else ""
        lines.append(f"  {key}: 勝率{pct}% ({v['wins']}勝/{v['losses']}敗){suffix}")

    # 警告: Beta(1+wins, 1+losses) 事後の 95%信頼区間上限が 50% 未満 かつ
    # サンプル数 >= 10 の時のみ「確実に低い」と判定。低サンプルでは抑制。
    warnings = []
    for key in sorted(filtered.keys()):
        v = filtered[key]
        if v["total"] < 10:
            continue
        _, ci_upper = _beta_posterior(v["wins"], v["losses"])
        if ci_upper >= 0.5:
            continue  # 事後に不確実性が残る場合は警告しない
        pct = int(round(v["win_rate"] * 100))
        ci_pct = int(round(ci_upper * 100))
        action_type = key.split("(")[0]
        if action_type == "stop_loss":
            note = "損切りタイミングを保守的に判断すること"
        elif action_type in ("trim", "sell", "short"):
            note = "SPY相対で有意にアンダーパフォームを外している — trim理由（決算ヘッジ/ドリフト是正/マクロ）を再吟味"
        else:
            note = "推奨判断を慎重に行うこと"
        warnings.append(
            f"⚠️ {key} 勝率{pct}% (事後95%CI上限 {ci_pct}%) — {note}"
        )

    if warnings:
        lines.append("")
        lines.extend(warnings)

    # Multi-horizon comparison
    horizon_stats = accuracy_data.get("horizon_stats", {})
    if horizon_stats:
        lines.append("")
        lines.append("【期間別勝率比較】")
        for h_key in sorted(horizon_stats.keys(), key=lambda x: int(x.replace("d", ""))):
            h_stats = horizon_stats[h_key]
            h_filtered = {k: v for k, v in h_stats.items() if v.get("total", 0) >= 3}
            if h_filtered:
                avg_wr = sum(v["win_rate"] for v in h_filtered.values()) / len(h_filtered)
                lines.append(f"  {h_key}: 平均勝率{int(round(avg_wr * 100))}% ({len(h_filtered)}カテゴリ)")

    return "\n".join(lines)

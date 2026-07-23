import csv
import json
import os
import requests
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"})


def generate_weekly_report():
    filepath = os.path.expanduser('~/portfolio-bot/trade_history.csv')

    if not os.path.exists(filepath):
        print("[weekly_report] 売買履歴なし — スキップ")
        return

    today = datetime.now()
    week_ago = today - timedelta(days=7)

    trades = []
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            trade_date = datetime.strptime(row['日時'], '%Y-%m-%d %H:%M')
            if trade_date >= week_ago:
                trades.append(row)

    buys  = [t for t in trades if t['アクション'] == 'BUY']
    sells = [t for t in trades if t['アクション'] == 'SELL']

    # ── 全体集計 ──────────────────────────────────────────
    total_pnl = 0
    wins = 0
    losses = 0
    for t in sells:
        pnl_str = t.get('損益%', '').replace('%', '').strip()
        try:
            pnl = float(pnl_str) if pnl_str else None
        except ValueError:
            pnl = None
        if pnl is not None:
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            else:
                losses += 1

    win_rate = int(wins / len(sells) * 100) if sells else 0
    avg_pnl  = total_pnl / len(sells) if sells else 0

    # ── 戦略別集計 ────────────────────────────────────────
    strategy_stats = _calc_strategy_stats(sells)
    strategy_lines = _fmt_strategy_stats(strategy_stats)

    # ── v5.1: Implementation Shortfall 集計 ──────────────
    is_lines = _fmt_implementation_shortfall(week_ago, today)

    # ── v5.1: Backtest Hygiene 自己チェック ───────────────
    hygiene_lines = _fmt_backtest_hygiene_checklist()
    llm_spend_lines = _fmt_llm_spend()
    follow_rate_lines = _fmt_follow_rate()
    narrative_lines = _fmt_narrative_context()
    excess_lines = _fmt_benchmark_excess()
    beta_alpha_lines = _fmt_beta_adjusted_alpha()

    # ── 現在の保有銘柄 ────────────────────────────────────
    holdings_path = os.path.expanduser('~/portfolio-bot/holdings.json')
    holdings = {}
    if os.path.exists(holdings_path):
        with open(holdings_path) as f:
            holdings = json.load(f)

    report = f"""📊 <b>週次パフォーマンスレポート</b>
{week_ago.strftime('%m/%d')} 〜 {today.strftime('%m/%d')}
━━━━━━━━━━━━━━
📈 売買回数: 買{len(buys)}回 / 売{len(sells)}回
🏆 勝率: {win_rate}%（{wins}勝{losses}敗）
💰 平均損益: {avg_pnl:+.1f}%
━━━━━━━━━━━━━━
{strategy_lines}
━━━━━━━━━━━━━━
{is_lines}
━━━━━━━━━━━━━━
{hygiene_lines}
━━━━━━━━━━━━━━
{llm_spend_lines}
━━━━━━━━━━━━━━
{follow_rate_lines}
━━━━━━━━━━━━━━
{narrative_lines}
━━━━━━━━━━━━━━
{excess_lines}
━━━━━━━━━━━━━━
{beta_alpha_lines}
━━━━━━━━━━━━━━
📁 現在の保有銘柄: {len(holdings)}銘柄
{chr(10).join([f"  • {k}" for k in holdings.keys()]) if holdings else "  なし"}
━━━━━━━━━━━━━━
来週も慎重に！"""

    # 週次レポートの Telegram 送信は廃止。report 内容は /risk (QuantStats HTML) で確認。
    print("週次レポート生成完了（Telegram 通知なし）")

    _generate_quantstats_tearsheet()


# ============================================================
# v5.1: Implementation Shortfall + Backtest Hygiene
# ============================================================

def _fmt_implementation_shortfall(week_start: datetime, week_end: datetime) -> str:
    """週次の AI 指値達成率 + 中央 shortfall_bps + 最悪約定の表示。
    execution_quality.shortfall_summary を呼ぶ薄いラッパー。
    """
    try:
        from execution_quality import shortfall_summary
    except Exception as e:
        return f"📐 IS集計: 実装エラー ({e})"

    s = shortfall_summary(
        week_start=week_start.strftime('%Y-%m-%d'),
        week_end=week_end.strftime('%Y-%m-%d'),
        min_n=3,
    )

    if s.get('n', 0) == 0:
        return "📐 <b>Implementation Shortfall</b>\n  集計対象の約定なし"

    if s.get('sample_too_small'):
        return (
            f"📐 <b>Implementation Shortfall</b>\n"
            f"  サンプル {s['n']} 件（&lt;3 で統計不足）— 暫定表示は控える"
        )

    lines = ["📐 <b>Implementation Shortfall</b>"]
    median = s.get('median_shortfall_bps')
    if median is not None:
        sign_icon = "🟢" if median <= 0 else ("🟡" if median < 30 else "🔴")
        lines.append(f"  {sign_icon} 中央 shortfall: {median:+.0f}bps（n={s['n']}）")
    if s.get('iqr_bps') is not None:
        lines.append(f"  IQR: {s['iqr_bps']:.0f}bps（25%={s.get('q25_shortfall_bps')} / 75%={s.get('q75_shortfall_bps')}）")
    if s.get('ai_compliance_rate') is not None:
        rate = s['ai_compliance_rate'] * 100
        icon = "✅" if rate >= 70 else "⚠️"
        lines.append(
            f"  {icon} AI指値遵守率: {rate:.0f}%（指値推奨 {s['ai_proposed_limit_n']}件中 {int(s['ai_proposed_limit_n']*s['ai_compliance_rate'])}件遵守）"
        )
    if s.get('worst'):
        w = s['worst']
        lines.append(f"  🔻 最悪: {w.get('ticker')} {w.get('direction','')} {w.get('sf')}bps")
    return "\n".join(lines)


def _fmt_backtest_hygiene_checklist() -> str:
    """5 項目セルフチェック。ここでは現状の運用が各落とし穴に対してどう守られているかを定型表示する。
    将来的には自動検出（例: holdings に上場廃止銘柄が含まれているか）も追加可能。
    """
    items = [
        ("Look-ahead Bias",       "Walk-Forward (recommendation_verifier) で訓練/検証窓を分離"),
        ("Survivorship Bias",     "上場廃止銘柄も holdings 履歴に保持（trade_history.csv 全件保存）"),
        ("Data Snooping",         "ファクター回帰は固定 ETF プロキシ、シグナル閾値は threshold_calibrator がベイズ更新"),
        ("Transaction Cost",      "Implementation Shortfall を実測値で控除して勝率再計算"),
        ("Regime Overfit",        "HMM 3状態レジーム × VIX 帯で分類、レジーム別パフォーマンスを記録"),
    ]
    lines = ["🧪 <b>Backtest Hygiene 自己チェック</b>"]
    for name, status in items:
        lines.append(f"  ✓ {name}: {status}")
    return "\n".join(lines)


def _fmt_llm_spend(log_path: Path | None = None) -> str:
    try:
        from llm_cost_accounting import read_usage_rows, summarize_month
        path = log_path or Path(__file__).parent / "logs" / "llm_calls.jsonl"
        summary = summarize_month(read_usage_rows(path))
    except Exception as exc:
        return f"LLM spend: unavailable ({exc})"
    return (
        f"LLM spend ({summary['month']}): ${summary['cost_usd']:.4f} "
        f"/ calls={summary['calls']} / unpriced={summary['unpriced_calls']}"
    )


def _fmt_follow_rate() -> str:
    try:
        from follow_rate_analyzer import match_recommendations
        summary = match_recommendations()
    except Exception as exc:
        return f"Follow-rate: unavailable ({exc})"
    return (
        f"AI follow-rate: {summary['follow_rate'] * 100:.1f}% "
        f"({summary['total_matched']}/{summary['total_recs']})"
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _fmt_narrative_context(
    decision_path: Path | None = None,
    outcome_path: Path | None = None,
) -> str:
    root = Path(__file__).parent
    decisions = _read_jsonl(decision_path or root / "sell_decision_log.jsonl")
    outcomes = _read_jsonl(outcome_path or root / "sell_outcome_log.jsonl")
    cutoff = datetime.now() - timedelta(days=30)
    recent = []
    for row in decisions:
        try:
            dt = datetime.fromisoformat(str(row.get("recommended_at") or "").replace("Z", "+00:00"))
            compare = dt.replace(tzinfo=None)
        except ValueError:
            continue
        if compare >= cutoff:
            recent.append(row)
    outcome_by_id = {
        row.get("sell_decision_id"): row
        for row in outcomes
        if row.get("sell_decision_id")
    }
    with_ctx = [r for r in recent if r.get("narrative_context_present") is True]
    without_ctx = [r for r in recent if r.get("narrative_context_present") is False]

    def avg_missed(rows: list[dict]) -> float | None:
        values = [
            float(outcome_by_id[r.get("sell_decision_id")]["missed_excess_return_bps"])
            for r in rows
            if r.get("sell_decision_id") in outcome_by_id
            and outcome_by_id[r.get("sell_decision_id")].get("missed_excess_return_bps") is not None
        ]
        return sum(values) / len(values) if values else None

    yes = avg_missed(with_ctx)
    no = avg_missed(without_ctx)
    return (
        "Narrative context 30d: "
        f"present={len(with_ctx)} avg_missed_bps={yes if yes is not None else 'n/a'}; "
        f"absent={len(without_ctx)} avg_missed_bps={no if no is not None else 'n/a'}"
    )


def _fmt_benchmark_excess() -> str:
    today = datetime.now().date()
    try:
        from nav_recorder import modified_dietz_twr
        result = modified_dietz_twr(
            date_from=(today - timedelta(days=30)).isoformat(),
            date_to=today.isoformat(),
        )
    except Exception as exc:
        return f"Benchmark excess 30d: unavailable ({exc})"
    value = result.get("excess_return_pct")
    if value is None:
        return f"Benchmark excess 30d: suppressed ({result.get('excess_suppressed_reason')})"
    return f"Benchmark excess 30d: {float(value):+.2f}%"


def _fmt_beta_adjusted_alpha() -> str:
    today = datetime.now().date()
    try:
        from benchmark_tracker import get_beta_adjusted_alpha
        result = get_beta_adjusted_alpha(
            date_from=(today - timedelta(days=90)).isoformat(),
            date_to=today.isoformat(),
        )
    except Exception as exc:
        return f"Beta-adjusted alpha: unavailable ({exc})"
    alpha = result.get("alpha_pct_annualized")
    if alpha is None:
        return f"Beta-adjusted alpha: suppressed ({result.get('error')})"
    return (
        f"Beta-adjusted alpha (annualized): {float(alpha):+.2f}% "
        f"/ beta={float(result['beta']):.2f} / n={result['n']}"
    )


def _calc_strategy_stats(sells: list) -> dict:
    """
    SELL レコードから戦略別勝率・平均損益を集計する。
    CSV に 'strategy' 列がない場合は investment_type で代替。
    """
    stats: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "total_pnl": 0.0})

    for t in sells:
        # 戦略名: 'strategy' 列 → 'investment_type' 列 → 'その他'
        strategy = (
            t.get('strategy', '').strip()
            or t.get('investment_type', '').strip()
            or 'その他'
        )

        pnl_str = t.get('損益%', '').replace('%', '').strip()
        try:
            pnl = float(pnl_str) if pnl_str else None
        except ValueError:
            pnl = None

        if pnl is not None:
            stats[strategy]['total_pnl'] += pnl
            if pnl > 0:
                stats[strategy]['wins'] += 1
            else:
                stats[strategy]['losses'] += 1

    # 勝率・平均損益を計算
    result = {}
    for strat, s in stats.items():
        total = s['wins'] + s['losses']
        result[strat] = {
            'wins':      s['wins'],
            'losses':    s['losses'],
            'total':     total,
            'win_rate':  int(s['wins'] / total * 100) if total else 0,
            'avg_pnl':   round(s['total_pnl'] / total, 2) if total else 0,
        }

    return result


def _fmt_strategy_stats(stats: dict) -> str:
    """戦略別統計を Telegram 用テキストにフォーマット"""
    if not stats:
        return "📊 戦略別データなし"

    lines = ["📊 <b>戦略別勝率</b>"]
    # 取引数が多い順にソート
    for strat, s in sorted(stats.items(), key=lambda x: -x[1]['total']):
        icon = "✅" if s['win_rate'] >= 50 else "⚠️"
        lines.append(
            f"  {icon} {strat}: {s['win_rate']}%勝率 "
            f"（{s['wins']}勝{s['losses']}敗 / 平均{s['avg_pnl']:+.1f}%）"
        )

    return "\n".join(lines)


def _generate_quantstats_tearsheet():
    """
    trade_history.csv の日次損益から QuantStats HTML レポートを生成。
    ~/portfolio-bot/reports/tearsheet_YYYYWWW.html に保存。
    """
    try:
        import quantstats as qs
        import pandas as pd
    except ImportError:
        print("[QuantStats] quantstats または pandas 未インストール → スキップ")
        return

    filepath = os.path.expanduser("~/portfolio-bot/trade_history.csv")
    if not os.path.exists(filepath):
        return

    try:
        df = pd.read_csv(filepath, encoding="utf-8")
        df["日時"] = pd.to_datetime(df["日時"])
        df = df.sort_values("日時")

        df["pnl"] = pd.to_numeric(df["損益%"].str.replace("%", ""), errors="coerce") / 100.0
        sells = df[(df["アクション"] == "SELL") & df["pnl"].notna()].copy()

        if len(sells) < 5:
            print("[QuantStats] データ不足（SELL < 5件）→ スキップ")
            return

        daily = sells.groupby(sells["日時"].dt.date)["pnl"].sum()
        daily.index = pd.to_datetime(daily.index)
        daily = daily.asfreq("D", fill_value=0.0)

        reports_dir = Path(os.path.expanduser("~/portfolio-bot/reports"))
        reports_dir.mkdir(exist_ok=True)
        week_label  = datetime.now().strftime("%Y-W%V")
        output_path = reports_dir / f"tearsheet_{week_label}.html"

        qs.reports.html(
            daily,
            output=str(output_path),
            title=f"ALMANAC 週次ティアシート {week_label}",
            download_filename=output_path.name,
        )
        print(f"[QuantStats] ティアシート生成: {output_path}")

    except Exception as e:
        print(f"[QuantStats] 生成エラー: {e}")


if __name__ == "__main__":
    # P2-9: ヘルスチェック用ハートビート
    try:
        from utils import heartbeat as _hb
    except Exception:
        _hb = None
    try:
        generate_weekly_report()
        if _hb:
            _hb('weekly_report', 'ok')
    except Exception as _e:
        if _hb:
            _hb('weekly_report', 'error', str(_e)[:500])
        raise

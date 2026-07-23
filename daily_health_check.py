"""
ALMANAC 朝次ヘルスチェック — 平日 8:30 に LaunchAgent から実行
ai-analysis (6:00) 終了後の出力を検証し、問題があれば Telegram 通知。

検証項目:
1. agent_briefing.json の鮮度 (6h以内)
2. priority_actions が十分な件数あるか (>= 3)
3. 直近 24h の screener_log / analyzer_log に OperationalError がないか
4. Batch API ステータスにエラーがないか
5. regime_state.json の鮮度 (48h以内 = alert デーモン稼働確認)
6. 非攻めモードで現金充足時に信用買い提案が出ていないか
7. portfolio_integrity の blocking issue
"""
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent

_LOG_RUN_MARKERS = {
    'screener_log.txt': (
        'スクリーニング開始',
        '全市場スクリーニング開始',
        '[short]',
        '[margin_long]',
    ),
    'analyzer_log.txt': (
        '[delta]',
        'AI分析',
        '分析開始',
    ),
}


def _hours_old(p: Path) -> float:
    return (datetime.now().timestamp() - p.stat().st_mtime) / 3600


def _load_primary_analysis() -> tuple[dict | None, Path | None, float | None, str | None]:
    """現行の AI 分析ファイルを優先して読み込む。

    旧 agent_briefing.json は補助出力になっており、現在の主出力は
    ai_portfolio_analysis.json の synthesis。旧ファイルだけを見ると
    分析は成功しているのに stale と誤判定する。
    """
    candidates = [
        (BASE / 'ai_portfolio_analysis.json', 'synthesis'),
        (BASE / 'agent_briefing.json', None),
    ]
    last_error = None
    for path, nested_key in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            payload = data.get(nested_key, data) if nested_key else data
            if isinstance(payload, dict):
                return payload, path, _hours_old(path), None
            last_error = f"{path.name} の payload が dict ではありません"
        except Exception as e:
            last_error = f"{path.name} パース失敗: {e}"
    return None, None, None, last_error


def _tail_text(path: Path, lines: int = 1000) -> str:
    try:
        result = subprocess.run(
            ['tail', f'-{lines}', str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout
    except Exception:
        return ''


def _window_after_latest_marker(text: str, markers: tuple[str, ...]) -> str:
    latest = -1
    for marker in markers:
        idx = text.rfind(marker)
        if idx > latest:
            latest = idx
    return text[latest:] if latest >= 0 else text


def _recent_log_error_count(path: Path, needle: str = 'OperationalError') -> int:
    """最新実行ウィンドウ内のエラーだけ数える。

    ログは長く、tail の中に「古い失敗 → その後の成功実行」が混在することがある。
    その場合に古い OperationalError を当日の異常として誤通知しない。
    """
    text = _tail_text(path)
    if not text:
        return 0
    window = _window_after_latest_marker(text, _LOG_RUN_MARKERS.get(path.name, ()))
    return window.count(needle)


def _send_telegram(msg: str) -> None:
    """alert.py の send_telegram を再利用"""
    try:
        from alert import send_telegram
        send_telegram(msg)
    except Exception as e:
        print(f"[health-check] Telegram 通知失敗: {e}")


def _calc_cash_jpy(holdings: dict) -> float:
    """holdings.json から JPY 換算合計現金を計算"""
    fx = 150.0  # 簡易（厳密にはライブ取得すべきだが警告判定なので OK）
    total = 0.0
    for k, v in holdings.items():
        if not isinstance(v, dict):
            continue
        if 'CASH' not in k.upper() and 'MMF' not in k.upper():
            continue
        shares = v.get('shares', 0) or 0
        entry = v.get('entry_price', 0) or 0
        curr = v.get('currency', 'JPY')
        total += shares * entry if curr == 'JPY' else shares * entry * fx
    return total


def _load_total_cash_jpy() -> float:
    account_path = BASE / 'account.json'
    if account_path.exists():
        try:
            account = json.loads(account_path.read_text())
            balance = account.get('balance')
            usd_balance = account.get('usd_balance')
            if balance is not None or usd_balance is not None:
                fx = float(account.get('fx_rate_usdjpy') or 150.0)
                return float(balance or 0) + float(usd_balance or 0) * fx
            if 'total_cash' in account:
                return float(account.get('total_cash') or 0)
        except Exception:
            pass
    holdings_path = BASE / 'holdings.json'
    if holdings_path.exists():
        try:
            return _calc_cash_jpy(json.loads(holdings_path.read_text()))
        except Exception:
            pass
    return 0.0


def _margin_buy_allowed_with_cash() -> bool:
    try:
        from scenario_strategy import get_strategy
        strategy = get_strategy()
        return (
            strategy.get('scenario') == 'BULL'
            and bool(strategy.get('leverage_allowed'))
            and float(strategy.get('cash_ratio_target') or 0) <= 3
        )
    except Exception:
        return False


def main() -> int:
    issues: list[str] = []

    # ── 1. AI 分析出力の鮮度 ──
    brief_data, analysis_path, age_h, load_error = _load_primary_analysis()
    if analysis_path:
        if age_h is not None and age_h > 8:
            issues.append(
                f"⚠️ {analysis_path.name} が <b>{age_h:.1f}h</b> 古い "
                "(ai-analysis / analyzer 失敗?)"
            )
    else:
        issues.append(f"🔴 AI分析出力が存在しない" + (f" ({load_error})" if load_error else ""))

    # ── 2. priority_actions の品質 ──
    # P0-9: 件数ノルマ（actions <3 を異常扱い）を廃止。no-trade は valid な出力。
    # 代わりに「actions=[] のとき rationale が明示されているか」「7 日連続 [] のとき regime を確認させる」を check。
    if brief_data:
        pa = brief_data.get('priority_actions', [])
        if isinstance(pa, list) and len(pa) == 0:
            rationale = (
                brief_data.get('no_action_rationale')
                or brief_data.get('headline')
                or ''
            )
            if not rationale.strip():
                issues.append("⚠️ priority_actions=[] だが理由(headline/no_action_rationale)が空")

    # ── 3. SQLite OperationalError 検出 ──
    for log in ['screener_log.txt', 'analyzer_log.txt']:
        path = BASE / log
        if not path.exists():
            continue
        cnt = _recent_log_error_count(path)
        if cnt:
            issues.append(f"🔴 <code>{log}</code> 最新実行に OperationalError <b>{cnt} 件</b>")

    # ── 4. Batch API エラー ──
    batch_path = BASE / 'long_term_batch_state.json'
    if batch_path.exists():
        try:
            state = json.loads(batch_path.read_text())
            if state.get('error') or state.get('status') == 'failed':
                err = state.get('error', state.get('status'))
                issues.append(f"⚠️ Batch API エラー: {err}")
        except Exception:
            pass

    # ── 5. regime_state.json 鮮度 (alert デーモン稼働確認) ──
    regime_path = BASE / 'regime_state.json'
    if regime_path.exists():
        age_h = _hours_old(regime_path)
        if age_h > 48:
            issues.append(f"⚠️ regime_state.json が <b>{age_h:.1f}h</b> 古い (alert デーモン停止?)")

    # ── 5.5. portfolio ledger integrity ──
    try:
        from portfolio_integrity import run_integrity_check
        integrity = run_integrity_check()
        if integrity.get("blocking_issue_count", 0) > 0:
            top = []
            for item in integrity.get("issues", [])[:5]:
                if item.get("severity") not in {"critical", "high"}:
                    continue
                label = item.get("execution_id") or item.get("tx_id") or item.get("event_id") or ""
                top.append(f"  • {item.get('check')}: {label} {item.get('message','')[:70]}")
            summary = integrity.get("summary", {})
            issues.append(
                f"🔴 portfolio_integrity blocking issue <b>{integrity.get('blocking_issue_count')}</b>件"
                + (f" / unapplied_executed={summary.get('unapplied_executed_count')}" if summary else "")
                + ("\n" + "\n".join(top) if top else "")
            )
    except Exception as e:
        issues.append(f"⚠️ portfolio_integrity 実行失敗: {e}")

    # ── 6. 非攻めモードでの現金充足時の信用買い検出 ──
    if brief_data:
        try:
            cash_jpy = _load_total_cash_jpy()
            pa = brief_data.get('priority_actions', [])
            margin_buys = [a for a in pa if a.get('type') == 'margin_buy']
            if margin_buys and cash_jpy > 1_000_000 and not _margin_buy_allowed_with_cash():
                msg = f"🔴 非攻めモードで現金 ¥{cash_jpy/10000:.0f}万 あるのに信用買い提案 <b>{len(margin_buys)} 件</b>:"
                for m in margin_buys[:3]:
                    msg += f"\n  • {m.get('ticker','?')}: {m.get('reason','')[:60]}"
                issues.append(msg)
        except Exception:
            pass

    # ── レポート ──
    today = datetime.now().strftime('%Y-%m-%d %H:%M')
    if issues:
        body = f"🔴 <b>ALMANAC 朝次ヘルスチェック</b> [{today}]\n\n" + "\n\n".join(issues)
        print(body.replace('<b>', '').replace('</b>', '').replace('<code>', '`').replace('</code>', '`'))
        # ALMANAC: telegram disabled — ai_analysis only
        # _send_telegram(body)
        # LaunchAgent 上は「チェック実行に成功し、問題を通知した」状態なので 0 で終了する。
        # 異常そのものは Telegram/ログ本文に残す。
        return 0
    else:
        print(f"✅ ALMANAC 朝次ヘルスチェック [{today}] - 問題なし")
        return 0


if __name__ == '__main__':
    raise SystemExit(main())

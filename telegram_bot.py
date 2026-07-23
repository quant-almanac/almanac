"""
ALMANAC Telegram Bot
テレグラムからコマンドでポートフォリオを操作する

コマンド:
  /analyze  /分析       — AI総合分析を強制実行し結果を送信（約2分）
  /brief    /ブリーフィング — Morning Brief を再生成して送信
  /status   /状況        — ガード・レジーム・P&L を即時確認
  /help     /ヘルプ      — コマンド一覧

起動:
  python telegram_bot.py
"""
import json
import html
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE_DIR    = Path(__file__).parent
OFFSET_FILE = BASE_DIR / ".telegram_offset"


# ─── Telegram 通信 ─────────────────────────────────────

def _get_updates(offset: int) -> list:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 25},
            timeout=30,
        )
        return r.json().get("result", [])
    except Exception:
        return []


def _send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[send error] {e}")


# ─── コマンドハンドラ ────────────────────────────────────

def handle_analyze():
    _send("⏳ AI総合分析を開始します（約1〜2分かかります）…")
    try:
        res = subprocess.run(
            [str(BASE_DIR / "venv/bin/python"), "portfolio_analyst.py", "--force", "--telegram"],
            cwd=str(BASE_DIR),
            capture_output=True, text=True, timeout=300,
        )
        if res.returncode != 0:
            # stderr の最後の非空行を抜き出す（yfinance 警告を除外）
            lines = [l for l in res.stderr.splitlines() if l.strip() and "possibly delisted" not in l and "Yahoo error" not in l]
            short_err = "\n".join(lines[-5:]) if lines else res.stderr[-200:]
            _send(f"❌ 分析エラー:\n{short_err}")
    except subprocess.TimeoutExpired:
        _send("❌ 分析がタイムアウトしました（5分超過）")
    except Exception as e:
        _send(f"❌ エラー: {e}")


def handle_brief():
    """Resend the latest canonical analysis; daily_briefing is retired."""
    try:
        path = BASE_DIR / "ai_portfolio_analysis.json"
        if not path.exists():
            _send("❌ 最新のAI分析が見つかりません")
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        synthesis = data.get("synthesis") or {}
        lines = [
            "🧠 <b>最新のAI分析</b>",
            f"日時: {html.escape(str(data.get('as_of') or '—'))}",
            f"スタンス: {html.escape(str(synthesis.get('overall_stance') or '—'))}",
        ]
        actions = [row for row in (synthesis.get("priority_actions") or []) if isinstance(row, dict)]
        if actions:
            lines.append("")
            for index, row in enumerate(actions[:10], 1):
                ticker = html.escape(str(row.get("ticker") or "—"))
                action = html.escape(str(row.get("action") or row.get("type") or ""))
                readiness = html.escape(str(row.get("execution_readiness") or "review"))
                lines.append(f"#{index} <b>{ticker}</b> [{readiness}] {action}")
        else:
            lines.append("\n実行候補は0件です")
        _send("\n".join(lines))
    except Exception as e:
        _send(f"❌ エラー: {e}")


def handle_status():
    try:
        guard_file  = BASE_DIR / "guard_state.json"
        regime_file = BASE_DIR / "regime_state.json"
        analysis_file = BASE_DIR / "ai_portfolio_analysis.json"

        guard  = json.loads(guard_file.read_text())  if guard_file.exists()  else {}
        regime = json.loads(regime_file.read_text()) if regime_file.exists() else {}

        trading_ok   = guard.get("trading_allowed", True)
        new_entry_ok = guard.get("new_entry_allowed", True)
        daily_pnl    = guard.get("daily_pnl_pct", 0)
        monthly_pnl  = guard.get("monthly_pnl_pct", 0)
        spy_above    = regime.get("spy_above", True)

        text  = f"📊 <b>現在の状況</b> — {datetime.now().strftime('%m/%d %H:%M')}\n\n"
        text += f"{'🟢' if trading_ok   else '🔴'} 取引: {'OK' if trading_ok else '停止'}\n"
        text += f"{'🟢' if new_entry_ok else '🔴'} 新規エントリー: {'OK' if new_entry_ok else '禁止'}\n"
        text += f"{'🟢' if daily_pnl   >= 0 else '🔴'} 日次P&L: {daily_pnl:+.1f}%\n"
        text += f"{'🟢' if monthly_pnl >= 0 else '🔴'} 月次P&L: {monthly_pnl:+.1f}%\n"
        text += f"{'📈' if spy_above else '📉'} レジーム: {'BULL (SPY > MA50)' if spy_above else 'BEAR (SPY < MA50)'}\n"

        # 最終分析日時
        if analysis_file.exists():
            data = json.loads(analysis_file.read_text())
            as_of = data.get("as_of", "—")
            stance = (data.get("synthesis") or {}).get("overall_stance", "—")
            text += f"\n🧠 最終分析: {as_of}\n📋 スタンス: {stance}"

        _send(text)
    except Exception as e:
        _send(f"❌ 状況取得エラー: {e}")


def handle_help():
    _send(
        "🤖 <b>ALMANAC Bot</b>\n\n"
        "/analyze  — AI総合分析を即時実行（約2分）\n"
        "/brief    — Morning Briefを再生成\n"
        "/status   — ガード・レジーム・P&L確認\n"
        "/help     — このヘルプ\n\n"
        "日本語コマンドも使えます:\n"
        "/分析 /ブリーフィング /状況 /ヘルプ"
    )


COMMANDS: dict[str, object] = {
    "/analyze":       handle_analyze,
    "/分析":           handle_analyze,
    "/brief":         handle_brief,
    "/ブリーフィング":   handle_brief,
    "/status":        handle_status,
    "/状況":           handle_status,
    "/help":          handle_help,
    "/ヘルプ":         handle_help,
}


# ─── オフセット管理 ──────────────────────────────────────

def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text())
    except Exception:
        return 0


def _save_offset(offset: int):
    OFFSET_FILE.write_text(str(offset))


# ─── メインループ ────────────────────────────────────────

def main():
    if not TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID が未設定")
        sys.exit(1)

    print(f"[{datetime.now():%H:%M:%S}] ALMANAC Bot 起動 (chat_id={CHAT_ID})")
    _send("🤖 ALMANAC Bot が起動しました\n/help でコマンド一覧を確認")

    offset = _load_offset()

    while True:
        updates = _get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            _save_offset(offset)

            msg     = upd.get("message", {})
            from_id = str(msg.get("chat", {}).get("id", ""))
            text    = (msg.get("text") or "").strip()

            # 自分のチャットID以外は無視
            if from_id != str(CHAT_ID):
                continue

            # コマンド照合（/start@BotName の @ 以降を除去）
            cmd = text.split()[0].split("@")[0].lower() if text else ""
            handler = COMMANDS.get(cmd)
            if handler:
                print(f"[{datetime.now():%H:%M:%S}] コマンド受信: {cmd}")
                handler()
            elif text:
                _send(f"❓ 未知のコマンド: <code>{cmd}</code>\n/help でコマンド一覧を確認")

        time.sleep(3)


if __name__ == "__main__":
    main()

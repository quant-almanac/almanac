"""
ALMANAC Portfolio Agent
Claude Agent SDK を使ったポートフォリオ分析オーケストレーター。

portfolio_analyst.py との違い:
  - 正式な統合分析ではなく、その上に乗る短い所見を返す補助経路
  - モデルには sanitized projection だけを渡し、ツールは与えない

⚠️ 2026-08-25 の再設計 (Codex レビュー round 11):
以前はプロンプトに作業ディレクトリの絶対パスと読むべきファイル名を書き、
Read/Write/Bash を許可していた。つまり Agent は raw の technical_state.json
を読めてしまい、他の全 consumer が通っている品質契約を迂回できた。
holdings.json の note / owner / broker / account まで見えていた。
出力はモデル自身がファイルへ書いており、ホスト側の検証は無かった。

今は agent_projection.py が CLI/API 共通で:
  - 入力を sanitized projection にする (パスもファイル名も渡さない)
  - ツールを一切与えない
  - 出力を構造化スキーマで受け、ホストが検証してから保存する

使い方:
  python portfolio_agent.py           # デフォルト分析
  python portfolio_agent.py --mode risk
  python portfolio_agent.py --mode nisa
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_projection import (
    AGENT_RUN_LOCK_NAME,
    AGENT_RUN_LOCK_TIMEOUT_SECONDS,
    ENABLED_MODES,
    AgentOutputError,
    AgentProtocolViolation,
    MODES,
    build_agent_options,
    build_agent_projection,
    build_agent_prompt,
    parse_agent_result,
    resolve_agent_model,
    save_verified_result,
    validate_agent_output,
)
from utils import LockBusy, process_lock

BASE_DIR = Path(__file__).parent


def _log_agent_result(**kwargs) -> None:
    """API 経路と同じ会計ログへ書く。CLI だけ記録が抜けると、
    費用の全体像が追えない (Codex レビュー round 14)。"""
    try:
        from api.routes.agent import _log_agent_result as _log
        _log(**kwargs)
    except Exception:
        pass

# ホストが書く。Agent には触らせない。
OUTPUT_FILES = {
    "default": "agent_briefing.json",
    "risk": "risk_agent_report.json",
    "nisa": "nisa_agent_strategy.json",
}


async def run_analysis(mode: str = "default") -> int:
    """projection 生成から保存までを共有ロックの中で行う。

    CLI と API が同時に走ると二重課金になり、遅く終わった古い run が
    新しい結果を上書きしうる (Codex レビュー round 13)。
    """
    try:
        with process_lock(AGENT_RUN_LOCK_NAME, timeout=AGENT_RUN_LOCK_TIMEOUT_SECONDS):
            return await _run_locked(mode)
    except LockBusy:
        print("⚠️ 別の Agent 実行が進行中です。二重起動しません。")
        return 1


async def _run_locked(mode: str) -> int:
    try:
        from claude_agent_sdk import query, AssistantMessage, ResultMessage
        from claude_agent_sdk.types import TextBlock, ToolUseBlock
    except ImportError:
        print("❌ claude-agent-sdk が未インストール: pip install claude-agent-sdk")
        return 1

    now = datetime.now(timezone.utc)
    try:
        projection = build_agent_projection(mode, base_dir=BASE_DIR, now=now)
    except Exception as exc:
        print(f"❌ projection の生成に失敗: {type(exc).__name__}: {exc}")
        return 1

    prompt = build_agent_prompt(projection)
    options = build_agent_options()
    started = time.monotonic()

    print(f"🤖 Portfolio Agent 起動 [モード: {mode}]")
    print(f"   候補 {len(projection['candidates'])} 件 / ツールなし / 構造化出力")
    print("─" * 50)

    result_payload = None
    cost_usd = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        # ツールを与えていないので、使おうとした時点で契約違反。
                        raise AgentProtocolViolation(
                            f"agent attempted tool use: {block.name}")
                    # ⚠️ TextBlock は表示しない。scope 外の銘柄に触れる
                    # 自由文が検証前に人の目に入ると、構造化 action を
                    # 縛った意味が薄れる (Codex レビュー round 13)。
                    # 表示するのは検証済みの結果だけ。
                    if isinstance(block, TextBlock):
                        continue
            elif isinstance(message, ResultMessage):
                print()
                # ⚠️ CLI もコストを記録・表示する。隔離ライブで
                # 「どのモデルにいくら使ったか」を検証できるようにする
                # (Codex レビュー round 14)。
                # ⚠️ 検証を通るまで記録しない (API と同じ理由)。
                cost_usd = getattr(message, "total_cost_usd", None)
                if cost_usd is not None:
                    print(f"💴 コスト: ${cost_usd:.4f} (model={resolve_agent_model()})")
                if message.subtype != "success":
                    _log_agent_result(mode=mode, prompt=prompt, started=started,
                                      status=message.subtype, cost_usd=cost_usd)
                    print(f"❌ エラー: {message.subtype}")
                    return 1
                result_payload = message
    except AgentProtocolViolation as exc:
        # ⚠️ 以前はここで会計ログを残していなかった。プロトコル違反や
        # query() の例外が「何も記録されない run」として消えていた
        # (レビューで指摘)。API 側は _run_agent_locked の except で記録して
        # いるのに、CLI 側だけ抜けていた。
        _log_agent_result(mode=mode, prompt=prompt, started=started,
                          status="protocol_violation", cost_usd=cost_usd, error=exc)
        print(f"\n❌ プロトコル違反: {exc}")
        return 1
    except Exception as exc:
        _log_agent_result(mode=mode, prompt=prompt, started=started,
                          status="error", cost_usd=cost_usd, error=exc)
        print(f"\n❌ Agent エラー: {exc}")
        print("ヒント: ANTHROPIC_API_KEY が設定されているか確認してください")
        return 1

    # ── ホスト側の検証と保存 ──
    # 検証に落ちたら **保存しない**。last-known-good をそのまま残す。
    try:
        raw = parse_agent_result(result_payload)
        verified = validate_agent_output(raw, projection, base_dir=BASE_DIR)
    except AgentOutputError as exc:
        _log_agent_result(mode=mode, prompt=prompt, started=started,
                          status="output_rejected", cost_usd=cost_usd, error=exc)
        print(f"\n❌ 出力の検証に失敗、保存しません: {exc}")
        return 2

    path = BASE_DIR / OUTPUT_FILES[mode]
    # ⚠️ 検証を通った後の保存失敗も、既知のコストを持つ run として
    # 記録する (API と同じ理由 —— レビューで再現)。
    try:
        saved = save_verified_result(path, verified, as_of=now.isoformat())
    except OSError as exc:
        _log_agent_result(mode=mode, prompt=prompt, started=started,
                          status="persistence_error", cost_usd=cost_usd, error=exc)
        print(f"\n❌ 保存に失敗しました: {exc}")
        return 1
    _log_agent_result(mode=mode, prompt=prompt, started=started,
                      status="success" if saved else "skipped_stale_write",
                      cost_usd=cost_usd)
    if not saved:
        print(f"\n⚠️ より新しい結果が既に保存済み。この run は書きません: {path.name}")
        return 0
    print(f"\n✅ 検証済みの結果を保存: {path.name}")
    for action in verified["actions"][:5]:
        print(f"   {action['rank']}. {action['ticker']} "
              f"{action['action_type']} [{action['actionability']}]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ALMANAC Portfolio Agent")
    parser.add_argument("--mode", choices=list(ENABLED_MODES), default="default",
                        help=f"分析モード（現在有効: {', '.join(ENABLED_MODES)}）")
    parser.add_argument("--print-projection", action="store_true",
                        help="Agent を呼ばず、渡す projection だけを表示する")
    args = parser.parse_args()

    if args.print_projection:
        projection = build_agent_projection(args.mode, base_dir=BASE_DIR)
        print(json.dumps(projection, ensure_ascii=False, indent=2))
        return 0

    return asyncio.run(run_analysis(args.mode))


if __name__ == "__main__":
    raise SystemExit(main())

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
from datetime import datetime, timezone
from pathlib import Path

from agent_projection import (
    AgentOutputError,
    AgentProtocolViolation,
    MODES,
    build_agent_options,
    build_agent_projection,
    build_agent_prompt,
    parse_agent_result,
    validate_agent_output,
)
from utils import atomic_write_json

BASE_DIR = Path(__file__).parent

# ホストが書く。Agent には触らせない。
OUTPUT_FILES = {
    "default": "agent_briefing.json",
    "risk": "risk_agent_report.json",
    "nisa": "nisa_agent_strategy.json",
}


async def run_analysis(mode: str = "default") -> int:
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

    print(f"🤖 Portfolio Agent 起動 [モード: {mode}]")
    print(f"   候補 {len(projection['candidates'])} 件 / ツールなし / 構造化出力")
    print("─" * 50)

    result_payload = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        # ツールを与えていないので、使おうとした時点で契約違反。
                        raise AgentProtocolViolation(
                            f"agent attempted tool use: {block.name}")
                    if isinstance(block, TextBlock) and block.text.strip():
                        print(block.text, end="", flush=True)
            elif isinstance(message, ResultMessage):
                print()
                if message.subtype != "success":
                    print(f"❌ エラー: {message.subtype}")
                    return 1
                result_payload = message
    except AgentProtocolViolation as exc:
        print(f"\n❌ プロトコル違反: {exc}")
        return 1
    except Exception as exc:
        print(f"\n❌ Agent エラー: {exc}")
        print("ヒント: ANTHROPIC_API_KEY が設定されているか確認してください")
        return 1

    # ── ホスト側の検証と保存 ──
    # 検証に落ちたら **保存しない**。last-known-good をそのまま残す。
    try:
        raw = parse_agent_result(result_payload)
        verified = validate_agent_output(raw, projection, base_dir=BASE_DIR)
    except AgentOutputError as exc:
        print(f"\n❌ 出力の検証に失敗、保存しません: {exc}")
        return 2

    path = BASE_DIR / OUTPUT_FILES[mode]
    atomic_write_json(path, {**verified, "as_of": now.isoformat()})
    print(f"\n✅ 検証済みの結果を保存: {path.name}")
    for action in verified["actions"][:5]:
        print(f"   {action['rank']}. {action['ticker']} "
              f"{action['action_type']} [{action['actionability']}]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ALMANAC Portfolio Agent")
    parser.add_argument("--mode", choices=list(MODES), default="default",
                        help="分析モード（default/risk/nisa）")
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

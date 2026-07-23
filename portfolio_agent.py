"""
ALMANAC Portfolio Agent
Claude Agent SDK を使った自律的なポートフォリオ分析オーケストレーター。

portfolio_analyst.py との違い:
  - Agent SDK が自律的にファイルを読み・判断し・出力を書く
  - ツールループを手書きしない（SDK が管理）
  - セッションが自動保存される（~/.claude/projects/ に蓄積）

使い方:
  python portfolio_agent.py           # デフォルト分析
  python portfolio_agent.py --mode risk   # リスク特化分析
  python portfolio_agent.py --mode nisa   # NISA戦略特化
"""

import asyncio
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent

ANALYSIS_PROMPTS = {
    "default": f"""\
あなたは ALMANAC ポートフォリオ AI です。
作業ディレクトリ: {BASE_DIR}

以下のファイルを順番に読み込み、統合分析を行ってください:
1. ai_portfolio_analysis.json - 現在の正式な統合分析
2. technical_state.json       - 現在のテクニカル状態とカバレッジ
3. holdings.json              - 現在の保有ポジション

分析後、以下の構造で agent_briefing.json に書き出してください:
{{
  "as_of": "ISO日時",
  "overall_stance": "defensive/neutral/moderately_aggressive/aggressive",
  "headline": "今日の戦略を1行で",
  "priority_actions": [
    {{"rank": 1, "urgency": "high/medium/low", "ticker": "銘柄", "action": "具体的アクション", "reason": "根拠"}}
  ],
  "risk_warnings": ["警告1", "警告2"],
  "opportunity": "今週の注目機会"
}}
""",
    "risk": f"""\
あなたは ALMANAC リスク管理 AI です。
作業ディレクトリ: {BASE_DIR}

以下を読み込んでリスク集中を分析してください:
1. holdings.json      - ポジション一覧
2. guard_state.json   - 現在のガードレール状態
3. macro_state.json   - マクロ指標

分析観点:
- 通貨別集中リスク（USD/JPY 比率）
- セクター別集中リスク
- 単一銘柄の比率超過（>20% 警告）
- ガードレール発動リスク（日次/月次損益が基準に近いか）

結果を risk_agent_report.json に書き出してください。
""",
    "nisa": f"""\
あなたは ALMANAC NISA 戦略 AI です。
作業ディレクトリ: {BASE_DIR}

以下を読み込んで NISA 最適化戦略を立案してください:
1. nisa_portfolio.json - NISA 保有・枠使用状況
2. long_term_screen_results.json - 長期スクリーニング結果
3. macro_state.json - マクロ指標

分析観点:
- 積立枠の年間ペース（残り月数で達成できるか）
- 成長枠の追加投資余地
- 生涯枠の効率的消化戦略
- 長期スクリーニング上位銘柄とのマッチング

結果を nisa_agent_strategy.json に書き出してください。
""",
}


async def run_analysis(mode: str = "default") -> None:
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage
    except ImportError:
        print("❌ claude-agent-sdk が未インストール: pip install claude-agent-sdk")
        sys.exit(1)

    prompt = ANALYSIS_PROMPTS.get(mode, ANALYSIS_PROMPTS["default"])
    print(f"🤖 Portfolio Agent 起動 [モード: {mode}]")
    print("─" * 50)

    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Write", "Bash"],
        max_turns=20,
    )

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                # Claude のテキスト出力をリアルタイム表示
                for block in message.content:
                    if hasattr(block, "text") and block.text:
                        print(block.text, end="", flush=True)
            elif isinstance(message, ResultMessage):
                print()
                if message.subtype == "success":
                    cost = getattr(message, "total_cost_usd", None)
                    cost_str = f" (${cost:.4f})" if cost else ""
                    print(f"\n✅ 分析完了{cost_str}")
                    if message.result:
                        print(f"結果: {message.result[:200]}")
                else:
                    print(f"\n❌ エラー: {message.subtype}")
                    if hasattr(message, "error") and message.error:
                        print(f"詳細: {message.error}")
    except Exception as e:
        print(f"\n❌ Agent エラー: {e}")
        print("ヒント: ANTHROPIC_API_KEY が設定されているか確認してください")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="ALMANAC Portfolio Agent")
    parser.add_argument(
        "--mode",
        choices=["default", "risk", "nisa"],
        default="default",
        help="分析モード（default/risk/nisa）",
    )
    args = parser.parse_args()
    asyncio.run(run_analysis(args.mode))


if __name__ == "__main__":
    main()

"""
Agent SDK SSE ストリーミングエンドポイント
GET /api/agent/run?mode=default  → text/event-stream でリアルタイム出力
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
AGENT_MAX_TURNS = 10

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

結果を nisa_agent_strategy.json に書き出してください。
""",
}


def _append_llm_call_log(row: dict) -> None:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        _append(row)
    except Exception:
        pass


def _log_agent_result(
    *,
    mode: str,
    prompt: str,
    started: float,
    status: str,
    cost_usd=None,
    error: Exception | None = None,
) -> None:
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": "agent_sdk_run",
        "model": "claude-agent-sdk",
        "use_tool": True,
        "max_turns": AGENT_MAX_TURNS,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": len(prompt),
        "mode": mode,
        "status": status,
    }
    if cost_usd is not None:
        row["cost_usd"] = cost_usd
    if error is not None:
        row.update({
            "error_type": type(error).__name__,
            "error": str(error)[:500],
            "cost_usd": 0.0,
        })
    _append_llm_call_log(row)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _run_agent(mode: str) -> AsyncIterator[str]:
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage
        from claude_agent_sdk.types import TextBlock, ToolUseBlock
    except ImportError:
        yield _sse("error", {"message": "claude-agent-sdk が未インストールです"})
        return

    prompt = ANALYSIS_PROMPTS.get(mode, ANALYSIS_PROMPTS["default"])
    started = time.monotonic()
    # P0-1: allowed_tools を Read のみに削減。
    # 旧設定は Write/Bash を許可しており、未認証 GET エンドポイントと組み合わさって
    # ブラウザ CSRF からホームディレクトリ全域への書き込みが可能だった。
    # Agent SDK の出力は別経路（Telegram or 専用 POST endpoint）で永続化する想定。
    options = ClaudeAgentOptions(
        allowed_tools=["Read"],
        max_turns=AGENT_MAX_TURNS,
    )

    yield _sse("start", {"mode": mode, "message": f"Agent 分析開始 [モード: {mode}]"})

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield _sse("text", {"content": block.text})
                    elif isinstance(block, ToolUseBlock):
                        yield _sse("tool", {
                            "name": block.name,
                            "input": str(block.input)[:200],
                        })
            elif isinstance(message, ResultMessage):
                cost = getattr(message, "total_cost_usd", None)
                _log_agent_result(
                    mode=mode,
                    prompt=prompt,
                    started=started,
                    status=message.subtype,
                    cost_usd=cost,
                )
                if message.subtype == "success":
                    yield _sse("done", {
                        "success": True,
                        "cost_usd": cost,
                        "result": (message.result or "")[:500],
                    })
                else:
                    yield _sse("done", {
                        "success": False,
                        "error": message.subtype,
                    })
            await asyncio.sleep(0)  # イベントループに制御を返す
    except Exception as e:
        _log_agent_result(
            mode=mode,
            prompt=prompt,
            started=started,
            status="error",
            error=e,
        )
        yield _sse("error", {"message": str(e)})


@router.post("/api/agent/run")
async def run_agent(mode: str = "default"):
    """
    P0-1: GET → POST 化。
    認証 middleware が POST のみ X-API-Key を要求するため、未認証ブラウザ CSRF で
    Agent SDK を起動されるリスクを塞ぐ。SSE のレスポンスは POST でも問題なく返せる。
    """
    if mode not in ANALYSIS_PROMPTS:
        mode = "default"
    return StreamingResponse(
        _run_agent(mode),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/agent/result")
async def get_agent_result(mode: str = "default"):
    """最後の Agent 分析結果を返す。agent_briefing.json が古い場合は ai_portfolio_analysis.json にフォールバック"""
    file_map = {
        "default": "agent_briefing.json",
        "risk": "risk_agent_report.json",
        "nisa": "nisa_agent_strategy.json",
    }
    path = BASE_DIR / file_map.get(mode, "agent_briefing.json")

    # defaultモードの場合、両方の実時刻を比較して新しい方を返す。
    if mode == "default":
        ai_path = BASE_DIR / "ai_portfolio_analysis.json"
        try:
            ai_data = json.loads(ai_path.read_text(encoding="utf-8")) if ai_path.exists() else {}
        except Exception:
            ai_data = {}
        try:
            agent_data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            agent_data = {}

        def result_time(data: dict, source_path: Path) -> float:
            for key in ("as_of", "generated_at", "updated_at"):
                raw = data.get(key)
                if raw:
                    try:
                        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass
            try:
                return source_path.stat().st_mtime
            except OSError:
                return 0.0

        if agent_data and result_time(agent_data, path) > result_time(ai_data, ai_path):
            return {**agent_data, "result_source": "agent_briefing"}
        synthesis = ai_data.get("synthesis", {}) if isinstance(ai_data, dict) else {}
        if synthesis:
            return {
                "headline": synthesis.get("morning_brief_headline", ""),
                "overall_stance": synthesis.get("overall_stance", "neutral"),
                "risk_warnings": synthesis.get("risk_warnings", []),
                "priority_actions": synthesis.get("priority_actions", []),
                "as_of": ai_data.get("as_of", ""),
                "result_source": "main_analysis",
            }

    if not path.exists():
        return {"error": "まだ分析が実行されていません"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)}

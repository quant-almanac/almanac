"""
Agent SDK SSE ストリーミングエンドポイント
POST /api/agent/run?mode=default  → text/event-stream でリアルタイム出力

⚠️ 2026-08-25 の再設計 (Codex レビュー round 11):
以前はプロンプトに作業ディレクトリの絶対パスと読むべきファイル名を書き、
allowed_tools=["Read"] を渡していた。プロンプトからファイル名を消しても
Read が残っていれば Agent は raw ファイルへ戻れるので、入力を
sanitized projection にし、ツール自体を外した。CLI (portfolio_agent.py) と
同じ agent_projection の builder / renderer / validator を使う。
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from agent_projection import (
    AGENT_RUN_LOCK_NAME,
    AGENT_RUN_LOCK_TIMEOUT_SECONDS,
    AgentOutputError,
    AgentProtocolViolation,
    MODES,
    build_agent_options,
    build_agent_projection,
    build_agent_prompt,
    parse_agent_result,
    projection_sha256,
    save_verified_result,
    validate_agent_output,
)
from utils import LockBusy, process_lock

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent

# ホストが書く。Agent には触らせない (CLI と同じ表)。
OUTPUT_FILES = {
    "default": "agent_briefing.json",
    "risk": "risk_agent_report.json",
    "nisa": "nisa_agent_strategy.json",
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
        "use_tool": False,  # ツールは与えていない (round 11 以降)
        "max_turns": 1,
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
    """projection 生成から保存までを共有ロックの中で行う。

    CLI と同じロック名を取るので、両者が同時に走って二重課金したり、
    遅く終わった古い run が新しい結果を上書きしたりしない
    (Codex レビュー round 13)。
    """
    try:
        with process_lock(AGENT_RUN_LOCK_NAME, timeout=AGENT_RUN_LOCK_TIMEOUT_SECONDS):
            async for chunk in _run_agent_locked(mode):
                yield chunk
    except LockBusy:
        yield _sse("error", {"message": "別の Agent 実行が進行中です。二重起動しません。"})


async def _run_agent_locked(mode: str) -> AsyncIterator[str]:
    try:
        from claude_agent_sdk import query, ResultMessage, AssistantMessage
        from claude_agent_sdk.types import TextBlock, ToolUseBlock
    except ImportError:
        yield _sse("error", {"message": "claude-agent-sdk が未インストールです"})
        return

    now = datetime.now(timezone.utc)
    try:
        projection = build_agent_projection(mode, base_dir=BASE_DIR, now=now)
    except Exception as exc:
        yield _sse("error", {"message": f"projection の生成に失敗: {exc}"})
        return

    prompt = build_agent_prompt(projection)
    started = time.monotonic()
    # ツールを一切与えない。以前は allowed_tools=["Read"] で、プロンプトから
    # ファイル名を消しても Agent は raw ファイルへ戻れた
    # (Codex レビュー round 11)。入力は projection だけ、出力は構造化スキーマ。
    options = build_agent_options()

    yield _sse("start", {
        "mode": mode,
        "message": f"Agent 分析開始 [モード: {mode}]",
        # request_id 等の実行固有情報は payload hash の外。hash 自体は
        # 「どの projection を見て出した結論か」の監査に必要なので出す。
        "projection_sha256": projection_sha256(projection),
        "candidates": len(projection["candidates"]),
    })

    result_payload = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        # ツールを与えていないので、使おうとした時点で契約違反。
                        raise AgentProtocolViolation(
                            f"agent attempted tool use: {block.name}")
                    # ⚠️ TextBlock は配信しない。scope 外の銘柄に触れる
                    # 自由文が検証前に画面へ出ると、構造化 action を縛った
                    # 意味が薄れる (Codex レビュー round 13)。
                    if isinstance(block, TextBlock):
                        continue
            elif isinstance(message, ResultMessage):
                cost = getattr(message, "total_cost_usd", None)
                _log_agent_result(
                    mode=mode,
                    prompt=prompt,
                    started=started,
                    status=message.subtype,
                    cost_usd=cost,
                )
                if message.subtype != "success":
                    yield _sse("done", {"success": False, "error": message.subtype})
                    return
                result_payload = message
            await asyncio.sleep(0)  # イベントループに制御を返す
    except AgentProtocolViolation as e:
        _log_agent_result(mode=mode, prompt=prompt, started=started,
                          status="protocol_violation", error=e)
        yield _sse("error", {"message": f"プロトコル違反: {e}"})
        return
    except Exception as e:
        _log_agent_result(mode=mode, prompt=prompt, started=started,
                          status="error", error=e)
        yield _sse("error", {"message": str(e)})
        return

    # ── ホスト側の検証と保存 ──
    # 検証に落ちたら **保存しない**。last-known-good をそのまま残し、
    # 監査ログへ隔離する。
    try:
        raw = parse_agent_result(result_payload)
        verified = validate_agent_output(raw, projection, base_dir=BASE_DIR)
    except AgentOutputError as e:
        _log_agent_result(mode=mode, prompt=prompt, started=started,
                          status="output_rejected", error=e)
        yield _sse("error", {"message": f"出力の検証に失敗、保存しません: {e}"})
        return

    saved = save_verified_result(BASE_DIR / OUTPUT_FILES[mode], verified,
                                 as_of=now.isoformat())
    yield _sse("done", {
        "success": True,
        "saved": OUTPUT_FILES[mode] if saved else None,
        "skipped_stale_write": not saved,
        "actions": len(verified["actions"]),
        "projection_sha256": verified["projection_sha256"],
    })


@router.post("/api/agent/run")
async def run_agent(mode: str = "default"):
    """
    P0-1: GET → POST 化。
    認証 middleware が POST のみ X-API-Key を要求するため、未認証ブラウザ CSRF で
    Agent SDK を起動されるリスクを塞ぐ。SSE のレスポンスは POST でも問題なく返せる。
    """
    if mode not in MODES:
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
    path = BASE_DIR / OUTPUT_FILES.get(mode, "agent_briefing.json")

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

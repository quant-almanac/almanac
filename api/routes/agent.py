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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

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
    projection_sha256,
    resolve_agent_model,
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


def _append_llm_call_log(row: dict) -> bool:
    """会計ログへ1行追加する。書けたかどうかを返す。

    ⚠️ 従来は例外を握り潰すだけで戻り値も無かった。真のディスク障害では
    save_verified_result の失敗とこのログ書き込みの失敗が **同時に** 起き
    うる —— その場合、既に確定した cost_usd がどこにも残らず消える
    (レビューで実測: 出力先を存在しないパスへ差し替えて再現)。
    呼び出し側 (_log_agent_result) が成否を見て stderr / SSE へ退避できる
    よう、bool を返すようにする。
    """
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        return bool(_append(row))
    except Exception:
        return False


def _log_agent_result(
    *,
    mode: str,
    prompt: str,
    started: float,
    status: str,
    cost_usd=None,
    error: Exception | None = None,
) -> dict:
    """会計ログへ1行記録し、その行を返す (常に返す —— 書き込みの成否に
    関わらず)。呼び出し側は返り値の cost_usd/model/status を SSE へ載せる
    ことで、監査ログ自体が書けない状況でもクライアント側に情報を残せる。"""
    # ⚠️ model は実際の ID を記録する。"claude-agent-sdk" という総称だと
    # 「どのモデルにいくら使ったか」を後から検証できない
    # (Codex レビュー round 14)。
    try:
        model = resolve_agent_model()
    except Exception:
        model = "unresolved"
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": "agent_sdk_run",
        "model": model,
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
        row["error_type"] = type(error).__name__
        row["error"] = str(error)[:500]
        # ⚠️ cost_usd を無条件に 0.0 で上書きしない。判明済みのコストがある
        # 呼び出し (例えば output_rejected は ResultMessage 後なので実コスト
        # を持つ) でそれを消してしまっていた (レビューで再現)。既知なら保持、
        # 未知なら 0 円と断定せず欠損のままにする。
        if cost_usd is None:
            row.setdefault("cost_status", "unknown")
    if not _append_llm_call_log(row):
        # 監査ログへ書けなかった。同じディスク障害が原因なら、この行が
        # 唯一の記録になりうる。stderr は systemd/launchd のログへ拾われる
        # ので、少なくとも運用者が後から grep できる。
        print(f"⚠️ agent_sdk_run accounting log write failed, row follows: "
              f"{json.dumps(row, ensure_ascii=False)}", file=sys.stderr)
    return row


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_error(message: str, row: dict) -> str:
    """会計ログの行から cost/model/status を SSE 側にも複製する。

    監査ログ (JSONL) 自体が書けない状況 (真のディスク障害) でも、
    クライアントへ配信される SSE にだけは cost_usd/model/status が残る
    ようにする —— これが唯一生き残る記録になりうる (レビューで指摘)。
    """
    payload = {"message": message}
    for key in ("cost_usd", "model", "status"):
        if key in row:
            payload[key] = row[key]
    return _sse("error", payload)


async def _run_agent(mode: str) -> AsyncIterator[str]:
    """projection 生成から保存までを共有ロックの中で行う。

    CLI と同じロック名を取るので、両者が同時に走って二重課金したり、
    遅く終わった古い run が新しい結果を上書きしたりしない
    (Codex レビュー round 13)。
    """
    try:
        # ⚠️ timeout=0。process_lock の待機は同期 time.sleep で、
        # FastAPI の event loop ごと止めてしまう (Codex レビュー round 14)。
        # API 側は即座に LockBusy にして呼び出し元へ返す。
        with process_lock(AGENT_RUN_LOCK_NAME, timeout=0):
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
    cost = None
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
                # ⚠️ ここでは記録しない。検証を通るまで結果は確定しない ——
                # 先に success を書くと、その後 output_rejected になっても
                # 会計ログには2行残り、最終状態が読めない
                # (レビューで success → output_rejected の2行を再現)。
                cost = getattr(message, "total_cost_usd", None)
                if message.subtype != "success":
                    row = _log_agent_result(mode=mode, prompt=prompt, started=started,
                                            status=message.subtype, cost_usd=cost)
                    yield _sse("done", {"success": False, "error": message.subtype,
                                        "cost_usd": cost, "model": row.get("model")})
                    return
                result_payload = message
            await asyncio.sleep(0)  # イベントループに制御を返す
    except AgentProtocolViolation as e:
        row = _log_agent_result(mode=mode, prompt=prompt, started=started,
                                status="protocol_violation", error=e)
        yield _sse_error(f"プロトコル違反: {e}", row)
        return
    except Exception as e:
        row = _log_agent_result(mode=mode, prompt=prompt, started=started,
                                status="error", error=e)
        yield _sse_error(str(e), row)
        return

    # ── ホスト側の検証と保存 ──
    # 検証に落ちたら **保存しない**。last-known-good をそのまま残し、
    # 監査ログへ隔離する。
    try:
        raw = parse_agent_result(result_payload)
        verified = validate_agent_output(raw, projection, base_dir=BASE_DIR)
    except AgentOutputError as e:
        row = _log_agent_result(mode=mode, prompt=prompt, started=started,
                                status="output_rejected", cost_usd=cost, error=e)
        yield _sse_error(f"出力の検証に失敗、保存しません: {e}", row)
        return

    # ⚠️ 検証を通った後、保存そのものが失敗しうる (ディスク満杯等)。
    # 課金は ResultMessage の時点で確定しているので、ここで例外を外へ
    # 投げっぱなしにすると、既知のコストを持つ run が会計ログに一行も
    # 残らず消える (レビューで再現: OSError 注入 → 例外伝播・ログ0行)。
    try:
        saved = save_verified_result(BASE_DIR / OUTPUT_FILES[mode], verified,
                                     as_of=now.isoformat())
    except OSError as e:
        row = _log_agent_result(mode=mode, prompt=prompt, started=started,
                                status="persistence_error", cost_usd=cost, error=e)
        yield _sse_error(f"保存に失敗しました: {e}", row)
        return
    # 検証を通ってから、最終 status と実コストを1行だけ記録する。
    _log_agent_result(mode=mode, prompt=prompt, started=started,
                      status="success" if saved else "skipped_stale_write",
                      cost_usd=cost)
    yield _sse("done", {
        "success": True,
        "saved": OUTPUT_FILES[mode] if saved else None,
        "skipped_stale_write": not saved,
        "actions": len(verified["actions"]),
        "projection_sha256": verified["projection_sha256"],
        # UI の費用表示はこれを読む。以前は done に載っておらず発火しなかった。
        "cost_usd": cost,
    })


@router.post("/api/agent/run")
async def run_agent(mode: str = "default"):
    """
    P0-1: GET → POST 化。
    認証 middleware が POST のみ X-API-Key を要求するため、未認証ブラウザ CSRF で
    Agent SDK を起動されるリスクを塞ぐ。SSE のレスポンスは POST でも問題なく返せる。
    """
    if mode not in ENABLED_MODES:
        # risk / nisa は projection の判断材料がまだ正しくないので拒否する。
        # 黙って default へ倒すと、利用者は risk を見たつもりになる。
        return JSONResponse(
            status_code=409,
            content={"error": f"mode {mode!r} is currently disabled",
                     "enabled_modes": list(ENABLED_MODES)},
        )
    return StreamingResponse(
        _run_agent(mode),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/agent/enabled-modes")
async def get_enabled_modes():
    """UI がタブを組み立てるための権威。

    画面側に候補を直書きすると、backend で無効化しても表示だけ残る
    (レビューで実際にそうなっていた)。
    """
    return {"enabled_modes": list(ENABLED_MODES), "all_modes": list(MODES)}


@router.get("/api/agent/result")
async def get_agent_result(mode: str = "default"):
    """最後の Agent 分析結果を返す。agent_briefing.json が古い場合は ai_portfolio_analysis.json にフォールバック"""
    # ⚠️ 閲覧も無効モードでは拒否する。実行だけ止めても、以前保存された
    # 信頼できない結果を読めてしまう。未知モードを default へ倒すのも
    # 危険 —— 利用者は risk を見たつもりで総合分析を読む
    # (レビューで GET mode=unknown が総合分析を返すのを再現)。
    if mode not in ENABLED_MODES:
        return JSONResponse(
            status_code=409,
            content={"error": f"mode {mode!r} is currently disabled or unknown",
                     "enabled_modes": list(ENABLED_MODES)},
        )
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

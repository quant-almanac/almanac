"""
POST /api/chat  — アクションカードのコンテキスト付きチャット（Claude Haiku ストリーミング）
"""
import json
import os
import sys
import time
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from utils import load_json as _load_json

CHAT_MODEL = "claude-haiku-4-5-20251001"


def _append_llm_call_log(row: dict) -> None:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        _append(row)
    except Exception:
        pass


def _content_len(value) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except Exception:
        return len(str(value))


def _log_chat_usage(
    *,
    started: float,
    system: str,
    messages: list[dict],
    response=None,
    status: str = "ok",
    error: Exception | None = None,
) -> None:
    usage = getattr(response, "usage", None)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": "chat_stream",
        "model": CHAT_MODEL,
        "use_tool": False,
        "max_tokens": 1024,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": len(system) + sum(_content_len(m.get("content", "")) for m in messages),
        "message_count": len(messages),
        "status": status,
    }
    if response is not None:
        row.update({
            "stop_reason": getattr(response, "stop_reason", None),
            "content_types": [getattr(block, "type", None) for block in getattr(response, "content", [])],
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        })
    if error is not None:
        row.update({
            "error_type": type(error).__name__,
            "error": str(error)[:500],
        })
    _append_llm_call_log(row)


def _build_context(action_ctx: dict | None) -> str:
    """ポートフォリオ概要 + アクションコンテキストをシステムプロンプト用に構築"""
    account  = _load_json(BASE_DIR / "account.json")
    guard    = _load_json(BASE_DIR / "guard_state.json")
    regime   = _load_json(BASE_DIR / "regime_state.json")
    holdings = _load_json(BASE_DIR / "holdings.json")
    analysis = _load_json(BASE_DIR / "ai_portfolio_analysis.json")

    spy_above = regime.get("spy_above", True)
    nk_above  = regime.get("nk_above", True)
    regime_label = "A_強気" if (spy_above and nk_above) else ("C_弱気" if not spy_above and not nk_above else "B_中立")

    pos_lines = []
    for k, v in list(holdings.items())[:8]:
        ticker = v.get("ticker", k)
        name   = v.get("name", "")[:12]
        shares = v.get("shares", 0)
        entry  = v.get("entry_price", 0)
        pos_lines.append(f"  {ticker}({name}): {shares}株 @{entry}")

    synthesis = analysis.get("synthesis", {})
    stance    = synthesis.get("overall_stance", "不明")
    theme     = synthesis.get("weekly_theme", "")

    # P0-1: FX は utils.get_fx_rate_cached() 経由（stale fallback あり）
    try:
        from utils import get_fx_rate_cached
        _fx, _ = get_fx_rate_cached(account_json_path=BASE_DIR / "account.json")
        _fx_display = float(_fx)
    except Exception:
        _fx_display = float(account.get('fx_rate_usdjpy', 150))

    ctx = f"""=== ポートフォリオ概要 ===
総資産: ¥{account.get('balance', 0) + round(account.get('usd_balance', 0) * _fx_display):,}
JPY残高: ¥{account.get('balance', 0):,} / USD残高: ${account.get('usd_balance', 0):,.0f}
レジーム: {regime_label} / スタンス: {stance}
週間テーマ: {theme}
ガードレール: {'制限中' if not guard.get('trading_allowed', True) else '正常'}

=== 主要保有銘柄 ===
{chr(10).join(pos_lines)}
"""

    if action_ctx:
        ctx += f"""
=== 今ユーザーが見ているアクションカード ===
ティッカー : {action_ctx.get('ticker', 'なし')}
方向       : {action_ctx.get('direction', '')}（{action_ctx.get('type', '')}）
アクション : {action_ctx.get('action', '')}
理由       : {action_ctx.get('reason', '')}
目安金額   : {action_ctx.get('amount_hint', 'なし')}
緊急度     : {action_ctx.get('urgency', '')}
ティア     : {action_ctx.get('tier', '')}
"""

    return ctx


def _stream_claude(system: str, messages: list[dict]):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    claude_msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
    started = time.monotonic()
    try:
        with client.messages.stream(
            model=CHAT_MODEL,
            max_tokens=1024,
            system=system,
            messages=claude_msgs,
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"
            final_message = stream.get_final_message() if hasattr(stream, "get_final_message") else None
        _log_chat_usage(
            started=started,
            system=system,
            messages=claude_msgs,
            response=final_message,
        )
    except Exception as e:
        _log_chat_usage(
            started=started,
            system=system,
            messages=claude_msgs,
            status="error",
            error=e,
        )
        raise
    yield "data: [DONE]\n\n"


@router.post("/api/chat")
async def chat_endpoint(body: dict):
    messages       = body.get("messages", [])
    action_context = body.get("action_context")   # アクションカードの内容

    sys.path.insert(0, str(BASE_DIR))
    portfolio_ctx = _build_context(action_context)

    system = f"""{portfolio_ctx}

=== あなたの役割 ===
あなたはユーザーの専属ポートフォリオアドバイザー「ALMANAC AI」です。
今ユーザーが見ているアクションカードの内容を中心に、具体的・実践的なアドバイスをしてください。

【スタンス】
- アクションカードの根拠・リスク・代替案について率直に答える
- 不確実な点は正直に伝える
- 日本語で回答（300字程度を目安、長くなる場合は箇条書き）
- 最終判断はユーザー自身が行う旨を必要に応じて添える"""

    return StreamingResponse(
        _stream_claude(system, messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

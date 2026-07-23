"""
ollama_chat.py — ALMANAC v4.0
ローカル Ollama LLM (qwen2.5:7b) にポートフォリオコンテキストを注入して
チャットする。Streamlit から呼び出す関数群を提供する。

フォールバック: Ollama が利用不可の場合は Claude Haiku-4.5 を使用。

依存: ollama (pip install ollama)  ※ Ollama アプリも起動必要
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional
import anthropic

BASE = Path(__file__).parent


CHAT_FALLBACK_MODEL = "claude-haiku-4-5-20251001"


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


def _log_claude_chat_usage(
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
        "role": "ollama_chat_claude_fallback",
        "model": CHAT_FALLBACK_MODEL,
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


# --------------------------------------------------------------------------- #
# ポートフォリオコンテキスト構築
# --------------------------------------------------------------------------- #

def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default or {}


def build_portfolio_context() -> str:
    """全データソースから LLM 用コンテキストを構築"""
    account   = _load_json(BASE / "account.json")
    guard     = _load_json(BASE / "guard_state.json")
    regime    = _load_json(BASE / "regime_state.json")
    signals   = _load_json(BASE / "signals_log.json")
    holdings  = _load_json(BASE / "holdings.json")
    analysis  = _load_json(BASE / "ai_portfolio_analysis.json")
    synthesis = analysis.get("synthesis") or {}
    briefing  = {
        "summary": synthesis.get("morning_brief_headline") or synthesis.get("stance_reason") or "",
        "actions": [
            f"{row.get('ticker')}: {row.get('action') or row.get('type')}"
            for row in (synthesis.get("priority_actions") or []) if isinstance(row, dict)
        ],
    }
    rebalance = _load_json(BASE / "rebalance_report.json")

    # レジーム
    spy_above = regime.get("spy_above", False)
    nk_above  = regime.get("nk_above", False)
    if spy_above and nk_above:
        regime_label = "A_強気"
    elif not spy_above and not nk_above:
        regime_label = "C_弱気"
    else:
        regime_label = "B_中立"

    # ガードレール
    guard_ok = guard.get("new_entry_allowed", True) and guard.get("trading_allowed", True)
    guard_str = "正常" if guard_ok else "⛔ 制限中"

    # シグナル
    signal_lines = []
    for ticker, sig in list(signals.items())[:5]:
        signal_lines.append(
            f"  {ticker}: エントリー${sig.get('entry_price','?')} "
            f"→ 目標${sig.get('target_price','?')} "
            f"/ ストップ${sig.get('stop_loss','?')} "
            f"(スコア{sig.get('score','?')})"
        )

    # 保有銘柄（主要10件）
    holding_lines = []
    if isinstance(holdings, dict):
        for ticker, info in list(holdings.items())[:10]:
            if isinstance(info, dict):
                holding_lines.append(
                    f"  {ticker}: {info.get('shares','?')}株 "
                    f"@ ¥{info.get('avg_cost',0):,.0f}"
                )

    # リバランス
    rebal_actions = []
    if isinstance(rebalance, dict) and "action_plan" in rebalance:
        for a in rebalance["action_plan"][:3]:
            rebal_actions.append(f"  {a.get('action','')}: {a.get('ticker','')} - {a.get('reason','')[:50]}")

    # 朝ブリーフィングのサマリー
    briefing_summary = briefing.get("summary", "（本日のブリーフィング未生成）")
    briefing_actions = briefing.get("actions", [])

    ctx = f"""=== ALMANAC ポートフォリオコンテキスト ({datetime.now().strftime('%Y-%m-%d %H:%M')}) ===

【投資家情報】
- ユーザー（勤務先非公開・個人投資家）
- 総資産: ¥{account.get('total_assets_jpy', 30_639_795):,.0f}
- 日次損益: {guard.get('daily_pnl_pct', 0.0):+.2f}%
- 月次損益: {guard.get('monthly_pnl_pct', 0.0):+.2f}%

【市場状態】
- レジーム: {regime_label}（SPY200MA超:{spy_above} / NK200MA超:{nk_above}）
- ガードレール: {guard_str}

【AIシグナル（直近）】
{chr(10).join(signal_lines) if signal_lines else '  なし'}

【主要保有銘柄】
{chr(10).join(holding_lines) if holding_lines else '  データなし'}

【リバランス推奨】
{chr(10).join(rebal_actions) if rebal_actions else '  なし'}

【今朝のAIブリーフィング】
{briefing_summary}
今日やること: {' / '.join(briefing_actions[:3])}
"""
    return ctx


SYSTEM_PROMPT_TEMPLATE = """{portfolio_context}

=== あなたの役割 ===
あなたは「ALMANAC AIアシスタント」です。
上記のポートフォリオデータを踏まえて、ユーザーの投資判断をサポートします。

【スタンス】
- 具体的・実践的なアドバイスを提供する
- 不確実性は正直に伝える（「わかりません」も OK）
- 日本語で回答する
- 投資判断の最終責任はユーザーにある旨を適宜添える
- 長すぎず、要点を絞って答える（300字程度を目安）

【得意分野】
- 個別銘柄の分析・判断
- ポートフォリオ配分の考え方
- リスク管理・ガードレールの解釈
- 相場レジームに応じた戦略
- 持株会・NISA・税務の一般的な考え方"""


# --------------------------------------------------------------------------- #
# Ollama チェック
# --------------------------------------------------------------------------- #

def is_ollama_available(model: str = "qwen2.5:7b") -> bool:
    """Ollama が起動中で指定モデルが利用可能か確認"""
    try:
        import ollama
        models = ollama.list()
        names = [m.model for m in models.models]
        return any(model in n for n in names)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# チャット（ストリーミング）
# --------------------------------------------------------------------------- #

def chat_stream_ollama(
    messages: list[dict],
    model: str = "qwen2.5:7b"
) -> Generator[str, None, None]:
    """Ollama でストリーミングチャット。テキストチャンクを yield する。"""
    import ollama

    portfolio_ctx = build_portfolio_context()
    system = SYSTEM_PROMPT_TEMPLATE.format(portfolio_context=portfolio_ctx)

    ollama_msgs = [{"role": "system", "content": system}]
    for m in messages:
        ollama_msgs.append({"role": m["role"], "content": m["content"]})

    stream = ollama.chat(model=model, messages=ollama_msgs, stream=True)
    for chunk in stream:
        delta = chunk.message.content
        if delta:
            yield delta


def chat_stream_claude(
    messages: list[dict],
    model: str = CHAT_FALLBACK_MODEL
) -> Generator[str, None, None]:
    """Claude Haiku でストリーミングチャット（Ollama フォールバック）。"""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    portfolio_ctx = build_portfolio_context()
    system = SYSTEM_PROMPT_TEMPLATE.format(portfolio_context=portfolio_ctx)

    claude_msgs = [{"role": m["role"], "content": m["content"]} for m in messages]

    started = time.monotonic()
    try:
        with client.messages.stream(
            model=model,
            max_tokens=1024,
            system=system,
            messages=claude_msgs
        ) as stream:
            for text in stream.text_stream:
                yield text
            final_message = stream.get_final_message() if hasattr(stream, "get_final_message") else None
        _log_claude_chat_usage(
            started=started,
            system=system,
            messages=claude_msgs,
            response=final_message,
        )
    except Exception as e:
        _log_claude_chat_usage(
            started=started,
            system=system,
            messages=claude_msgs,
            status="error",
            error=e,
        )
        raise


def chat_stream(
    messages: list[dict],
    prefer_ollama: bool = True,
    ollama_model: str = "qwen2.5:7b"
) -> tuple[Generator[str, None, None], str]:
    """
    メインのチャット関数。Ollama → Claude Haiku の順にフォールバック。
    Returns: (generator, backend_name)
    """
    if prefer_ollama:
        try:
            if is_ollama_available(ollama_model):
                return chat_stream_ollama(messages, ollama_model), f"Ollama ({ollama_model})"
        except ImportError:
            pass

    return chat_stream_claude(messages), "Claude Haiku-4.5"


# --------------------------------------------------------------------------- #
# Streamlit 用ヘルパー
# --------------------------------------------------------------------------- #

def render_chat_panel(container=None):
    """
    Streamlit のコンテナ内にチャット UI を描画する。
    container が None の場合は st 直接使用。
    使用例:
        with st.sidebar:
            ollama_chat.render_chat_panel()
    """
    import streamlit as st

    tgt = container or st

    tgt.markdown("### 🤖 AI チャット")

    # バックエンド選択
    use_ollama = tgt.toggle(
        "Ollama (ローカル)",
        value=True,
        help="OFF にすると Claude Haiku を使用（API課金）"
    )

    # メッセージ履歴
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    # チャット履歴表示（最新5件）
    chat_container = tgt.container(height=350)
    with chat_container:
        recent = st.session_state.chat_messages[-10:]
        for msg in recent:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    # 入力
    if prompt := tgt.chat_input("ポートフォリオについて質問…"):
        st.session_state.chat_messages.append({"role": "user", "content": prompt})

        with chat_container:
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                placeholder = st.empty()
                full_text = ""

                try:
                    gen, backend = chat_stream(
                        st.session_state.chat_messages,
                        prefer_ollama=use_ollama
                    )
                    for chunk in gen:
                        full_text += chunk
                        placeholder.markdown(full_text + "▌")
                    placeholder.markdown(full_text)

                    # フッター
                    st.caption(f"_powered by {backend}_")

                    # Claude Sonnet へのエスカレーション
                    if tgt.button("🔬 Claude Sonnet に深掘りさせる", key=f"escalate_{len(st.session_state.chat_messages)}"):
                        st.session_state["escalate_prompt"] = prompt

                except Exception as e:
                    placeholder.error(f"エラー: {e}")
                    full_text = f"エラーが発生しました: {e}"

                st.session_state.chat_messages.append(
                    {"role": "assistant", "content": full_text}
                )

    # クリアボタン
    if st.session_state.chat_messages:
        if tgt.button("🗑️ 会話をクリア", use_container_width=True):
            st.session_state.chat_messages = []
            st.rerun()


# --------------------------------------------------------------------------- #
# CLI テスト
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    print("=== Ollama Chat テスト ===")
    print(f"Ollama 利用可能: {is_ollama_available()}")
    print()

    test_msg = sys.argv[1] if len(sys.argv) > 1 else "NVDAのシグナルを教えて"
    messages = [{"role": "user", "content": test_msg}]

    print(f"Q: {test_msg}")
    print("A: ", end="", flush=True)

    gen, backend = chat_stream(messages)
    for chunk in gen:
        print(chunk, end="", flush=True)
    print()
    print(f"\n[backend: {backend}]")

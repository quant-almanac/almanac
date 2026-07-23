"""
llm_adapters.py — DeepSeek / Qwen / Gemini の統一呼出 adapter

全 adapter は以下の統一形式を返す:
    {
        "content": str,         # 本文
        "usage":   dict,        # {"prompt_tokens": int, "completion_tokens": int}
        "model":   str,         # 実際に応答したモデル ID
        "adapter": str,         # "deepseek" / "qwen" / "gemini_flash"
    }

エラー時は例外送出ではなく {"content": "", "error": "<msg>", ...} を返す。
Red Team など並列呼出で片方だけ失敗しても他が続くように。

環境変数:
    DEEPSEEK_API_KEY, DASHSCOPE_API_KEY, GOOGLE_AI_API_KEY
    （~/.almanac_secrets 経由で起動時に load_environment_secrets で読む想定。
    legacy ~/.nexustrader_secrets も fallback）
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

__all__ = [
    "call_deepseek",
    "call_qwen",
    "call_gemini",
    "call_by_role",
    "AdapterResult",
]

AdapterResult = dict[str, Any]

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_DEFAULT_TIMEOUT = 60.0
_MAX_RETRIES = 2


def _retry_openai_compat(base_url: str, api_key: str, model: str,
                         system: str, user: str,
                         max_tokens: int, temperature: float,
                         json_mode: bool, adapter_name: str,
                         request_timeout: float | None = None) -> AdapterResult:
    """OpenAI SDK 互換のエンドポイント共通呼び出し（DeepSeek / Qwen）"""
    try:
        from openai import OpenAI
    except ImportError:
        return {"content": "", "error": "openai SDK not installed", "adapter": adapter_name}

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=_DEFAULT_TIMEOUT)
    kwargs: dict[str, Any] = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if json_mode:
        # DeepSeek / Qwen は response_format={"type": "json_object"} を一部サポート
        kwargs["response_format"] = {"type": "json_object"}

    # max_tokens に比例した per-request タイムアウト（60s〜300s）。
    # tier runner から明示 timeout が来た場合はそれを優先する。
    if request_timeout is not None:
        try:
            req_timeout = max(1.0, float(request_timeout))
        except (TypeError, ValueError):
            req_timeout = _DEFAULT_TIMEOUT
    else:
        _max_tok = kwargs.get("max_tokens", 2000)
        req_timeout = max(60.0, min(300.0, _max_tok * 0.05))

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(**kwargs, timeout=req_timeout)
            content = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            usage_dict: dict[str, int] = {}
            if usage:
                usage_dict = {
                    "prompt_tokens":     getattr(usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                    "total_tokens":      getattr(usage, "total_tokens", 0) or 0,
                }
            return {
                "content": content,
                "usage":   usage_dict,
                "model":   getattr(resp, "model", model),
                "adapter": adapter_name,
            }
        except Exception as e:  # noqa: BLE001 — adapter 側は握り潰して dict を返す契約
            last_err = e
            # 429 / 529 相当は指数バックオフ、他は即座に抜ける
            msg = str(e).lower()
            if attempt < _MAX_RETRIES and ("rate" in msg or "429" in msg or "529" in msg or "overloaded" in msg):
                time.sleep(2 ** attempt)
                continue
            break

    return {
        "content": "",
        "usage":   {},
        "model":   model,
        "adapter": adapter_name,
        "error":   str(last_err) if last_err else "unknown",
    }


def call_deepseek(system: str, user: str, *,
                   model: str = "deepseek-v4-flash",
                   max_tokens: int = 2000,
                   temperature: float = 0.7,
                   json_mode: bool = False,
                   request_timeout: float | None = None) -> AdapterResult:
    """DeepSeek V4-flash chat / R1 reasoner を呼び出す。"""
    try:
        from utils import load_environment_secrets
        load_environment_secrets()
    except Exception:
        pass
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {"content": "", "error": "DEEPSEEK_API_KEY not set", "adapter": "deepseek", "model": model}
    return _retry_openai_compat(
        base_url=_DEEPSEEK_BASE_URL,
        api_key=api_key,
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=json_mode,
        adapter_name="deepseek",
        request_timeout=request_timeout,
    )


def call_qwen(system: str, user: str, *,
              model: str = "qwen2.5-72b-instruct",
              max_tokens: int = 2000,
              temperature: float = 0.7,
              json_mode: bool = False,
              request_timeout: float | None = None) -> AdapterResult:
    """Qwen 2.5 72B を DashScope OpenAI 互換経由で呼び出す。
    DASHSCOPE_API_KEY が無い場合は OpenRouter (OPENROUTER_API_KEY) → Groq (GROQ_API_KEY) の順で fallback."""
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if api_key:
        return _retry_openai_compat(
            base_url=_DASHSCOPE_BASE_URL, api_key=api_key, model=model,
            system=system, user=user, max_tokens=max_tokens, temperature=temperature,
            json_mode=json_mode, adapter_name="qwen", request_timeout=request_timeout,
        )
    # Fallback 1: OpenRouter 経由で qwen 系モデル
    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    if or_key:
        return _retry_openai_compat(
            base_url="https://openrouter.ai/api/v1",
            api_key=or_key,
            model="qwen/qwen-2.5-72b-instruct",
            system=system, user=user, max_tokens=max_tokens, temperature=temperature,
            json_mode=json_mode, adapter_name="qwen_via_openrouter", request_timeout=request_timeout,
        )
    # Fallback 2: Groq (Llama 3.1 70B — Qwen と同格の open-weight 大型)
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        return _retry_openai_compat(
            base_url="https://api.groq.com/openai/v1",
            api_key=groq_key,
            model="llama-3.3-70b-versatile",
            system=system, user=user, max_tokens=max_tokens, temperature=temperature,
            json_mode=json_mode, adapter_name="groq_llama_fallback", request_timeout=request_timeout,
        )
    return {"content": "", "error": "Qwen key chain exhausted (DASHSCOPE/OPENROUTER/GROQ all missing)",
            "adapter": "qwen", "model": model}


def call_gemini(system: str, user: str, *,
                model: str = "gemini-flash-latest",
                max_tokens: int = 2000,
                temperature: float = 0.7,
                json_mode: bool = False,
                request_timeout: float | None = None) -> AdapterResult:
    """Google Gemini 2.0 Flash (experimental) を呼び出す。"""
    # GEMINI_API_KEY と GOOGLE_AI_API_KEY の両方を受け入れる（secrets の慣習に合わせて）
    api_key = os.environ.get("GOOGLE_AI_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    if not api_key:
        return {"content": "", "error": "GEMINI_API_KEY / GOOGLE_AI_API_KEY neither set", "adapter": "gemini_flash", "model": model}

    try:
        import google.generativeai as genai  # type: ignore[import-not-found]
    except ImportError:
        return {"content": "", "error": "google-generativeai not installed", "adapter": "gemini_flash", "model": model}

    genai.configure(api_key=api_key)

    # System instruction は genai ではコンストラクタ引数
    generation_config: dict[str, Any] = {
        "max_output_tokens": max_tokens,
        "temperature":       temperature,
    }
    if json_mode:
        generation_config["response_mime_type"] = "application/json"

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            g_model = genai.GenerativeModel(
                model_name=model,
                system_instruction=system,
                generation_config=generation_config,
            )
            if request_timeout is not None:
                resp = g_model.generate_content(user, request_options={"timeout": request_timeout})
            else:
                resp = g_model.generate_content(user)
            # resp.text でプレーンテキスト取得（パーツが複数ある場合あり）
            content = ""
            try:
                content = (resp.text or "").strip()
            except Exception:
                # fallback
                parts = getattr(resp, "candidates", [])
                if parts:
                    content = "".join(
                        getattr(p, "text", "") for c in parts for p in getattr(getattr(c, "content", None), "parts", [])
                    ).strip()

            usage_meta = getattr(resp, "usage_metadata", None)
            usage_dict: dict[str, int] = {}
            if usage_meta:
                usage_dict = {
                    "prompt_tokens":     getattr(usage_meta, "prompt_token_count", 0) or 0,
                    "completion_tokens": getattr(usage_meta, "candidates_token_count", 0) or 0,
                    "total_tokens":      getattr(usage_meta, "total_token_count", 0) or 0,
                }
            return {
                "content": content,
                "usage":   usage_dict,
                "model":   model,
                "adapter": "gemini_flash",
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e).lower()
            if attempt < _MAX_RETRIES and ("rate" in msg or "429" in msg or "quota" in msg):
                time.sleep(2 ** attempt)
                continue
            break

    return {
        "content": "",
        "usage":   {},
        "model":   model,
        "adapter": "gemini_flash",
        "error":   str(last_err) if last_err else "unknown",
    }


def call_by_role(role: str, system: str, user: str, *,
                 max_tokens: int = 2000,
                 temperature: float = 0.7,
                 json_mode: bool = False,
                 request_timeout: float | None = None) -> AdapterResult:
    """
    model_router に基づき role → adapter を dispatch する。
    Anthropic 系 role を渡しても動く（analyst/llm_client.call_claude 経由）。
    """
    from model_router import get_model, resolve_adapter

    adapter = resolve_adapter(role)
    model_id = get_model(role)

    if adapter == "anthropic":
        # Anthropic SDK 経由（既存 llm_client.call_claude を使う）
        try:
            from analyst.llm_client import call_claude
            content = call_claude(
                system=system,
                user=user,
                model=model_id,
                max_tokens=max_tokens,
                temperature=temperature,
                use_tool=False,
                request_timeout=request_timeout,
            )
            return {
                "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                "usage":   {},
                "model":   model_id,
                "adapter": "anthropic",
            }
        except Exception as e:  # noqa: BLE001
            return {"content": "", "error": str(e), "adapter": "anthropic", "model": model_id}

    if adapter == "deepseek":
        return call_deepseek(system, user, model=model_id, max_tokens=max_tokens,
                              temperature=temperature, json_mode=json_mode,
                              request_timeout=request_timeout)
    if adapter == "qwen":
        return call_qwen(system, user, model=model_id, max_tokens=max_tokens,
                         temperature=temperature, json_mode=json_mode,
                         request_timeout=request_timeout)
    if adapter == "gemini_flash":
        return call_gemini(system, user, model=model_id, max_tokens=max_tokens,
                           temperature=temperature, json_mode=json_mode,
                           request_timeout=request_timeout)

    return {"content": "", "error": f"unknown adapter: {adapter}", "adapter": adapter, "model": model_id}


# ─────────────────────────────────────────────────────────────
# CLI（疎通確認用）
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "ping":
        target = sys.argv[2] if len(sys.argv) > 2 else "deepseek"
        fn = {"deepseek": call_deepseek, "qwen": call_qwen, "gemini": call_gemini}.get(target)
        if fn is None:
            print(f"Unknown target: {target}")
            sys.exit(1)
        result = fn(
            system="あなたは金融アナリストです。",
            user="NVDA の強みを1文で。",
            max_tokens=80,
            temperature=0.2,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Usage: python llm_adapters.py ping [deepseek|qwen|gemini]")

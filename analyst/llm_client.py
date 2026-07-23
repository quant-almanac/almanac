"""
Claude API クライアント
- Prompt Caching（system + shared prefix の 2 段階）
- Tool Use（submit_analysis で JSON を強制出力）
- 529 過負荷リトライ
"""
import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ANTHROPIC_REQUEST_TIMEOUT_SECONDS = _env_float("ANTHROPIC_REQUEST_TIMEOUT_SECONDS", 300.0)
ANTHROPIC_WEB_SEARCH_TIMEOUT_SECONDS = _env_float("ANTHROPIC_WEB_SEARCH_TIMEOUT_SECONDS", 60.0)

# Sonnet 5 / Opus 4.7+ / Fable 5 reject a non-default temperature (400);
# Opus 4.7+ rejects the field outright even at the default. Omit it for these.
_MODELS_REJECTING_SAMPLING_PARAMS = frozenset({
    "claude-sonnet-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-fable-5",
    "claude-mythos-5",
})


def _model_rejects_sampling_params(model: str) -> bool:
    return model in _MODELS_REJECTING_SAMPLING_PARAMS


# Sonnet 5 / Fable 5 / Mythos 5 default to adaptive thinking when `thinking`
# is omitted (Opus 4.7/4.8 still default to no-thinking, unchanged from 4.6).
# Explicitly disable it for non-tool-forced calls to preserve prior behavior
# and avoid an uncontrolled thinking-token cost/latency increase.
_MODELS_DEFAULTING_TO_ADAPTIVE_THINKING = frozenset({
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
})


def _model_defaults_to_adaptive_thinking(model: str) -> bool:
    return model in _MODELS_DEFAULTING_TO_ADAPTIVE_THINKING


def _append_llm_call_log(row: dict) -> None:
    """LLM timeout 調査用の軽量メタログ。プロンプト本文は保存しない。"""
    try:
        from llm_cost_accounting import normalize_usage_row
        row = normalize_usage_row(row)
        path = BASE_DIR / "logs" / "llm_calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _is_max_tokens_error_text(text: str) -> bool:
    return "stop_reason=max_tokens" in text or "max_tokens=" in text


def _compact_retry_prompt(user: str, role: str) -> str:
    return (
        f"{user}\n\n"
        "## 再出力制約（max_tokens / JSON truncation 対策・必須）\n"
        f"role={role} の前回出力が長すぎる、またはJSONが不完全でした。\n"
        "- priority_actions は実行可能な高優先候補を最大12件目安（6件固定で圧縮しない）\n"
        "- list項目は最大8件\n"
        "- reason/action/summary は短く、各160字以内\n"
        "- 冗長な市場概況や同じ根拠の繰り返しは禁止\n"
        "- 必ず有効なJSON/tool_useのみを返す\n"
    )

# ── Tool Use スキーマ ──────────────────────────────────────
_SUBMIT_TOOL = {
    "name": "submit_analysis",
    "description": "分析結果をJSONオブジェクトとして提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "description": "プロンプトで指定されたフィールドをすべて含む分析結果",
                "properties": {
                    # ── 共通フィールド ────────────────────────────────
                    "priority_actions": {
                        "type": "array",
                        "description": "優先アクションリスト（buy/sell/rebalance/trim/dca/stop_loss/take_profit/short/add/margin_buyのみ）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rank": {"type": "integer"},
                                "tier": {"type": "string"},
                                "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
                                "type": {"type": "string"},
                                "ticker": {"type": "string"},
                                "action": {"type": "string"},
                                "reason": {"type": "string"},
                                "amount_hint": {"type": "string"},
                                "confidence_pct": {"type": "integer", "description": "確信度0-100%"},
                                "return_20d_rank": {"type": "string", "enum": ["top", "middle", "bottom"]},
                                # ── v5.1: 執行方式 AI 決定（CHART_CONTEXT があるとき必須） ──
                                "order_type": {"type": "string", "enum": ["market", "limit", "stop_limit"],
                                               "description": "成行/指値/逆指値。CHART_CONTEXTの規則に従う"},
                                "limit_price": {"type": "number",
                                                "description": "指値価格（order_type=limit/stop_limit のとき必須）"},
                                "limit_price_band": {
                                    "type": "object",
                                    "description": "分割指値の帯（任意）。{low, high}",
                                    "properties": {"low": {"type": "number"}, "high": {"type": "number"}},
                                },
                                "expiry_minutes": {"type": "integer",
                                                   "description": "指値の有効分数。標準240、urgency=high→60、low→720+"},
                                "execution_reason": {"type": "string",
                                                     "description": "VWAP/ATR/支持線/spread を引いた指値判断根拠（1〜2文）"},
                                "decision_price": {"type": "number",
                                                   "description": "AI が見ている現値（CHART_CONTEXT.last_close または snapshot.last）。後で shortfall_bps 計算に使う"},
                                # ── No-Transaction Band（Nakagawa流） ──
                                "no_trade_zone": {"type": "boolean",
                                                  "description": "true なら推定 edge < 推定コストで発注見送り推奨"},
                                "skip_reason": {"type": "string",
                                                "description": "no_trade_zone=true 時の根拠（1文）"},
                                # ── Multi-Horizon target hint（TFT 風） ──
                                "target_5d_pct": {"type": "number",
                                                  "description": "5営業日後の期待リターン%（任意、撤退判断材料）"},
                                "target_20d_pct": {"type": "number",
                                                   "description": "20営業日後の期待リターン%（任意）"},
                            },
                        },
                    },
                    "hold_notes": {
                        "type": "array",
                        "description": "保有継続銘柄メモ（ホールド/様子見はここに記載）",
                        "items": {"type": "string"},
                    },
                    "risk_warnings": {
                        "type": "array",
                        "description": "重要なリスク警告",
                        "items": {"type": "string"},
                    },
                    "red_team_verdict": {
                        "type": "array",
                        "description": "Red Team仮説の採否評決（全件必須）",
                        "items": {"type": "object"},
                    },
                    "health":        {"type": "string", "enum": ["good", "caution", "critical"],
                                      "description": "全体健全性（3ティアを統合）"},
                    "health_reason": {"type": "string", "description": "health 判定根拠を1文で"},
                    "summary":       {"type": "string", "description": "ティア概要（3文以内）"},
                    "news_impact":   {"type": "string"},
                    "market_meta_snapshot": {
                        "type": "object",
                        "description": "Opus/ティア分析が参照した市場スナップショット（履歴比較用）",
                        "properties": {
                            "vix":                 {"type": "number"},
                            "vix_level":           {"type": "string"},
                            "us10y_yield":         {"type": "number"},
                            "yield_curve_status":  {"type": "string"},
                        },
                    },

                    # ── 合成（_synthesize）固有 ──────────────────────
                    "overall_stance": {
                        "type": "string",
                        "enum": ["defensive", "neutral", "moderately_aggressive", "aggressive"],
                        "description": "全体スタンス",
                    },
                    "stance_reason":          {"type": "string"},
                    "telegram_message":       {"type": "string"},
                    "weekly_theme":           {"type": "string"},
                    "geopolitical_note":      {"type": "string"},
                    "opportunity_highlights": {"type": "array", "items": {"type": "object"}},
                    "currency_target_recommendation": {
                        "type": "object",
                        "description": (
                            "AI が判断する外貨比率方針。currency_policy が検証し、"
                            "basis=long_tier・confidence十分・未期限切れ・合計100%なら次回 rebalance の"
                            "通貨目標に採用する。自動発注はしない (人間が最終実行)。"
                            "無効/期限切れ/自信不足は static 目標に fail-closed。"
                        ),
                        "properties": {
                            "basis": {
                                "type": "string",
                                "enum": ["long_tier", "whole_portfolio"],
                                "description": "目標の適用母数。rebalance 適用は long_tier のみ。",
                            },
                            "usd_target_pct": {"type": "number"},
                            "jpy_target_pct": {"type": "number", "description": "usd+jpy=100 必須"},
                            "confidence_pct": {"type": "integer", "description": "60未満は不採用"},
                            "horizon_days": {"type": "integer", "description": "想定有効期間 (最大30日にクランプ)"},
                            "valid_until": {"type": "string", "description": "YYYY-MM-DD。失効後は static へ"},
                            "reason": {"type": "string"},
                            "review_triggers": {
                                "type": "array", "items": {"type": "string"},
                                "description": "方針を再評価すべき市況トリガー",
                            },
                            "risk_notes": {"type": "string"},
                        },
                    },
                    "information_lane_verdicts": {
                        "type": "array",
                        "description": "display/context-only 情報レーンの採否評決。各 item は lane, verdict(adopt/reject/ignore), verdict_reason, adopted_as。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "lane": {"type": "string"},
                                "ticker": {"type": "string"},
                                "verdict": {"type": "string", "enum": ["adopt", "reject", "ignore"]},
                                "verdict_reason": {"type": "string"},
                                "adopted_as": {"type": "string"},
                            },
                        },
                    },

                    # ── Medium tier 固有 ─────────────────────────────
                    "margin_long_picks": {
                        "type": "array",
                        "description": "信用買い候補の上位3件（blocked=False時は必須）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ticker":         {"type": "string"},
                                "strategy":       {"type": "string"},
                                "reason":         {"type": "string"},
                                "stop_loss_pct":  {"type": "number"},
                                "urgency":        {"type": "string", "enum": ["high", "medium", "low"]},
                            },
                        },
                    },
                    "new_entries":                 {"type": "array", "items": {"type": "object"}},
                    "profit_taking":               {"type": "array", "items": {"type": "object"}},
                    "watchlist_alert":             {"type": "string"},
                    "signals_quality":             {"type": "string"},
                    "medium_high_return_strategy": {"type": "string"},

                    # ── Short_Positions (swing保有) 固有 ──────────────
                    "loss_management":   {"type": "string"},
                    "recovery_scenario": {"type": "string"},
                    "stop_loss_alerts":  {"type": "array", "items": {"type": "string"}},

                    # ── Short_Selling (空売り・信用全般) 固有 ─────────
                    "margin_health":         {"type": "string", "enum": ["safe", "warning", "danger", "emergency"]},
                    "margin_summary":        {"type": "string"},
                    "short_opportunities":   {
                        "type": "array",
                        "description": "空売り候補の分析結果",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rank":           {"type": "integer"},
                                "ticker":         {"type": "string"},
                                "urgency":        {"type": "string", "enum": ["high", "medium", "low"]},
                                "entry_zone":     {"type": "string"},
                                "target_price":   {"type": "string"},
                                "stop_loss":      {"type": "string"},
                                "risk_reward":    {"type": "string"},
                                "catalyst":       {"type": "string"},
                                "reason":         {"type": "string"},
                                "return_20d_rank":{"type": "string"},
                                "confidence_pct": {"type": "integer"},
                            },
                        },
                    },
                    "margin_actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
                                "action":  {"type": "string"},
                                "reason":  {"type": "string"},
                            },
                        },
                    },
                    "crisis_strategy":       {"type": "string"},
                    "short_not_recommended": {"type": "string"},
                },
                # ティア別アナライザの自由度を確保するため required は最小限。
                # 合成ステップの overall_stance / morning_brief_* はプロンプト側で強く指示する。
                "required": ["priority_actions"],
            }
        },
        "required": ["result"],
    },
}

_SYSTEM_SONNET = """\
あなたはユーザーの専属ポートフォリオアドバイザーです。
提供されたデータ・ニュース・世界情勢を総合的に分析し、必ず以下の形式のJSONのみで回答してください（日本語）。
余計な説明は不要。JSONのみ出力すること。"""

_GEO_KEYWORDS = {
    "war", "peace", "iran", "trump", "tariff", "china", "russia", "ukraine",
    "military", "sanction", "oil", "crude", "diplomat", "nato", "taiwan",
    "korea", "middle east", "geopolit", "hostil", "conflict", "attack",
    "troops", "missile", "trade war", "yen", "dollar", "currency", "boj",
    "fed", "rate", "inflation", "recession", "fiscal", "stimulus",
}


def call_claude(system: str, user: str, model: str = "claude-sonnet-4-6",
                max_tokens: int = 8192, cached_prefix: str = "",
                use_tool: bool = False,
                temperature: float | None = None,
                role: str | None = None,
                request_timeout: float | None = None) -> "str | dict":
    """
    Claude API 呼び出し。Prompt Caching を 2 段階で適用:
      1. system プロンプト: cache_control ephemeral
      2. cached_prefix:    各ティア共通の market_meta/regime/scenario テキスト

    use_tool=True の場合: submit_analysis ツールを force 指定して構造化 JSON を返す。
    use_tool=False の場合: テキストを返す（後方互換）。

    P3-16: temperature 未指定時は ALMANAC_DETERMINISTIC=1 で 0.0 固定、
    それ以外は Anthropic デフォルト（1.0）。

    role を指定した場合 (例: "final_synthesis", "tier_analysis_short")、
    model_router.get_model(role) で解決したモデル ID を使用し、引数の `model`
    は無視される。非 Anthropic モデル（deepseek/qwen/gemini_flash）が解決された
    場合は呼び出し側で llm_adapters.call_by_role を使うべきで、ここでは
    ValueError を送出する（assertion 的扱い）。
    """
    import anthropic
    # role 指定時は model_router 経由でモデル ID を上書き
    if role is not None:
        try:
            from model_router import get_model, resolve_adapter
        except ImportError:
            from ..model_router import get_model, resolve_adapter  # type: ignore
        adapter = resolve_adapter(role)
        if adapter != "anthropic":
            raise ValueError(
                f"call_claude: role '{role}' resolves to non-anthropic adapter "
                f"'{adapter}'. Use llm_adapters.call_by_role(role, ...) instead."
            )
        model = get_model(role)

    try:
        from utils import get_llm_temperature
        effective_temp = temperature if temperature is not None else get_llm_temperature(default=1.0)
    except ImportError:
        effective_temp = temperature if temperature is not None else 1.0

    effective_timeout = request_timeout or ANTHROPIC_REQUEST_TIMEOUT_SECONDS
    client = anthropic.Anthropic(timeout=effective_timeout)
    system_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if cached_prefix:
        user_content = [
            {"type": "text", "text": cached_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": user},
        ]
    else:
        user_content = user

    kwargs: dict = dict(
        model=model,
        max_tokens=max_tokens,
        system=system_param,
        messages=[{"role": "user", "content": user_content}],
    )
    if not _model_rejects_sampling_params(model):
        kwargs["temperature"] = effective_temp
    if use_tool:
        kwargs["tools"] = [_SUBMIT_TOOL]
        kwargs["tool_choice"] = {"type": "tool", "name": "submit_analysis"}
    elif _model_defaults_to_adaptive_thinking(model):
        kwargs["thinking"] = {"type": "disabled"}

    prompt_chars = len(user or "") if isinstance(user, str) else len(json.dumps(user, ensure_ascii=False))
    prefix_chars = len(cached_prefix or "")
    started = time.monotonic()
    for attempt in range(4):
        try:
            msg = client.messages.create(**kwargs)
            _stop_reason = getattr(msg, "stop_reason", None)
            _content_types = [b.type for b in msg.content]
            _usage = getattr(msg, "usage", None)
            _append_llm_call_log({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "role": role,
                "model": model,
                "use_tool": use_tool,
                "max_tokens": max_tokens,
                "timeout_sec": effective_timeout,
                "attempt": attempt + 1,
                "elapsed_sec": round(time.monotonic() - started, 2),
                "prompt_chars": prompt_chars,
                "cached_prefix_chars": prefix_chars,
                "status": "ok",
                "stop_reason": _stop_reason,
                "content_types": _content_types,
                "input_tokens": getattr(_usage, "input_tokens", None),
                "output_tokens": getattr(_usage, "output_tokens", None),
            })
            if use_tool:
                tool_result = None
                _raw_input = None
                for block in msg.content:
                    if block.type == "tool_use":
                        _raw_input = block.input
                        tool_result = block.input.get("result", block.input)
                        break
                if isinstance(tool_result, dict) and tool_result:
                    return tool_result
                # max_tokens truncation: JSON が途中で切れると block.input = {} になる。
                # temperature=0 の決定論的呼び出しでは何度リトライしても同結果なので即raise。
                _is_maxtok = (_stop_reason == "max_tokens")
                _append_llm_call_log({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "role": role,
                    "model": model,
                    "use_tool": use_tool,
                    "max_tokens": max_tokens,
                    "timeout_sec": effective_timeout,
                    "attempt": attempt + 1,
                    "elapsed_sec": round(time.monotonic() - started, 2),
                    "prompt_chars": prompt_chars,
                    "cached_prefix_chars": prefix_chars,
                    "status": "empty_tool_result",
                    "stop_reason": _stop_reason,
                    "content_types": _content_types,
                    "raw_input_keys": list(_raw_input.keys()) if isinstance(_raw_input, dict) else repr(type(_raw_input)),
                    "raw_input_len": len(str(_raw_input)) if _raw_input is not None else 0,
                })
                if _is_maxtok:
                    raise RuntimeError(
                        f"Claude tool_use: stop_reason=max_tokens — max_tokens={max_tokens} が不足。"
                        "増やすか出力スキーマを縮小してください。"
                    )
                if attempt < 2:
                    time.sleep(5)
                    continue
                raise RuntimeError("Claude tool_use returned no usable result")
            for _block in msg.content:
                if _block.type == "text":
                    return _block.text
            return ""
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 3:
                wait = 5 * (2 ** attempt)
                print(f"⚠️ Anthropic 過負荷 (529)、{wait}秒後にリトライ ({attempt+1}/3)…")
                time.sleep(wait)
            else:
                _append_llm_call_log({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "role": role,
                    "model": model,
                    "use_tool": use_tool,
                    "max_tokens": max_tokens,
                    "timeout_sec": effective_timeout,
                    "attempt": attempt + 1,
                    "elapsed_sec": round(time.monotonic() - started, 2),
                    "prompt_chars": prompt_chars,
                    "cached_prefix_chars": prefix_chars,
                    "status": "error",
                    "error_type": type(e).__name__,
                    "error": str(e)[:500],
                })
                raise
        except Exception as e:
            _append_llm_call_log({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "role": role,
                "model": model,
                "use_tool": use_tool,
                "max_tokens": max_tokens,
                "timeout_sec": effective_timeout,
                "attempt": attempt + 1,
                "elapsed_sec": round(time.monotonic() - started, 2),
                "prompt_chars": prompt_chars,
                "cached_prefix_chars": prefix_chars,
                "status": "error",
                "error_type": type(e).__name__,
                "error": str(e)[:500],
            })
            raise


class _TierTransportError(Exception):
    """call_by_role がエラー dict を返したことを call_book_aware_llm 経由でも
    retry ロジックに伝えるための内部例外 (Codex P2 #13)。"""


def call_tier_analysis(system: str, user: str, *,
                       role: str,
                       max_tokens: int = 4096,
                       cached_prefix: str = "",
                       temperature: float | None = None,
                       request_timeout: float | None = None) -> dict:
    """
    ティア分析専用ディスパッチ。
    role を resolve_adapter で評価し:
      - anthropic → 既存 call_claude(use_tool=True) で構造化 JSON 取得
      - deepseek  → llm_adapters.call_by_role(json_mode=True) で JSON 取得 → parse
    返り値は **必ず dict**。失敗時は {"error": "...", "_source": "..."} を返す。
    呼出側は dict["_source"] でモデル出典を確認可能（例: "anthropic:claude-opus-4-7", "deepseek:deepseek-v4-pro"）。
    """
    try:
        from model_router import get_model, resolve_adapter
    except ImportError:
        from ..model_router import get_model, resolve_adapter  # type: ignore

    adapter = resolve_adapter(role)
    model_id = get_model(role)
    source_tag = f"{adapter}:{model_id}"

    # Tier analysis is inherently book-aware (it reasons over the actual
    # portfolio) — gate it by ALMANAC_PRIVACY_MODE before it reaches either
    # provider branch below. Fail-closed if the safety module itself can't
    # be imported, matching the existing DeepSeek book-aware path's policy
    # of refusing an un-audited call rather than silently proceeding.
    try:
        from almanac.llm_safety import assert_book_aware_allowed, BookAwareDisabled, get_privacy_mode
    except ImportError as _ls_err:
        return {
            "error": f"almanac.llm_safety unavailable; refusing un-audited book-aware call: {_ls_err}",
            "_source": f"blocked:{adapter}",
        }
    try:
        assert_book_aware_allowed(provider=adapter)
    except BookAwareDisabled as e:
        return {
            "error": str(e),
            "_source": f"blocked:{adapter}",
            "_privacy_mode": get_privacy_mode(),
        }

    if adapter == "anthropic":
        try:
            result = call_claude(
                system=system, user=user,
                max_tokens=max_tokens,
                cached_prefix=cached_prefix,
                use_tool=True,
                temperature=temperature,
                role=role,
                request_timeout=request_timeout,
            )
            if not isinstance(result, dict):
                return {"error": "non-dict result from anthropic", "_source": source_tag}
            result.setdefault("_source", source_tag)
            return result
        except Exception as e:  # noqa: BLE001
            if _is_max_tokens_error_text(str(e)):
                retry_tokens = max(max_tokens * 2, 12000)
                try:
                    result = call_claude(
                        system=system,
                        user=_compact_retry_prompt(user, role),
                        max_tokens=retry_tokens,
                        cached_prefix=cached_prefix,
                        use_tool=True,
                        temperature=temperature,
                        role=role,
                        request_timeout=max(request_timeout or 0, ANTHROPIC_REQUEST_TIMEOUT_SECONDS),
                    )
                    if isinstance(result, dict):
                        result.setdefault("_source", source_tag)
                        result.setdefault("_retry", "max_tokens_compact")
                        return result
                except Exception as retry_e:  # noqa: BLE001
                    return {"error": f"{e}; retry_failed={retry_e}", "_source": source_tag}
            return {"error": str(e), "_source": source_tag}

    if adapter == "deepseek":
        try:
            from llm_adapters import call_by_role
        except ImportError:
            return {"error": "llm_adapters not importable", "_source": source_tag}
        # Codex re-review #13: book-aware tier 呼び出しは almanac.llm_safety 経由で監査ログに集約。
        # book 送信は方針として許容済みだが、監査経路は fail-closed —— safety を import できなければ
        # 無監査で DeepSeek を呼ばず error を返す (silent fallback は廃止)。
        try:
            from almanac.llm_safety import Payload, call_book_aware_llm, BOOK_AWARE_KIND
        except ImportError as _ls_err:
            return {"error": f"almanac.llm_safety unavailable; refusing un-audited book-aware DeepSeek call: {_ls_err}",
                    "_source": source_tag}

        # cached_prefix は単純連結（DeepSeek は prompt caching 非対応のため保持のみ）
        if cached_prefix:
            full_user = (
                f"### 共通コンテキスト ###\n{cached_prefix}\n\n"
                f"### タスク ###\n{user}\n\n"
                "**必ず純粋な JSON オブジェクトのみを出力（解説や markdown 禁止）。**"
            )
        else:
            full_user = (
                f"{user}\n\n"
                "**必ず純粋な JSON オブジェクトのみを出力（解説や markdown 禁止）。**"
            )

        _temp = temperature if temperature is not None else 0.3

        def _deepseek_transport(*, base_url, api_key, model_id, system, user,
                                max_tokens, temperature):
            # 実アダプタ呼び出し。base_url/api_key は call_by_role が role から解決するため未使用。
            r = call_by_role(
                role=role, system=system, user=user,
                max_tokens=max_tokens, temperature=temperature,
                json_mode=True, request_timeout=request_timeout,
            )
            if r.get("error"):
                raise _TierTransportError(str(r.get("error")))
            usage = r.get("usage") or {}
            return (r.get("content") or ""), {
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
            }

        def _run(u: str, mt: int) -> str:
            payload = Payload(kind=BOOK_AWARE_KIND, system=system, user=u,
                              meta={"role": role})
            return call_book_aware_llm(
                payload, model_id=model_id, transport=_deepseek_transport,
                role=role, max_tokens=mt, temperature=_temp,
            ).content

        try:
            content = _run(full_user, max_tokens)
        except _TierTransportError as te:
            err = str(te)
            if _is_max_tokens_error_text(err):
                retry_tokens = max(max_tokens * 2, 12000)
                try:
                    content = _run(_compact_retry_prompt(full_user, role), retry_tokens)
                except _TierTransportError as te2:
                    return {"error": f"{err}; retry_failed={te2}", "_source": source_tag}
            else:
                return {"error": err, "_source": source_tag}

        content = (content or "").strip()
        try:
            parsed = json.loads(content)
        except Exception:
            # truncate fallback (utils._extract_json があれば利用)
            try:
                from utils import _extract_json  # type: ignore
                parsed = _extract_json(content) or {}
            except Exception:
                parsed = {}
        if not isinstance(parsed, dict):
            return {"error": "non-dict JSON from deepseek", "_source": source_tag, "raw_excerpt": content[:200]}
        parsed.setdefault("_source", source_tag)
        return parsed

    return {"error": f"unsupported adapter: {adapter}", "_source": source_tag}


def fetch_web_search_news() -> str:
    """
    Claude Haiku の組み込み Web Search ツールで最新市場ニュースを取得。
    失敗時は空文字を返してフォールバック（RSS/yfinance）を使用。
    """
    import anthropic
    from datetime import datetime
    client = anthropic.Anthropic(timeout=ANTHROPIC_WEB_SEARCH_TIMEOUT_SECONDS)

    today = datetime.now().strftime("%Y年%m月%d日")
    model = "claude-haiku-4-5-20251001"
    search_prompt = f"""本日{today}の以下の最新情報を日本語の箇条書きで教えてください（各項目3点以内）:
1. 米国株式市場の主要ニュース（S&P500/NASDAQ/Dow動向・VIX）
2. Fed（FRB）・金融政策に関する最新発言・FOMC動向
3. 地政学リスク（米中関税・ロシア/ウクライナ・中東・NATO）の重要動向
4. 半導体・AI業界ニュース（NVDA/AVGO/AMD等）
5. 為替（円ドル）・日銀動向"""

    started = time.monotonic()
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=1500,
            tools=[{"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]}],
            messages=[{"role": "user", "content": search_prompt}],
        )
        usage = getattr(response, "usage", None)
        server_tool_use = getattr(usage, "server_tool_use", None)
        server_tool_use_row = {}
        if server_tool_use is not None:
            for key in ("web_search_requests",):
                value = getattr(server_tool_use, key, None)
                if value is not None:
                    server_tool_use_row[key] = value
        _append_llm_call_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "role": "web_search_news",
            "model": model,
            "use_tool": True,
            "max_tokens": 1500,
            "timeout_sec": ANTHROPIC_WEB_SEARCH_TIMEOUT_SECONDS,
            "elapsed_sec": round(time.monotonic() - started, 2),
            "prompt_chars": len(search_prompt),
            "status": "ok",
            "stop_reason": getattr(response, "stop_reason", None),
            "content_types": [getattr(b, "type", None) for b in getattr(response, "content", [])],
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            **({"server_tool_use": server_tool_use_row} if server_tool_use_row else {}),
        })
        texts = [b.text for b in response.content if hasattr(b, "text") and b.text]
        result = "\n".join(texts).strip()
        if result:
            print(f"  ✅ Web search 完了 ({len(result)}文字)")
        return result
    except Exception as e:
        _append_llm_call_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "role": "web_search_news",
            "model": model,
            "use_tool": True,
            "max_tokens": 1500,
            "timeout_sec": ANTHROPIC_WEB_SEARCH_TIMEOUT_SECONDS,
            "elapsed_sec": round(time.monotonic() - started, 2),
            "prompt_chars": len(search_prompt),
            "status": "error",
            "error_type": type(e).__name__,
            "error": str(e)[:500],
        })
        print(f"  ⚠️ Web search 失敗（RSS/yfinance にフォールバック）: {e}")
        return ""

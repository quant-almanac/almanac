import yfinance as yf
import json
import os
import time
import pandas as pd
from datetime import datetime, timedelta
from almanac.runtime_config import get_env
from regime_params import get_params, get_regime
from utils import init_yfinance_timeout

init_yfinance_timeout()

SONNET_MODEL_ID = "claude-sonnet-5"
HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"


def _append_llm_call_log(row: dict) -> None:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        _append(row)
    except Exception:
        pass


def _log_anthropic_usage(
    *,
    role: str,
    model: str,
    max_tokens: int,
    started: float,
    prompt_chars: int,
    response=None,
    status: str = "ok",
    use_tool: bool = True,
    error: Exception | None = None,
    **extra,
) -> None:
    usage = getattr(response, "usage", None)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": model,
        "use_tool": use_tool,
        "max_tokens": max_tokens,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": prompt_chars,
        "status": status,
        **extra,
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


def _log_adapter_usage(
    *,
    role: str,
    result: dict,
    started: float,
    prompt_chars: int,
    max_tokens: int,
    status: str,
    **extra,
) -> None:
    usage = result.get("usage") or {}
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": result.get("model") or extra.get("model"),
        "adapter": result.get("adapter"),
        "use_tool": False,
        "max_tokens": max_tokens,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": prompt_chars,
        "status": status,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        **extra,
    }
    if result.get("error"):
        row.update({
            "error": str(result.get("error"))[:500],
            "cost_usd": 0.0 if not usage else row.get("cost_usd"),
        })
    _append_llm_call_log(row)

# ── AI シグナル生成（Sonnet×3並列ディベート → Opus統合 / Haiku フォールバック） ──────────

# Tool Use スキーマ: Sonnet の視点出力
_DEBATE_TOOL = {
    "name": "submit_views",
    "description": "各候補銘柄への分析視点をリストで提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "views": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "view": {"type": "string", "enum": ["BULLISH", "NEUTRAL", "BEARISH"]},
                        "reason": {"type": "string", "description": "50字以内"},
                        "conviction_score": {"type": "integer", "minimum": 0, "maximum": 100, "description": "確信度（0-100）"},
                        "risk_factors": {"type": "array", "items": {"type": "string"}, "description": "主要リスク要因リスト（最大3件）"}
                    },
                    "required": ["ticker", "view", "reason"]
                }
            }
        },
        "required": ["views"]
    }
}

# Tool Use スキーマ: Opus の最終判定
_FINAL_SIGNAL_TOOL = {
    "name": "submit_final_signals",
    "description": "全候補の最終 BUY/WATCH/SKIP 判定をリストで提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "signal": {"type": "string", "enum": ["BUY", "WATCH", "SKIP"]},
                        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                        "reason": {"type": "string", "description": "60字以内"}
                    },
                    "required": ["ticker", "signal", "confidence", "reason"]
                }
            }
        },
        "required": ["signals"]
    }
}


def _build_candidates_text(candidates: list, market_meta: dict, macro_context: dict | None) -> str:
    """全候補をまとめたテキストを生成（Sonnet/Opus 共通入力）"""
    lines = []
    ff  = (macro_context or {}).get('fed_rate', (macro_context or {}).get('fed_funds_rate', '?'))
    t10 = (macro_context or {}).get('yield_10y', '?')
    sp  = (macro_context or {}).get('yield_spread', '')
    lines.append(f"【地合い】S&P500: 50日線の{market_meta.get('sp500','不明')} / 日経: {market_meta.get('nikkei','不明')}")
    lines.append(f"【マクロ】FF金利{ff}% / 10年債{t10}%" + (f" / スプレッド{sp}%" if sp else ""))
    lines.append("")
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"{i}. {c['ticker']} [{c['strategy']}]"
            f" RSI:{c['rsi']} mom5d:{c['mom_5d']}% mom1m:{c['mom_1m']}%"
            f" MA50乖離:{c['ma50_dev']}% 出来高比:{c['volume_ratio']}倍"
            f" ATR:{c['atr_pct']}% 52週高値更新:{'YES' if c['new_52w_high'] else 'NO'}"
            f" | {c['reason']}"
        )
    return "\n".join(lines)


def _call_debate_signals(candidates: list, market_meta: dict,
                         macro_context: dict | None = None) -> list:
    """
    Sonnet×3並列（強気・弱気・マクロ）→ Opus統合で全候補のBUY/WATCH/SKIP判定。
    候補10件をまとめて処理（4 API呼び出しのみ）。
    Returns: candidates（ai_signal/ai_confidence/ai_reason/ai_debate を追記済み）
    """
    from concurrent.futures import ThreadPoolExecutor
    import anthropic

    client = anthropic.Anthropic()
    ctx = _build_candidates_text(candidates, market_meta, macro_context)

    def _sonnet_view(role_system: str, role_instruction: str, label: str = "") -> list:
        """1視点のSonnet分析。Tool Useで [{ticker, view, reason}] を返す。
        空 views を返した場合は最大 2 回リトライ（プロンプトを強化）。
        """
        # 全候補を必ず評価するよう明示指示
        enhanced_instruction = (
            f"{role_instruction}\n\n"
            "⚠️ 重要: 上記の候補銘柄【全件】について、必ず view と reason を返してください。\n"
            "・評価の根拠が薄い銘柄でも NEUTRAL + 「情報不足」等の理由で必ず views に含めること。\n"
            "・views: [] の空配列返答は禁止（アナリストとして職務放棄に相当）。"
        )
        last_views: list = []
        for attempt in range(2):
            try:
                started = time.monotonic()
                resp = client.messages.create(
                    model=SONNET_MODEL_ID,
                    max_tokens=800,
                    system=[{"type": "text", "text": role_system, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": ctx, "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": enhanced_instruction},
                    ]}],
                    tools=[_DEBATE_TOOL],
                    tool_choice={"type": "tool", "name": "submit_views"},
                )
                _log_anthropic_usage(
                    role="screener_legacy_debate_view",
                    model=SONNET_MODEL_ID,
                    max_tokens=800,
                    started=started,
                    prompt_chars=len(ctx) + len(enhanced_instruction),
                    response=resp,
                    perspective=label or "debate",
                    attempt=attempt + 1,
                    candidate_count=len(candidates),
                )
                for block in resp.content:
                    if block.type == "tool_use":
                        views = block.input.get("views", []) or []
                        if views:
                            return views
                        last_views = views
                        print(f"  ⚠️ [{label or 'debate'}] views が空 (attempt {attempt+1}/2) — retry")
            except Exception as e:
                _log_anthropic_usage(
                    role="screener_legacy_debate_view",
                    model=SONNET_MODEL_ID,
                    max_tokens=800,
                    started=started if "started" in locals() else time.monotonic(),
                    prompt_chars=len(ctx) + len(enhanced_instruction),
                    status="error",
                    perspective=label or "debate",
                    attempt=attempt + 1,
                    candidate_count=len(candidates),
                    error=e,
                )
                print(f"  [Sonnet debate] エラー ({label or '?'}): {e}")
        return last_views

    # Sonnet A: 強気アナリスト
    sys_bull = "あなたは短期トレードの強気アナリストです。各銘柄のエントリーを支持する根拠に注目して分析してください。"
    inst_bull = "上記の候補銘柄それぞれについて、テクニカル的に強気な根拠（モメンタム・ブレイクアウト・出来高確認等）を評価し、BULLISH/NEUTRAL/BEARISHで判定してください。"

    # Sonnet B: リスクアナリスト
    sys_bear = "あなたは短期トレードのリスク管理アナリストです。各銘柄のリスク要因と失敗シナリオに注目して分析してください。"
    inst_bear = "上記の候補銘柄それぞれについて、過熱感・騙しのシグナル・不利なタイミング等のリスク要因を評価し、BULLISH/NEUTRAL/BEARISHで判定してください。"

    # Sonnet C: マクロ・セクターアナリスト
    sys_macro = "あなたはマクロ経済とセクターローテーションの専門アナリストです。市場環境と各銘柄の戦略適合性に注目して分析してください。"
    inst_macro = "上記の候補銘柄それぞれについて、現在の市場レジーム・金利環境・セクタートレンドとの適合性を評価し、BULLISH/NEUTRAL/BEARISHで判定してください。"

    print("  🤖 Sonnet×3 ディベート分析中（強気・弱気・マクロ）...")
    with ThreadPoolExecutor(max_workers=3) as ex:
        fa = ex.submit(_sonnet_view, sys_bull,  inst_bull,  "bull")
        fb = ex.submit(_sonnet_view, sys_bear,  inst_bear,  "bear")
        fc = ex.submit(_sonnet_view, sys_macro, inst_macro, "macro")
        views_bull  = fa.result()
        views_bear  = fb.result()
        views_macro = fc.result()
    print(f"  📊 ディベート結果: bull={len(views_bull)} bear={len(views_bear)} macro={len(views_macro)}")

    # Opus 最終統合
    def _fmt_views(views: list, label: str) -> str:
        if not views:
            return f"{label}: データなし"
        return f"{label}:\n" + "\n".join(
            f"  {v.get('ticker','?')}: {v.get('view','?')} — {v.get('reason','')}"
            for v in views
        )

    opus_prompt = (
        f"{ctx}\n\n"
        f"{_fmt_views(views_bull,  '【強気アナリスト】')}\n\n"
        f"{_fmt_views(views_bear,  '【リスクアナリスト】')}\n\n"
        f"{_fmt_views(views_macro, '【マクロアナリスト】')}\n\n"
        "3人のアナリストの意見を総合し、各銘柄の最終シグナル（BUY/WATCH/SKIP）と確信度（0-100）を判定してください。\n"
        "BUY: 3視点が概ね揃いエントリー推奨 / WATCH: 意見が割れ様子見 / SKIP: リスクが上回り見送り"
    )

    print("  🎯 Sonnet 最終統合中（Opus → Sonnet にコスト最適化）...")
    try:
        # model_router 経由で "screener_deepdive" → Sonnet に降格。
        # ALMANAC_BUDGET_MODE=premium で Opus に戻せる。
        from model_router import get_model as _get_model
        _screener_model = _get_model("screener_deepdive")
        started = time.monotonic()
        resp = client.messages.create(
            model=_screener_model,
            max_tokens=1200,
            system=[{"type": "text", "text": "あなたはチーフ投資アナリストです。複数のアナリストの意見を統合し、最終判断を下します。", "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": opus_prompt}],
            tools=[_FINAL_SIGNAL_TOOL],
            tool_choice={"type": "tool", "name": "submit_final_signals"},
        )
        _log_anthropic_usage(
            role="screener_legacy_final_signal",
            model=_screener_model,
            max_tokens=1200,
            started=started,
            prompt_chars=len(opus_prompt),
            response=resp,
            candidate_count=len(candidates),
        )
        final_signals = []
        for block in resp.content:
            if block.type == "tool_use":
                final_signals = block.input.get("signals", [])
    except Exception as e:
        _log_anthropic_usage(
            role="screener_legacy_final_signal",
            model=_screener_model if "_screener_model" in locals() else SONNET_MODEL_ID,
            max_tokens=1200,
            started=started if "started" in locals() else time.monotonic(),
            prompt_chars=len(opus_prompt),
            status="error",
            candidate_count=len(candidates),
            error=e,
        )
        print(f"  [Opus] エラー: {e}")
        final_signals = []

    # 結果をcandidatesに反映
    sig_map = {s["ticker"]: s for s in final_signals}
    debate_map = {
        "bull": {v["ticker"]: v for v in views_bull},
        "bear": {v["ticker"]: v for v in views_bear},
        "macro": {v["ticker"]: v for v in views_macro},
    }

    for c in candidates:
        t = c["ticker"]
        sig = sig_map.get(t, {})
        c["ai_signal"]     = sig.get("signal", "WATCH")
        c["ai_confidence"] = int(sig.get("confidence", 50))
        c["ai_reason"]     = str(sig.get("reason", ""))[:80]
        c["ai_comment"]    = f"[{c['ai_signal']} {c['ai_confidence']}%] {c['ai_reason']}"
        # ディベート詳細（フロントエンド表示用）
        c["ai_debate"] = {
            "bull":  debate_map["bull"].get(t, {}).get("reason", ""),
            "bear":  debate_map["bear"].get(t, {}).get("reason", ""),
            "macro": debate_map["macro"].get(t, {}).get("reason", ""),
            "bull_view":  debate_map["bull"].get(t, {}).get("view", ""),
            "bear_view":  debate_map["bear"].get(t, {}).get("view", ""),
            "macro_view": debate_map["macro"].get(t, {}).get("view", ""),
        }

    return candidates


# ─────────────────────────────────────────────────────────────
# S6 ハーネス再設計: DeepSeek V4 単一コール + Sonnet 第二意見
# ─────────────────────────────────────────────────────────────

_DEEPSEEK_MULTI_SYSTEM = (
    "あなたは ALMANAC の統合トレードアナリストです。"
    "候補銘柄を Bull / Bear / Macro の 3 視点で順番に評価し、最終 BUY/WATCH/SKIP 判定を返します。"
    "必ず純粋な JSON のみを出力（解説・markdown 禁止）。"
)

_DEEPSEEK_USER_TMPL = (
    "{candidates_text}\n\n"
    "各銘柄について以下のステップで分析してください:\n"
    "  Step 1 (Bull):  モメンタム/ブレイクアウト/出来高根拠（50字以内）\n"
    "  Step 2 (Bear):  過熱感/騙し/不利タイミング等のリスク（50字以内）\n"
    "  Step 3 (Macro): 現在のレジーム/金利/セクタートレンドとの適合性（50字以内）\n"
    "  Step 4 (Final): BUY/WATCH/SKIP, confidence(0-100), reason(80字以内)\n\n"
    "出力 JSON 形式:\n"
    '{{"signals": [\n'
    '  {{"ticker": "AAPL", "bull_view": "...", "bear_view": "...",'
    ' "macro_view": "...", "signal": "BUY", "confidence": 75, "reason": "..."}}\n'
    "]}}"
)


def _call_deepseek_multiperspective(candidates: list, market_meta: dict,
                                    macro_context: dict | None) -> list | None:
    """DeepSeek V4-flash 単一コール。視点を内部で展開し、各候補の最終シグナルを返す。
    失敗時は None を返してフォールバックを促す。
    """
    try:
        from llm_adapters import call_deepseek
    except Exception as e:
        print(f"  [deepseek] llm_adapters import 失敗: {e}")
        return None
    text  = _build_candidates_text(candidates, market_meta, macro_context)
    user  = _DEEPSEEK_USER_TMPL.format(candidates_text=text)
    started = time.monotonic()
    res = call_deepseek(_DEEPSEEK_MULTI_SYSTEM, user,
                        max_tokens=3000, temperature=0.3, json_mode=True)
    _log_adapter_usage(
        role="screener_deepseek_multiperspective",
        result=res,
        started=started,
        prompt_chars=len(_DEEPSEEK_MULTI_SYSTEM) + len(user),
        max_tokens=3000,
        status="error" if res.get("error") else "ok",
        candidate_count=len(candidates),
    )
    if res.get("error"):
        print(f"  [deepseek] error: {res['error']}")
        return None
    raw = res.get("content", "")
    try:
        data = json.loads(raw)
    except Exception:
        # 部分パース fallback: 最初の { ... } ブロックを探す
        import re
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            print(f"  [deepseek] JSON parse fail")
            return None
        try:
            data = json.loads(m.group())
        except Exception as e:
            print(f"  [deepseek] JSON parse fail2: {e}")
            return None
    signals = data.get("signals") or []
    if not isinstance(signals, list):
        return None
    # 正規化
    out = []
    for s in signals:
        if not isinstance(s, dict) or not s.get("ticker"):
            continue
        sig = str(s.get("signal", "WATCH")).upper()
        if sig not in ("BUY", "WATCH", "SKIP"):
            sig = "WATCH"
        out.append({
            "ticker":     s["ticker"],
            "signal":     sig,
            "confidence": max(0, min(100, int(s.get("confidence", 50) or 50))),
            "reason":     str(s.get("reason", ""))[:80],
            "bull_view":  str(s.get("bull_view", ""))[:60],
            "bear_view":  str(s.get("bear_view", ""))[:60],
            "macro_view": str(s.get("macro_view", ""))[:60],
        })
    return out


def _call_sonnet_second_opinion(top_buys: list, market_meta: dict,
                                macro_context: dict | None) -> dict:
    """BUY 上位 3 件を Sonnet で再判定。{ticker: {signal, confidence, reason}} を返す。"""
    if not top_buys:
        return {}
    try:
        import anthropic as _anthropic
    except Exception:
        return {}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    text = _build_candidates_text(top_buys, market_meta, macro_context)
    system = (
        "あなたは ALMANAC の最終判定アナリスト。"
        "DeepSeek の予選を通過した BUY 候補を受け取り、過熱感やリスク再評価を行い、"
        "最終 BUY/WATCH/SKIP を 1 件ずつ Tool で提出してください。"
        "DeepSeek の判定が甘いと感じれば WATCH/SKIP に降格して構いません。"
    )
    try:
        from model_router import get_model
        sonnet_id = get_model("screener_second_opinion")
    except Exception:
        sonnet_id = "claude-sonnet-4-6"
    try:
        c = _anthropic.Anthropic()
        started = time.monotonic()
        resp = c.messages.create(
            model=sonnet_id,
            max_tokens=1500,
            system=system,
            tools=[_FINAL_SIGNAL_TOOL],
            tool_choice={"type": "tool", "name": "submit_final_signals"},
            messages=[{"role": "user", "content": text}],
        )
    except Exception as e:
        _log_anthropic_usage(
            role="screener_sonnet_second_opinion",
            model=sonnet_id,
            max_tokens=1500,
            started=started if "started" in locals() else time.monotonic(),
            prompt_chars=len(text),
            status="error",
            candidate_count=len(top_buys),
            error=e,
        )
        print(f"  [sonnet 2nd] エラー: {e}")
        return {}
    _log_anthropic_usage(
        role="screener_sonnet_second_opinion",
        model=sonnet_id,
        max_tokens=1500,
        started=started,
        prompt_chars=len(text),
        response=resp,
        candidate_count=len(top_buys),
    )
    out: dict = {}
    for blk in resp.content:
        if getattr(blk, "type", None) == "tool_use" and getattr(blk, "name", "") == "submit_final_signals":
            for s in (blk.input.get("signals") or []):
                tk = s.get("ticker")
                if not tk:
                    continue
                sig = str(s.get("signal", "WATCH")).upper()
                if sig not in ("BUY", "WATCH", "SKIP"):
                    sig = "WATCH"
                out[tk] = {
                    "signal":     sig,
                    "confidence": max(0, min(100, int(s.get("confidence", 50) or 50))),
                    "reason":     str(s.get("reason", ""))[:80],
                }
    return out


def _haiku_fallback_loop(candidates: list, market_meta: dict, macro_context: dict | None) -> list:
    """既存 Haiku 単体フォールバック（DeepSeek 失敗 + Sonnet 障害時）"""
    market_str = f"S&P500は50日線の{market_meta.get('sp500','不明')}、日経は{market_meta.get('nikkei','不明')}"
    macro_str = ""
    if macro_context:
        ff = macro_context.get('fed_rate', macro_context.get('fed_funds_rate', '?'))
        t10 = macro_context.get('yield_10y', '?')
        spread = macro_context.get('yield_spread', '')
        macro_str = f"\nマクロ: FF金利{ff}% / 10年債{t10}%"
        if spread:
            macro_str += f" / イールドスプレッド{spread}%"
    for c in candidates:
        try:
            prompt = (
                f"地合い: {market_str}{macro_str}\n"
                f"銘柄: {c['ticker']} / 戦略: {c['strategy']}\n"
                f"テクニカル: {c['reason']}\n"
                f"5日騰落: {c['mom_5d']}% / 1M: {c['mom_1m']}% / MA50乖離: {c['ma50_dev']}%\n"
                "BUY/WATCH/SKIPをJSON形式で返せ。"
            )
            result = _call_fallback_signal(prompt)
            c['ai_signal']     = result.get('signal', 'WATCH')
            c['ai_confidence'] = int(result.get('confidence', 50))
            c['ai_reason']     = result.get('reason', '')
            c['ai_comment']    = f"[{c['ai_signal']} {c['ai_confidence']}%] {c['ai_reason']}"
            c['ai_debate']     = {}
            c['ai_source']     = 'haiku_fallback'
        except Exception as ex:
            print(f"  [Haiku] {c['ticker']} エラー: {ex}")
            c['ai_signal'] = 'WATCH'; c['ai_confidence'] = 50
            c['ai_reason'] = ''; c['ai_comment'] = ''; c['ai_debate'] = {}
            c['ai_source'] = 'unavailable'
    return candidates


def _call_fallback_signal(prompt: str) -> dict:
    """Haiku フォールバック（Anthropic クレジット不足 / API障害時）"""
    import re
    system = (
        "あなたは短期トレードの定量アナリスト。"
        "候補銘柄を分析し、必ず以下のJSON形式のみで回答せよ（余計なテキスト不要）:\n"
        '{"signal": "BUY", "confidence": 75, "reason": "根拠50字以内"}\n'
        "BUY: 複数条件が揃いエントリー推奨 / WATCH: 条件不足で様子見 / SKIP: リスク過大で見送り"
    )
    try:
        import anthropic as _anthropic
        c = _anthropic.Anthropic()
        started = time.monotonic()
        resp = c.messages.create(
            model=HAIKU_MODEL_ID,
            max_tokens=120,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        _log_anthropic_usage(
            role="screener_haiku_fallback",
            model=HAIKU_MODEL_ID,
            max_tokens=120,
            started=started,
            prompt_chars=len(prompt),
            response=resp,
            use_tool=False,
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if m:
            d = json.loads(m.group())
            signal = str(d.get('signal', 'WATCH')).upper()
            if signal not in ('BUY', 'WATCH', 'SKIP'):
                signal = 'WATCH'
            return {'signal': signal, 'confidence': max(0, min(100, int(d.get('confidence', 50)))),
                    'reason': str(d.get('reason', ''))[:80]}
    except Exception as e:
        _log_anthropic_usage(
            role="screener_haiku_fallback",
            model=HAIKU_MODEL_ID,
            max_tokens=120,
            started=started if "started" in locals() else time.monotonic(),
            prompt_chars=len(prompt),
            status="error",
            use_tool=False,
            error=e,
        )
        print(f"  [Haiku] エラー: {e}")
    return {"signal": "WATCH", "confidence": 50, "reason": "AI判定不可"}


def add_ai_signals(candidates: list, market_meta: dict, macro_context: dict | None = None) -> list:
    """
    候補リストに ai_signal / ai_confidence / ai_reason / ai_debate / ai_source を追加して返す。

    ハーネス選択（ALMANAC_SCREENER_HARNESS）:
      - "deepseek" (default): DeepSeek V4-flash 単一コールで 3 視点を内部生成
                              + BUY 上位 3 件のみ Sonnet 第二意見で再判定
      - "legacy":             旧 Sonnet×3並列ディベート → Opus統合（A/B 比較用）
    フォールバック順:
      DeepSeek 失敗 → Sonnet legacy ディベート → Haiku 単体
    """
    if not candidates:
        return candidates

    harness = (get_env("ALMANAC_SCREENER_HARNESS", "deepseek") or "deepseek").lower()

    # ── Stage 1: DeepSeek V4 マルチパースペクティブ（既定） ──
    if harness == "deepseek":
        print("  モード: DeepSeek V4 マルチパースペクティブ → BUY上位 Sonnet 第二意見")
        signals = _call_deepseek_multiperspective(candidates, market_meta, macro_context)
        if signals is not None:
            sig_map = {s["ticker"]: s for s in signals}
            for c in candidates:
                s = sig_map.get(c["ticker"])
                if s is None:
                    c['ai_signal'] = 'WATCH'; c['ai_confidence'] = 50
                    c['ai_reason'] = 'DeepSeek未応答'; c['ai_comment'] = ''
                    c['ai_debate'] = {}; c['ai_source'] = 'deepseek_missing'
                    continue
                c['ai_signal']     = s['signal']
                c['ai_confidence'] = s['confidence']
                c['ai_reason']     = s['reason']
                c['ai_comment']    = f"[{s['signal']} {s['confidence']}%] {s['reason']}"
                c['ai_debate']     = {
                    'bull':  s['bull_view'],
                    'bear':  s['bear_view'],
                    'macro': s['macro_view'],
                }
                c['ai_source']     = 'deepseek_v4'

            # ── Stage 2: BUY 上位 3 件のみ Sonnet 第二意見 ──
            top_buys = sorted(
                [c for c in candidates if c.get('ai_signal') == 'BUY'],
                key=lambda c: c.get('ai_confidence', 0), reverse=True
            )[:3]
            if top_buys:
                print(f"  Sonnet 第二意見: 上位 {len(top_buys)} BUY 件を確認...")
                sonnet_map = _call_sonnet_second_opinion(top_buys, market_meta, macro_context)
                for c in top_buys:
                    so = sonnet_map.get(c['ticker'])
                    if not so:
                        continue
                    c['sonnet_second_signal']     = so['signal']
                    c['sonnet_second_confidence'] = so['confidence']
                    c['sonnet_second_reason']     = so['reason']
                    if so['signal'] in ('WATCH', 'SKIP'):
                        c['ai_signal']     = so['signal']
                        c['ai_confidence'] = so['confidence']
                        c['ai_reason']     = f"[Sonnet降格] {so['reason']}"
                        c['ai_comment']    = f"[{so['signal']} {so['confidence']}%] (Sonnet降格) {so['reason']}"
                        c['ai_source']     = 'deepseek_v4 + sonnet_demoted'
            return candidates
        # DeepSeek 失敗 → legacy / haiku に落とす
        print("  [deepseek] フォールバック: legacy または haiku")

    # ── Legacy: Sonnet×3 ディベート（A/B 比較用 or DeepSeek 失敗時）──
    if harness == "legacy" or harness == "deepseek":
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                print("  モード: Legacy Sonnet×3ディベート → Opus統合")
                out = _call_debate_signals(candidates, market_meta, macro_context)
                for c in out:
                    c.setdefault('ai_source', 'legacy_sonnet_debate')
                return out
            except Exception as e:
                print(f"  [debate] 失敗、Haikuフォールバックへ: {e}")

    # ── Haiku フォールバック ───────────────────────────────────
    print("  モード: Haiku 単体（最終フォールバック）")
    return _haiku_fallback_loop(candidates, market_meta, macro_context)


SIGNAL_HISTORY_FILE = os.path.expanduser('~/portfolio-bot/signal_history.json')

def save_signal_history(candidates: list) -> None:
    from insider_restrictions import filter_signal_records
    candidates = filter_signal_records(candidates)
    """AI シグナルを signal_history.json に追記（勝率トラッキング用）"""
    try:
        history: list = []
        if os.path.exists(SIGNAL_HISTORY_FILE):
            with open(SIGNAL_HISTORY_FILE) as f:
                history = json.load(f)
    except Exception:
        history = []

    today = datetime.now().strftime('%Y-%m-%d')
    new_records = [
        {
            'date': today,
            'ticker': c['ticker'],
            'strategy': c['strategy'],
            'signal': c.get('ai_signal', 'WATCH'),
            'confidence': c.get('ai_confidence', 50),
            'reason': c.get('ai_reason', ''),
            'price_at_signal': c['price'],
            'rsi': c.get('rsi'),
            'volume_ratio': c.get('volume_ratio'),
            'mom_5d': c.get('mom_5d'),
            'debate': c.get('ai_debate', {}),  # Sonnet×3の視点詳細
            'regime': None,  # run_full_screen から上書き
            'outcome_5d': None,  # signal_tracker.py が後で埋める
            'outcome_10d': None,
        }
        for c in candidates if c.get('ai_signal')
    ]

    # 同日同銘柄の重複は上書き
    existing_keys = {(r['date'], r['ticker']) for r in new_records}
    history = [r for r in history if (r['date'], r['ticker']) not in existing_keys]
    history.extend(new_records)
    # 直近180日分のみ保持
    cutoff = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
    history = [r for r in history if r.get('date', '') >= cutoff]

    try:
        from utils import atomic_write_json
        atomic_write_json(SIGNAL_HISTORY_FILE, history)
    except ImportError:
        with open(SIGNAL_HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)


# 後方互換エイリアス
def add_ai_comments(candidates: list, market_meta: dict) -> list:
    return add_ai_signals(candidates, market_meta)

# 銘柄→セクターマッピング（主要銘柄）
TICKER_SECTOR_MAP = {
    # テック
    'AAPL':'テック','MSFT':'テック','NVDA':'テック','AVGO':'テック','AMD':'テック',
    'GOOGL':'テック','META':'テック','AMZN':'テック','TSLA':'テック','CRM':'テック',
    'ORCL':'テック','ADBE':'テック','NOW':'テック','SNPS':'テック','CDNS':'テック',
    'PANW':'テック','ZS':'テック','CRWD':'テック','FTNT':'テック','OKTA':'テック',
    'GTLB':'テック','MDB':'テック','DDOG':'テック','NET':'テック','SNOW':'テック',
    # ヘルスケア
    'JNJ':'ヘルスケア','UNH':'ヘルスケア','LLY':'ヘルスケア','ABBV':'ヘルスケア',
    'MRK':'ヘルスケア','TMO':'ヘルスケア','ABT':'ヘルスケア','DHR':'ヘルスケア',
    # 金融
    'JPM':'金融','BAC':'金融','WFC':'金融','GS':'金融','MS':'金融','BLK':'金融',
    # 資本財
    'RTX':'資本財','HON':'資本財','UPS':'資本財','BA':'資本財','GE':'資本財','CAT':'資本財',
    # エネルギー
    'XOM':'エネルギー','CVX':'エネルギー','COP':'エネルギー',
    # 生活必需品
    'WMT':'生活必需品','COST':'生活必需品','PG':'生活必需品','KO':'生活必需品',
    # 一般消費財
    'NKE':'一般消費財','MCD':'一般消費財','SBUX':'一般消費財',
    # 通信
    'VZ':'通信','T':'通信',
}

TICKERS_FILE = os.path.expanduser('~/portfolio-bot/tickers.json')
RESULTS_FILE = os.path.expanduser('~/portfolio-bot/screen_results.json')

def load_tickers():
    with open(TICKERS_FILE) as f:
        data = json.load(f)
    return data['all']

def get_market_meta():
    """地合いメタ情報：S&P500・日経225の50日線との位置関係"""
    meta = {}
    try:
        spy = yf.Ticker('SPY').history(period='3mo')
        sp_ma50 = spy['Close'].rolling(50).mean().iloc[-1]
        sp_price = spy['Close'].iloc[-1]
        meta['sp500'] = '上' if sp_price > sp_ma50 else '下'
        meta['sp500_price'] = round(float(sp_price), 2)
        meta['sp500_ma50'] = round(float(sp_ma50), 2)
    except:
        meta['sp500'] = '不明'

    try:
        nk = yf.Ticker('^N225').history(period='3mo')
        nk_ma50 = nk['Close'].rolling(50).mean().iloc[-1]
        nk_price = nk['Close'].iloc[-1]
        meta['nikkei'] = '上' if nk_price > nk_ma50 else '下'
        meta['nikkei_price'] = round(float(nk_price), 0)
        meta['nikkei_ma50'] = round(float(nk_ma50), 0)
    except:
        meta['nikkei'] = '不明'

    return meta

def get_credit_ratio(ticker):
    """日本株の信用倍率取得（yfinanceでは取得不可のため暫定でNoneを返す）"""
    # 本来はSBI・楽天APIや各証券会社のスクレイピングが必要
    # 暫定実装：Noneを返し、フィルタリングをスキップ
    return None


def _release_market_data_handles() -> None:
    """yfinance/curl_cffi の残存FDを閉じ、launchd環境のEMFILEを避ける。"""
    try:
        from utils import reset_yfinance_session
        reset_yfinance_session()
    except Exception:
        pass
    try:
        import gc
        gc.collect()
    except Exception:
        pass


def _bulk_download(tickers: list) -> dict:
    """
    全銘柄を一括ダウンロード（VectorBT → yfinance threads の順で試行）。
    Returns: {ticker: pd.DataFrame(Open/High/Low/Close/Volume)}
    """
    # ── macOS のファイルディスクリプタ上限を引き上げる ────────
    # vectorbt が SQLite/HDF5 を多数同時に開くため、230銘柄では
    # デフォルト 256 を超えて EMFILE になる場合がある。
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
    except Exception:
        pass  # Windows / 権限なし の場合はスキップ

    # ── VectorBT (インストール済み時) ──────────────────────────
    try:
        import vectorbt as vbt
        data = vbt.YFData.download(tickers, missing_index="drop", period="1y")
        result: dict = {}
        for t in tickers:
            try:
                df = data.get(t)
                if df is not None and len(df) >= 60:
                    result[t] = df
            except Exception:
                pass
        if result:
            return result
    except Exception:
        pass

    # ── yfinance 一括（threads=True でパラレル取得） ──────────
    try:
        raw = yf.download(
            tickers, period="1y", group_by="ticker",
            progress=False, threads=True, auto_adjust=True,
        )
        result = {}
        for t in tickers:
            try:
                df = raw[t].dropna(how="all") if len(tickers) > 1 else raw.dropna(how="all")
                if len(df) >= 60:
                    result[t] = df
            except Exception:
                pass
        return result
    except Exception:
        return {}


def _screen_rejection_diagnostic(ticker, market_meta, regime='A_強気', hist=None) -> dict:
    """screen_ticker が None を返した時の大まかな落選理由を返す。"""
    try:
        is_japan = ticker.endswith('.T')
        if hist is None or getattr(hist, 'empty', True) or len(hist) < 60:
            return {'reason': 'no_hist'}

        current = float(hist['Close'].iloc[-1])
        prev_close = float(hist['Close'].iloc[-2])
        open_price = float(hist['Open'].iloc[-1])
        avg_notional = float(hist['Volume'].iloc[-20:].mean() * hist['Close'].iloc[-20:].mean())

        if is_japan and avg_notional < 300_000_000:
            return {'reason': 'liquidity_lt_300m_jpy'}
        if not is_japan and avg_notional < 10_000_000:
            return {'reason': 'liquidity_lt_10m_usd'}

        high = hist['High']
        low = hist['Low']
        close = hist['Close']
        tr = pd.concat([
            high - low,
            abs(high - close.shift()),
            abs(low - close.shift())
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        atr_pct = atr / current * 100
        if atr_pct < 2.0:
            return {'reason': 'atr_lt_2pct', 'atr_pct': round(atr_pct, 2)}

        delta = hist['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rsi = float(100 - (100 / (1 + gain / loss)).iloc[-1])
        if rsi != rsi:
            return {'reason': 'rsi_nan'}

        vol_ratio = float(hist['Volume'].iloc[-1] / hist['Volume'].iloc[-20:].mean())
        mom_5d = float((current - hist['Close'].iloc[-6]) / hist['Close'].iloc[-6] * 100) if len(hist) >= 6 else 0
        ma50 = float(hist['Close'].rolling(50).mean().iloc[-1])
        ma50_dev = (current - ma50) / ma50 * 100
        high_52w = float(hist['High'].iloc[-252:].max()) if len(hist) >= 252 else float(hist['High'].max())
        new_52w_high = float(hist['High'].iloc[-5:].max()) >= high_52w * 0.99
        gap_pct = (open_price - prev_close) / prev_close * 100
        change_pct = (current - prev_close) / prev_close * 100

        mkt = 'JP' if is_japan else 'US'
        near_miss = None
        p_uri = get_params('逆張り', mkt, regime)
        if p_uri and rsi < p_uri['rsi'] and mom_5d <= p_uri['mom5d'] and current > ma50 * 0.7:
            near_miss = 'near_reversal_volume_only'

        p_mom = get_params('モメンタム', mkt, regime)
        hi_ok = (new_52w_high if regime == 'A_強気' else current >= high_52w * 0.82)
        if is_japan and regime == 'A_強気' and not hi_ok:
            hi_ok = (ma50_dev >= 5.0)
        if (p_mom and rsi >= p_mom['rsi_min'] and
                p_mom['ma50_min'] <= ma50_dev <= p_mom['ma50_max'] and
                hi_ok and current > ma50):
            near_miss = 'near_momentum_volume_only'

        p_gap = get_params('ギャップダウン', mkt, regime)
        if p_gap and gap_pct <= p_gap['gap']:
            near_miss = 'near_gap_volume_only'

        p_ev = get_params('イベントドリブン後', mkt, regime)
        if p_ev and change_pct <= p_ev['change']:
            near_miss = 'near_event_volume_only'

        return {
            'reason': near_miss or 'no_strategy',
            'rsi': round(rsi, 1),
            'volume_ratio': round(vol_ratio, 2),
            'mom_5d': round(mom_5d, 1),
            'ma50_dev': round(ma50_dev, 1),
            'atr_pct': round(atr_pct, 2),
            'change_pct': round(change_pct, 2),
        }
    except Exception:
        return {'reason': 'diagnostic_error'}


def screen_ticker(ticker, market_meta, regime='A_強気', sector_strength=None, hist=None):
    """単一銘柄のスクリーニング（5戦略）。hist が渡された場合はダウンロードをスキップ。"""
    try:
        is_japan = ticker.endswith('.T')

        # データ取得（52週高値確認のため1年分）
        if hist is None:
            hist = yf.Ticker(ticker).history(period='1y')
        if hist.empty or len(hist) < 60:
            return None

        current = float(hist['Close'].iloc[-1])
        prev_close = float(hist['Close'].iloc[-2])
        open_price = float(hist['Open'].iloc[-1])

        # --- 共通前提条件チェック ---
        # 平均売買代金
        avg_volume_usd = float(hist['Volume'].iloc[-20:].mean() * hist['Close'].iloc[-20:].mean())
        if is_japan:
            # 2026-05-16: ¥10億 → ¥3億に引き下げ。中型・小型株もスクリーニング対象化（流動性は十分ある）
            if avg_volume_usd < 300_000_000:  # 3億円
                return None
        else:
            if avg_volume_usd < 10_000_000:  # $10M
                return None

        # ATR計算
        high = hist['High']
        low = hist['Low']
        close = hist['Close']
        tr = pd.concat([
            high - low,
            abs(high - close.shift()),
            abs(low - close.shift())
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        atr_pct = atr / current * 100

        if atr_pct < 2.0:  # ATR < 株価の2%は除外
            return None

        # --- 各種指標計算 ---
        # RSI
        delta = hist['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rsi_raw = float(100 - (100 / (1 + gain / loss)).iloc[-1])
        if rsi_raw != rsi_raw:  # NaN: 14日間値動きなし → スキップ
            return None
        rsi = rsi_raw

        # 出来高比率
        vol_ratio = float(hist['Volume'].iloc[-1] / hist['Volume'].iloc[-20:].mean())

        # モメンタム
        mom_5d = float((current - hist['Close'].iloc[-6]) / hist['Close'].iloc[-6] * 100) if len(hist) >= 6 else 0
        mom_1m = float((current - hist['Close'].iloc[-22]) / hist['Close'].iloc[-22] * 100) if len(hist) >= 22 else 0
        mom_3m = float((current - hist['Close'].iloc[-66]) / hist['Close'].iloc[-66] * 100) if len(hist) >= 66 else 0

        # 50日移動平均
        ma50 = float(hist['Close'].rolling(50).mean().iloc[-1])
        ma50_dev = (current - ma50) / ma50 * 100  # 50日線乖離率

        # 52週高値
        high_52w = float(hist['High'].iloc[-252:].max()) if len(hist) >= 252 else float(hist['High'].max())
        new_52w_high = float(hist['High'].iloc[-5:].max()) >= high_52w * 0.99  # 直近5日で52週高値更新

        # 始値ギャップ
        gap_pct = (open_price - prev_close) / prev_close * 100

        # 当日値動き
        change_pct = (current - prev_close) / prev_close * 100

        # ストップロス目安（2×ATR）
        stop_loss_atr = round(current - 2 * atr, 2)

        # --- 日本株：信用倍率チェック ---
        if is_japan:
            credit_ratio = get_credit_ratio(ticker)
            if credit_ratio and credit_ratio > 3.0:
                return None

        strategy = None
        reason = ''
        score = 0
        priority = 99

        # === レジーム別パラメータ取得 ===
        mkt = 'JP' if is_japan else 'US'
        p_uri = get_params('逆張り', mkt, regime)
        p_mom = get_params('モメンタム', mkt, regime)
        p_gap = get_params('ギャップダウン', mkt, regime)
        p_ev  = get_params('イベントドリブン後', mkt, regime)

        # === モメンタム条件の事前計算 (2026-05-16: 日本株緩和) ===
        # A_強気: 52週高値更新（直近5日）が必須
        # B_中立/C_弱気: 52週高値の82%以内であればOK（弱相場では高値更新は稀）
        # 日本株は 52週高値更新が稀 → A_強気でも MA50比+5% で代替条件を許可
        _hi_ok = (new_52w_high if regime == 'A_強気' else current >= high_52w * 0.82)
        if is_japan and regime == 'A_強気' and not _hi_ok:
            _hi_ok = (ma50_dev >= 5.0)

        # === 戦略①：逆張り ===
        if (p_uri and
            rsi < p_uri['rsi'] and
            vol_ratio >= p_uri['vol'] and
            mom_5d <= p_uri['mom5d'] and
            current > ma50 * 0.7):  # 暴落しすぎ除外
            strategy = '逆張り'
            reason = f"RSI {rsi:.1f}, 出来高{vol_ratio:.1f}倍, 5日{mom_5d:.1f}%"
            score = (p_uri['rsi'] - rsi) * 2 + vol_ratio * 3
            if change_pct > 0: score += 10
            priority = 4

        # === 戦略②：モメンタム ===
        elif (p_mom and
              rsi >= p_mom['rsi_min'] and
              p_mom['ma50_min'] <= ma50_dev <= p_mom['ma50_max'] and
              _hi_ok and
              current > ma50 and
              vol_ratio >= p_mom['vol']):
            strategy = 'モメンタム'
            _high_label = '52週高値更新' if new_52w_high else f'高値比{current/high_52w*100:.0f}%'
            reason = f"RSI {rsi:.1f}, 50日線乖離{ma50_dev:.1f}%, {_high_label}, 出来高{vol_ratio:.1f}倍"
            score = ma50_dev * 2 + vol_ratio * 5
            if rsi < 75: score += 10
            priority = 5

        # === 戦略③：ギャップダウン（当日限定） ===
        elif (p_gap and
              gap_pct <= p_gap['gap'] and
              vol_ratio >= p_gap['vol']):
            strategy = 'ギャップダウン'
            reason = f"ギャップ{gap_pct:.1f}%, 出来高{vol_ratio:.1f}倍"
            score = abs(gap_pct) * 3 + vol_ratio * 4
            priority = 2

        # === 戦略④：イベントドリブン（前）===
        # event_calendar.pyと連携
        # ここではフラグのみ立てる（event_calendarで処理）

        # === 戦略⑤：イベントドリブン（後）===
        elif (p_ev and
              change_pct <= p_ev['change'] and
              vol_ratio >= p_ev['vol']):
            strategy = 'イベントドリブン後'
            reason = f"決算後急落{change_pct:.1f}%, 出来高{vol_ratio:.1f}倍"
            score = abs(change_pct) * 2 + vol_ratio * 3
            priority = 1

        # === 戦略⑥：出来高異常急増（Volume Surge）===
        # 株価上昇 + 出来高3倍以上 → ブレイクアウト前兆
        elif (vol_ratio >= 3.0 and
              change_pct >= 1.0 and
              ma50_dev >= -5.0 and
              rsi >= 40):
            strategy = 'ボリュームサージ'
            reason = f"出来高{vol_ratio:.1f}倍（3倍基準超）, 株価+{change_pct:.1f}%"
            score = vol_ratio * 10 + change_pct * 3
            priority = 3

        if not strategy:
            # === 戦略⑦：決算前後シグナル（Earnings Catalyst）===
            try:
                import datetime
                cal = yf.Ticker(ticker).calendar
                if cal is not None and not cal.empty:
                    if len(cal.columns) > 0:
                        next_earnings = cal.iloc[0, 0] if hasattr(cal.iloc[0, 0], 'date') else None
                        if next_earnings:
                            days_to_earnings = (next_earnings.date() - datetime.date.today()).days
                            if 0 <= days_to_earnings <= 5:
                                if rsi >= 50 and mom_5d >= 2:
                                    strategy = '決算前モメンタム'
                                    reason = f"決算{days_to_earnings}日前 / RSI {rsi:.1f} / 5日+{mom_5d:.1f}%"
                                    score = 50 + rsi * 0.5 + mom_5d * 2
                                    priority = 1
            except Exception:
                pass  # 決算カレンダー取得失敗は無視

        if not strategy:
            return None

        # 地合いに応じたスコア調整
        if strategy == '逆張り':
            if market_meta.get('sp500') == '下' and not is_japan:
                score *= 0.7
            if market_meta.get('nikkei') == '下' and is_japan:
                score *= 0.7

        # セクター強度によるスコア調整（米国株のみ）
        if not is_japan and sector_strength:
            ticker_sector = TICKER_SECTOR_MAP.get(ticker, '')
            if ticker_sector and ticker_sector in sector_strength:
                if sector_strength[ticker_sector].get('strong'):
                    score *= 1.2  # 強いセクター: +20%
                else:
                    score *= 0.8  # 弱いセクター: -20%

        return {
            'ticker': ticker,
            'strategy': strategy,
            'priority': priority,
            'price': round(current, 2),
            'change_pct': round(change_pct, 2),
            'gap_pct': round(gap_pct, 2),
            'rsi': round(rsi, 1),
            'volume_ratio': round(vol_ratio, 2),
            'mom_5d': round(mom_5d, 1),
            'mom_1m': round(mom_1m, 1),
            'mom_3m': round(mom_3m, 1),
            'ma50_dev': round(ma50_dev, 1),
            'new_52w_high': new_52w_high,
            'atr_pct': round(atr_pct, 2),
            'atr': round(atr, 2),
            'stop_loss_atr': stop_loss_atr,
            'reason': reason,
            'score': round(score, 1),
            'is_japan': is_japan
        }

    except Exception as e:
        return None

def format_candidate_for_claude(c, market_meta):
    """ClaudeへのPrompt用テキスト生成"""
    market = '日本株' if c['is_japan'] else 'S&P500'
    lines = [
        f"【{c['strategy']}候補・{market}】{c['ticker']}",
        f"理由: {c['reason']}",
        f"ATR: 株価の{c['atr_pct']}%",
        f"ストップロス目安: ${c['stop_loss_atr']}（現在値 - 2×ATR）",
    ]
    if c['strategy'] == 'ギャップダウン':
        lines.append("※ニュース確認要（ギャップダウン戦略）")
    return '\n'.join(lines)

def _get_current_regime() -> str:
    """regime_state.json からレジームを取得する（v5.0形式）"""
    try:
        path = os.path.expanduser('~/portfolio-bot/regime_state.json')
        with open(path) as f:
            state = json.load(f)
        if 'regime' in state:
            return state['regime']
        # フォールバック: 旧形式
        macro_score = state.get('macro_score', 5)
        spy_above = bool(state.get('spy_above', True))
        return get_regime(macro_score, spy_above)
    except Exception:
        return 'B_中立'


def run_full_screen(
    us_only: bool = False,
    jp_only: bool = False,
    morning: bool = False,
    ai_comments: bool = False,
):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 全市場スクリーニング開始...")

    # 出力先選択（朝バッチ・JP-only は別ファイル、フロントから両方マージ表示）
    output_path = RESULTS_FILE
    if morning:
        output_path = os.path.expanduser('~/portfolio-bot/screen_results_morning.json')
    elif jp_only:
        output_path = os.path.expanduser('~/portfolio-bot/screen_results_jp.json')

    # レジーム取得（先に行い、スクリーニング閾値に反映）
    regime = _get_current_regime()
    print(f"  レジーム: {regime}")

    # 地合い取得
    print("地合い情報取得中...")
    market_meta = get_market_meta()
    print(f"  S&P500: 50日線の{market_meta.get('sp500', '不明')}")
    print(f"  日経225: 50日線の{market_meta.get('nikkei', '不明')}")

    # セクター強度をループ外で一度だけ読み込む
    sector_strength = {}
    try:
        sp = os.path.expanduser('~/portfolio-bot/sector_strength.json')
        with open(sp) as f:
            sector_strength = json.load(f)
    except Exception:
        pass

    from insider_restrictions import filter_allowed_tickers
    tickers = filter_allowed_tickers(load_tickers())
    if us_only:
        tickers = [t for t in tickers if not t.endswith('.T')]
        print(f"  --us-only: {len(tickers)} 銘柄に絞込")
    elif jp_only:
        tickers = [t for t in tickers if t.endswith('.T')]
        print(f"  --jp-only: {len(tickers)} 銘柄に絞込")
    print(f"対象: {len(tickers)}銘柄")

    # 一括ダウンロード（VectorBT or yfinance threads）
    print("OHLCV 一括ダウンロード中...")
    hist_all = _bulk_download(tickers)
    print(f"  取得成功: {len(hist_all)}/{len(tickers)} 銘柄")

    # 戦略別バケット
    buckets = {
        'イベントドリブン後': [],
        'ギャップダウン': [],
        'イベントドリブン前': [],
        '決算前モメンタム': [],
        'ボリュームサージ': [],
        '逆張り': [],
        'モメンタム': []
    }
    diagnostics = {
        'download_success_count': len(hist_all),
        'download_requested_count': len(tickers),
        'rejection_summary': {},
        'near_misses': [],
        'unbucketed_strategy_counts': {},
    }

    for i, ticker in enumerate(tickers):
        hist = hist_all.get(ticker)  # キャッシュ済みデータを渡す（None の場合は内部でフォールバック取得）
        result = screen_ticker(ticker, market_meta, regime=regime, sector_strength=sector_strength, hist=hist)
        if result:
            s = result['strategy']
            if s in buckets:
                buckets[s].append(result)
            else:
                diagnostics['unbucketed_strategy_counts'][s] = (
                    diagnostics['unbucketed_strategy_counts'].get(s, 0) + 1
                )
            print(f"  ✅ {ticker}: {s} | {result['reason']}")
        else:
            diag = _screen_rejection_diagnostic(ticker, market_meta, regime=regime, hist=hist)
            reason = diag.get('reason', 'unknown')
            diagnostics['rejection_summary'][reason] = diagnostics['rejection_summary'].get(reason, 0) + 1
            if str(reason).startswith('near_') and len(diagnostics['near_misses']) < 20:
                diagnostics['near_misses'].append({'ticker': ticker, **diag})

        if (i + 1) % 50 == 0:
            print(f"  進捗: {i+1}/{len(tickers)}...")

    # 戦略別スコアソート
    for s in buckets:
        buckets[s].sort(key=lambda x: x['score'], reverse=True)

    # 戦略別上位2〜3件、合計最大10件
    selected = []
    limits = {
        'イベントドリブン後': 3,
        'ギャップダウン': 3,
        'イベントドリブン前': 2,
        '決算前モメンタム': 2,
        'ボリュームサージ': 2,
        '逆張り': 3,  # イベント系がない日は逆張りを多めに
        'モメンタム': 2
    }

    # 優先度順に選出（相関管理：同セクター2件以上は1件に絞る）
    used_sectors = {}
    for strategy in ['イベントドリブン後', 'ギャップダウン', 'イベントドリブン前', '決算前モメンタム', 'ボリュームサージ', '逆張り', 'モメンタム']:
        take = limits[strategy]
        count = 0
        for c in buckets[strategy]:
            if count >= take:
                break
            sector = TICKER_SECTOR_MAP.get(c['ticker'], '')
            # 同セクターがすでに同戦略で1件選ばれていたらスキップ
            sector_key = f"{strategy}_{sector}"
            if sector and not c.get('is_japan') and used_sectors.get(sector_key, 0) >= 1:
                continue
            selected.append(c)
            if sector:
                used_sectors[sector_key] = used_sectors.get(sector_key, 0) + 1
            count += 1
        if len(selected) >= 10:
            break

    selected = selected[:10]

    # 結果表示
    print(f"\n完了:")
    for s, candidates in buckets.items():
        if candidates:
            print(f"  {s}: {len(candidates)}件")

    print(f"\n分析候補（上位{len(selected)}件）:")
    for c in selected:
        print(f"  [{c['strategy']}] {c['ticker']}: score {c['score']} | {c['reason']}")

    # マクロコンテキスト取得（任意）
    macro_context = None
    try:
        macro_path = os.path.expanduser('~/portfolio-bot/macro_state.json')
        if os.path.exists(macro_path):
            with open(macro_path) as f:
                macro_context = json.load(f)
    except Exception:
        pass

    # AI コメントは明示 opt-in。決定論的な複合スコア計算と保存は常時維持する。
    if selected:
        if ai_comments:
            print("\nAI シグナル生成中...")
            selected = add_ai_signals(selected, market_meta, macro_context)
        else:
            print("\nAI コメント省略（--ai-comments 未指定）")
        # regime を各候補に追記
        for c in selected:
            c['regime'] = regime
        # ── 複合スコア + 決算ガード + 流動性フロア（S2 強化）─
        try:
            from screening_helpers import (calc_composite_score, get_historical_win_rate,
                                           days_to_next_earnings, liquidity_ok)
        except Exception:
            calc_composite_score = lambda technical, **k: technical  # type: ignore
            get_historical_win_rate = lambda *a, **k: 0.5  # type: ignore
            days_to_next_earnings = lambda *a, **k: None  # type: ignore
            liquidity_ok = lambda *a, **k: True  # type: ignore

        post_filtered: list = []
        for c in selected:
            # 流動性フロア（hist 取得済の volume_ratio から avg_volume を逆算するのは不安定なため、
            # screen_ticker が出した volume_ratio>=最低基準で代用 + price>1 を最低限のチェックに）
            if c.get('price', 0) <= 1.0:
                print(f"  ⏭️  {c['ticker']}: 価格 ≤ $1 → SKIP (流動性懸念)")
                continue
            # 決算日ガード
            d = days_to_next_earnings(c['ticker'])
            c['days_to_earnings']  = d
            c['earnings_imminent'] = (d is not None and 0 <= d <= 2)
            # 複合スコア
            tech_score = float(c.get('score', 0))
            tech_norm  = min(100.0, tech_score)  # screener.score は概ね 0-100 想定
            ai_conv    = float(c.get('ai_confidence', 50))
            wr         = get_historical_win_rate(c.get('strategy', ''), c['ticker'])
            comp = calc_composite_score(
                technical=tech_norm, fundamental=0.0,  # screener はファンダ未統合（短期戦略）
                ai_conviction=ai_conv, win_rate=wr,
                weights=(0.50, 0.0, 0.30, 0.20),
            )
            if c['earnings_imminent'] and c.get('strategy') != 'イベントドリブン前':
                comp = max(0.0, comp - 10)
            # S4D: ニュース・SNS の boost / buzz を加算
            try:
                from screening_helpers import get_news_social_boost
                ns = get_news_social_boost(c['ticker'], side='long')
                comp = min(100.0, max(0.0, comp + ns['news_boost'] + ns['social_buzz']))
                c['news_signal'] = ns['news_signal']
                c['news_score']  = ns['news_score']
                c['news_boost']  = ns['news_boost']
                c['social_bias'] = ns['social_bias']
                c['social_buzz'] = ns['social_buzz']
            except Exception:
                pass
            c['win_rate']        = round(wr, 3)
            c['composite_score'] = comp
            post_filtered.append(c)
        selected = post_filtered
        # 複合スコアで再ソート
        selected.sort(key=lambda x: x.get('composite_score', x.get('score', 0)), reverse=True)
        # S4C: HMM regime confidence で候補数を制限（不確実時は控えめに）
        try:
            from screening_helpers import get_regime_confidence
            conf = get_regime_confidence()
            if conf < 0.6 and selected:
                trim = max(1, int(len(selected) * 0.7))
                print(f"  ⚠️ regime_confidence={conf:.2f} < 0.6 → 候補 {len(selected)}→{trim} に縮小")
                selected = selected[:trim]
        except Exception:
            pass
        # シグナル履歴保存
        save_signal_history(selected)
        if ai_comments:
            buy_count = sum(1 for c in selected if c.get('ai_signal') == 'BUY')
            print(f"  BUY: {buy_count}件 / WATCH: {sum(1 for c in selected if c.get('ai_signal')=='WATCH')}件 / SKIP: {sum(1 for c in selected if c.get('ai_signal')=='SKIP')}件")
        else:
            print(f"  決定論的候補: {len(selected)}件")

    # 地合いテキスト生成
    meta_text = f"S&P500: 50日線の{market_meta.get('sp500', '不明')} / 日経225: 50日線の{market_meta.get('nikkei', '不明')}"

    # 保存
    output = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'market_meta': market_meta,
        'meta_text': meta_text,
        'total_screened': len(tickers),
        'strategy_counts': {s: len(v) for s, v in buckets.items()},
        'candidates': selected,
        'all_candidates': {s: buckets[s][:10] for s in buckets},
        'diagnostics': diagnostics,
    }
    _release_market_data_handles()
    try:
        from utils import atomic_write_json
        atomic_write_json(output_path, output)
    except ImportError:
        with open(output_path, 'w') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"[screener] 結果保存: {output_path}")

    return selected, market_meta, meta_text


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='ALMANAC 全市場スクリーナー')
    parser.add_argument('--us-only', action='store_true',
                        help='米国銘柄のみ対象（朝バッチ用、.T を除外）')
    parser.add_argument('--jp-only', action='store_true',
                        help='日本株のみ対象（15:30 JST バッチ用、.T のみ）')
    parser.add_argument('--morning', action='store_true',
                        help='朝バッチモード（出力先を screen_results_morning.json に分離）')
    parser.add_argument('--ai-comments', action='store_true',
                        help='候補へのLLMコメントを明示的に有効化')
    args = parser.parse_args()
    run_full_screen(
        us_only=args.us_only,
        jp_only=args.jp_only,
        morning=args.morning,
        ai_comments=args.ai_comments,
    )

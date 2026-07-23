"""
social_topic_analyzer.py
========================

StockTwits の message_count / bullish_pct のラベル集計だけでは区別できない
「熱狂 (pump / momentum / meme) vs 業績確変 (earnings beat / new product / catalyst)」
を中間層 LLM (DeepSeek V3 / Qwen fallback) で判定する。

- 入力: social_sentiment.json (social_screener.py 出力)
- 出力: social_topic_analysis.json

閾値: message_count > 200 かつ bullish_pct > 70% の銘柄を選抜（熱狂候補）。
Opus 合成 (analyst/__init__.py _synthesize) に social_topic_context として注入される。
コスト想定: 10-20 銘柄 / 月 ≈ $0.02-0.04 / 月。

Plan Part C 参照。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

SOCIAL_FILE = BASE_DIR / "social_sentiment.json"
NEWS_FILE   = BASE_DIR / "news_signal_candidates.json"  # 補助: 同じ ticker の記事見出し参照
OUTPUT_FILE = BASE_DIR / "social_topic_analysis.json"

try:
    from llm_adapters import call_by_role       # type: ignore
except Exception as e:                          # pragma: no cover
    call_by_role = None                         # type: ignore
    print(f"[social_topic] llm_adapters import failed: {e}", file=sys.stderr)

# --- 熱狂候補の抽出基準 ---
MSG_THRESHOLD      = 200       # plan: message_count > 200
BULLISH_THRESHOLD  = 70.0      # plan: bullish_pct > 70
MAX_TICKERS        = 15


def _append_llm_call_log(row: dict) -> None:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        _append(row)
    except Exception:
        pass


def _log_adapter_usage(
    *,
    role: str,
    result: dict,
    started: float,
    prompt_chars: int,
    max_tokens: int,
    candidate_count: int,
) -> None:
    usage = result.get("usage") or {}
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": result.get("model"),
        "adapter": result.get("adapter"),
        "use_tool": False,
        "max_tokens": max_tokens,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": prompt_chars,
        "status": "error" if result.get("error") else "ok",
        "candidate_count": candidate_count,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }
    if result.get("error"):
        row["error"] = str(result.get("error"))[:500]
        if not usage:
            row["cost_usd"] = 0.0
    _append_llm_call_log(row)

SYSTEM_PROMPT = (
    "あなたは機関投資家向けセンチメント分析官です。"
    "StockTwits で熱狂している銘柄が次のどれに該当するか、JSON で判定してください:\n"
    "  - pump_meme: 材料なきモメンタム / milm / ショートスクイーズ煽り\n"
    "  - earnings_driven: 決算ビート or ガイダンス引き上げ\n"
    "  - product_catalyst: 新製品 / 契約 / FDA など明確イベント\n"
    "  - macro_rotation: マクロ or セクター資金流入\n"
    "  - mixed: 複数要因\n"
    "さらに confidence_pct (0-100), action_bias ∈ {buy, hold, avoid, short}, "
    "one_liner (日本語 50 文字以内) を返す。\n"
    "出力は `{\"evaluations\": [ {ticker, category, confidence_pct, action_bias, "
    "one_liner}, ... ]}` の JSON のみ。Markdown / コメント禁止。"
)


def _load_heated() -> list[dict[str, Any]]:
    if not SOCIAL_FILE.exists():
        print(f"[social_topic] {SOCIAL_FILE.name} not found; nothing to analyze")
        return []
    try:
        data = json.loads(SOCIAL_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[social_topic] parse error: {e}", file=sys.stderr)
        return []
    stocktwits: dict[str, dict] = data.get("stocktwits", {})
    heated: list[dict[str, Any]] = []
    for tk, info in stocktwits.items():
        mc = info.get("message_count", 0) or 0
        bp = info.get("bullish_pct", 0.0) or 0.0
        if mc > MSG_THRESHOLD and bp > BULLISH_THRESHOLD:
            heated.append({
                "ticker":         tk,
                "bullish_pct":    bp,
                "message_count":  mc,
                "is_trending":    info.get("is_trending", False),
                "watchlist_ct":   info.get("watchlist_count"),
                "sentiment":      info.get("sentiment", ""),
            })
    # ソート: trending > message_count 降順
    heated.sort(key=lambda x: (x["is_trending"], x["message_count"]), reverse=True)
    return heated[:MAX_TICKERS]


def _load_news_headlines() -> dict[str, list[str]]:
    if not NEWS_FILE.exists():
        return {}
    try:
        data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for c in data.get("candidates", []):
        out[c.get("ticker", "")] = c.get("top_headlines", [])[:2]
    return out


def _build_user_prompt(heated: list[dict], news_map: dict[str, list[str]]) -> str:
    lines = [
        "以下は直近 24 時間で StockTwits の強気比率が 70% を超え、",
        "メッセージ数 200 超の『熱狂候補』です。可能であれば参考記事見出しを併記しています。",
        "",
    ]
    for h in heated:
        t = h["ticker"]
        lines.append(
            f"## {t}  bullish {h['bullish_pct']:.1f}%  msgs {h['message_count']}"
            f"  trending={h['is_trending']}  sentiment={h['sentiment']}"
        )
        heads = news_map.get(t, [])
        if heads:
            for hd in heads:
                lines.append(f"  - {hd}")
        else:
            lines.append("  - （ニュース材料: 直近記事無し → 熱狂が pump/meme 系の可能性）")
        lines.append("")
    lines.append(
        "各銘柄について category / confidence_pct / action_bias / one_liner を判定してください。"
    )
    return "\n".join(lines)


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").lstrip("json").strip()
    m = _JSON_BLOCK_RE.search(s)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        try:
            return json.loads(m.group(0).rstrip(",") + "}")
        except Exception:
            return None


def analyze(dry_run: bool = False) -> dict:
    heated = _load_heated()
    if not heated:
        out = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "evaluations": [],
            "note": f"no tickers matched (msg>{MSG_THRESHOLD} & bullish>{BULLISH_THRESHOLD}%)",
        }
        if not dry_run:
            OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        return out

    if call_by_role is None:
        out = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "evaluations": [],
            "error": "llm_adapters unavailable",
        }
        if not dry_run:
            OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        return out

    news_map = _load_news_headlines()
    user_prompt = _build_user_prompt(heated, news_map)
    print(f"[social_topic] analyzing {len(heated)} heated tickers via DeepSeek V3…")

    started = time.monotonic()
    res = call_by_role(
        "social_topic_deepdive",
        SYSTEM_PROMPT,
        user_prompt,
        max_tokens=2500,
        temperature=0.2,
        json_mode=True,
    )
    _log_adapter_usage(
        role="social_topic_deepdive",
        result=res,
        started=started,
        prompt_chars=len(SYSTEM_PROMPT) + len(user_prompt),
        max_tokens=2500,
        candidate_count=len(heated),
    )
    content = res.get("content", "")
    err     = res.get("error")
    parsed  = _extract_json(content) if content else None

    if (err or not parsed):
        print(f"[social_topic] DeepSeek failed ({err or 'parse error'}); fallback to Qwen")
        started = time.monotonic()
        res = call_by_role(
            "social_topic_fallback",
            SYSTEM_PROMPT,
            user_prompt,
            max_tokens=2500,
            temperature=0.2,
            json_mode=True,
        )
        _log_adapter_usage(
            role="social_topic_fallback",
            result=res,
            started=started,
            prompt_chars=len(SYSTEM_PROMPT) + len(user_prompt),
            max_tokens=2500,
            candidate_count=len(heated),
        )
        content = res.get("content", "")
        err     = res.get("error")
        parsed  = _extract_json(content) if content else None

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tickers_evaluated": [h["ticker"] for h in heated],
        "adapter":     res.get("adapter"),
        "model":       res.get("model"),
        "usage":       res.get("usage"),
        "evaluations": (parsed.get("evaluations") if parsed and isinstance(parsed, dict) else []) or [],
    }
    if err:
        out["error"] = err
    if not parsed:
        out["raw_response"] = content[:2000]

    if not dry_run:
        OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[social_topic] wrote {OUTPUT_FILE.name}: {len(out['evaluations'])} evaluations")
    return out


def format_for_prompt(max_entries: int = 8) -> str:
    """Opus 合成プロンプトに差し込むコンテキスト文字列を返す。"""
    if not OUTPUT_FILE.exists():
        return ""
    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    evals = data.get("evaluations", [])[:max_entries]
    if not evals:
        return ""
    lines = ["## 🔥 Social Heat Classification (DeepSeek)", ""]
    for e in evals:
        t = e.get("ticker", "?")
        cat = e.get("category", "?")
        conf = e.get("confidence_pct", "?")
        bias = e.get("action_bias", "?")
        one = e.get("one_liner", "")
        lines.append(f"- **{t}** [{cat}/{bias}/conf {conf}] {one}")
    return "\n".join(lines)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = analyze(dry_run=dry)
    if dry:
        print(json.dumps(out, indent=2, ensure_ascii=False))

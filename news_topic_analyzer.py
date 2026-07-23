"""
news_topic_analyzer.py
======================

FinBERT / news_signal_candidates.json の単純な sentiment ラベルでは拾えない
「材料の耐久性・波及先・想定 hold 期間」を中間層 LLM (DeepSeek V3 / Qwen fallback)
で構造化分析するモジュール。

- 入力: news_signal_candidates.json  (news_screener.py の出力)
- 出力: news_topic_analysis.json

Opus 合成 (analyst/__init__.py _synthesize) に news_topic_context として注入される。
コスト想定: 40-60 記事 / 月 × DeepSeek-chat ≈ $0.10-0.20 / 月。

Plan Part C 参照。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# 入出力
CANDIDATES_FILE = BASE_DIR / "news_signal_candidates.json"
OUTPUT_FILE     = BASE_DIR / "news_topic_analysis.json"

# LLM ルーティング
try:
    from llm_adapters import call_by_role          # type: ignore
except Exception as e:                             # pragma: no cover
    call_by_role = None                            # type: ignore
    print(f"[news_topic] llm_adapters import failed: {e}", file=sys.stderr)

# ---------------------------------------------------------------------------
# 選別: |sentiment_score| >= THRESHOLD の上位 N 銘柄のみ深掘りする。
# ---------------------------------------------------------------------------
SCORE_THRESHOLD = 30     # |score| >= 30 のみ対象（弱シグナルを切り捨て）
MAX_TICKERS     = 20     # DeepSeek 呼び出し上限
ARTICLES_PER_TK = 3      # 1 銘柄につき top_headlines 3 本までプロンプトに投入


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
    "あなたは機関投資家向けの株式アナリストです。"
    "ニュース記事の見出しから、1 銘柄あたり次の 5 項目を JSON で返してください:\n"
    "  - catalyst_type: {earnings, guidance, product, macro, regulatory, m_and_a, "
    "people, litigation, tech, unknown} のいずれか\n"
    "  - durability: short (数日〜2週間) / medium (1〜3ヶ月) / long (四半期以上)\n"
    "  - impact_magnitude: 0-100 (株価への見込みインパクト)\n"
    "  - ripple_tickers: 波及先候補 (最大 5) — 半導体/決済/EV など明らかな連想\n"
    "  - hold_horizon_days: 想定 hold 期間 (整数)\n"
    "  - one_liner: 日本語で 50 文字以内の所見\n"
    "回答は `{\"analyses\": [ {ticker, ...}, ... ]}` の JSON のみ。コメントや"
    "Markdown を混ぜないこと。"
)


def _load_candidates() -> list[dict[str, Any]]:
    if not CANDIDATES_FILE.exists():
        print(f"[news_topic] {CANDIDATES_FILE.name} not found; nothing to analyze")
        return []
    try:
        data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[news_topic] failed to parse candidates JSON: {e}", file=sys.stderr)
        return []
    cands: list[dict[str, Any]] = data.get("candidates", [])
    # |sentiment_score| >= THRESHOLD でフィルタ、|score| 降順にソート
    filtered = [c for c in cands if abs(c.get("sentiment_score", 0)) >= SCORE_THRESHOLD]
    filtered.sort(key=lambda c: abs(c.get("sentiment_score", 0)), reverse=True)
    return filtered[:MAX_TICKERS]


def _build_user_prompt(batch: list[dict[str, Any]]) -> str:
    lines = [
        "以下は現在注目度の高い銘柄とその代表記事ヘッドラインです。",
        "各銘柄について上記 5 項目 + one_liner を JSON で返してください。",
        "",
    ]
    for c in batch:
        t = c.get("ticker", "?")
        name = c.get("name", "")
        score = c.get("sentiment_score", 0)
        signal = c.get("signal", "")
        heads = c.get("top_headlines", [])[:ARTICLES_PER_TK]
        lines.append(f"## {t} ({name}) — score {score:+d}  signal {signal}")
        for h in heads:
            lines.append(f"  - {h}")
        lines.append("")
    return "\n".join(lines)


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    # ```json ... ``` を剥がす
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").lstrip("json").strip()
    m = _JSON_BLOCK_RE.search(stripped)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        # よくある truncate: 末尾括弧付与リトライ
        candidate = m.group(0).rstrip(",") + "}"
        try:
            return json.loads(candidate)
        except Exception:
            return None


def analyze(dry_run: bool = False) -> dict:
    batch = _load_candidates()
    if not batch:
        out = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "analyses": [],
            "note": "no candidates above threshold",
        }
        if not dry_run:
            OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        return out

    if call_by_role is None:
        out = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "analyses": [],
            "error": "llm_adapters unavailable",
        }
        if not dry_run:
            OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        return out

    user_prompt = _build_user_prompt(batch)
    print(f"[news_topic] analyzing {len(batch)} tickers via DeepSeek V3…")
    started = time.monotonic()
    res = call_by_role(
        "news_topic_deepdive",
        SYSTEM_PROMPT,
        user_prompt,
        max_tokens=3000,
        temperature=0.2,
        json_mode=True,
    )
    _log_adapter_usage(
        role="news_topic_deepdive",
        result=res,
        started=started,
        prompt_chars=len(SYSTEM_PROMPT) + len(user_prompt),
        max_tokens=3000,
        candidate_count=len(batch),
    )
    content = res.get("content", "")
    err     = res.get("error")
    parsed  = _extract_json(content) if content else None

    # DeepSeek 失敗時は Qwen fallback
    if (err or not parsed) and call_by_role is not None:
        print(f"[news_topic] DeepSeek failed ({err or 'parse error'}); fallback to Qwen")
        started = time.monotonic()
        res = call_by_role(
            "news_topic_fallback",
            SYSTEM_PROMPT,
            user_prompt,
            max_tokens=3000,
            temperature=0.2,
            json_mode=True,
        )
        _log_adapter_usage(
            role="news_topic_fallback",
            result=res,
            started=started,
            prompt_chars=len(SYSTEM_PROMPT) + len(user_prompt),
            max_tokens=3000,
            candidate_count=len(batch),
        )
        content = res.get("content", "")
        err     = res.get("error")
        parsed  = _extract_json(content) if content else None

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tickers_analyzed": [c.get("ticker") for c in batch],
        "adapter":  res.get("adapter"),
        "model":    res.get("model"),
        "usage":    res.get("usage"),
        "analyses": (parsed.get("analyses") if parsed and isinstance(parsed, dict) else []) or [],
    }
    if err:
        out["error"] = err
    if not parsed:
        out["raw_response"] = content[:2000]

    if not dry_run:
        OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[news_topic] wrote {OUTPUT_FILE.name}: {len(out['analyses'])} analyses")
    return out


def format_for_prompt(max_entries: int = 10) -> str:
    """Opus 合成プロンプトに差し込むコンテキスト文字列を返す。"""
    if not OUTPUT_FILE.exists():
        return ""
    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    analyses = data.get("analyses", [])[:max_entries]
    if not analyses:
        return ""
    lines = ["## 📰 News Topic Deep-dive (DeepSeek)", ""]
    for a in analyses:
        t = a.get("ticker", "?")
        cat = a.get("catalyst_type", "unknown")
        dur = a.get("durability", "?")
        imp = a.get("impact_magnitude", "?")
        hold = a.get("hold_horizon_days", "?")
        one = a.get("one_liner", "")
        ripple = ", ".join(a.get("ripple_tickers", [])[:5])
        lines.append(
            f"- **{t}** [{cat}/{dur}/impact {imp}/hold {hold}d] {one}"
            + (f"  波及: {ripple}" if ripple else "")
        )
    return "\n".join(lines)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = analyze(dry_run=dry)
    if dry:
        print(json.dumps(out, indent=2, ensure_ascii=False))

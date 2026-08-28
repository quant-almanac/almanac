"""
news_topic_analyzer.py
======================

FinBERT / news_signal_candidates.json の単純な sentiment ラベルでは拾えない
「材料の耐久性・波及先・想定 hold 期間」を中間層 LLM (DeepSeek V3) で
構造化分析するモジュール。

- 入力: news_signal_candidates.json  (news_screener.py の出力)
- 出力: news_topic_analysis.json

Opus 合成 (analyst/__init__.py _synthesize) に news_topic_context として注入される。

⚠️ 2026-08-28 の監査で判明した停止状態と、その修正:
  20 銘柄を 1 回の呼び出しにまとめて max_tokens=3000 を要求していたため、
  実測 44 回中 43 回が完了トークン 2999-3000 に張り付いて出力が切れていた。
  切れた JSON は閉じ括弧を持たないので、抽出正規表現が構造的にマッチせず
  毎回 parse に失敗し、fallback の Qwen も OpenRouter 残高切れ (402) で
  失敗するため、直近 10 営業日の分析結果は 0 件だった。
  にもかかわらず会計ログは全 44 回が status=ok — API が 200 を返したことしか
  見ていなかったため、毎日コストだけ払って何も生産していない状態が
  正常に見えていた。

  対策:
    - 小バッチ (既定 5 銘柄) へ分割し、1 バッチあたりの出力量を抑える
    - parse だけでなくスキーマ検証まで通って初めて status=ok を名乗る
    - truncation / parse / schema / transport / quota を区別して記録
    - 402 を掴んだら同一 run 内で以降の呼び出しを止める (circuit breaker)
    - 全バッチ成功時のみ最終分析へ注入。部分成功は監査用に保存するだけ
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from topic_lane_contract import (  # noqa: E402
    ERROR_PARSE,
    ERROR_QUOTA,
    ERROR_SCHEMA,
    ERROR_TRANSPORT,
    ERROR_TRUNCATION,
    RUN_STATUS_FAILED,
    RUN_STATUS_NO_CANDIDATES,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_SUCCESS,
    RUN_STATUS_UNAVAILABLE,
    build_run_record,
    classify_error,
    extract_json,
    is_quota_error,
    load_and_gate,
    looks_truncated,
    validate_rows,
    write_heartbeat,
)

LANE = "news_topic"
FRESHNESS_SOURCE = "news_topic"

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
MAX_TICKERS     = 20     # 1 run で扱う銘柄数の上限
ARTICLES_PER_TK = 3      # 1 銘柄につき top_headlines 3 本までプロンプトに投入

# ⚠️ バッチサイズ。20 銘柄一括は出力が max_tokens に張り付いて必ず切れていた。
# 5 銘柄なら 1 銘柄あたり ~600 トークン使えるので、6 項目の構造化出力には十分。
BATCH_SIZE = int(os.environ.get("NEWS_TOPIC_BATCH_SIZE", "5") or 5)
MAX_TOKENS_PER_BATCH = int(os.environ.get("NEWS_TOPIC_MAX_TOKENS", "3000") or 3000)

# ⚠️ Qwen fallback は既定で無効。OpenRouter の残高切れ (402) が続いており、
# 有効にすると毎バッチで確実に失敗する呼び出しを 1 回ずつ足すだけになる。
# 残高を補充したら NEWS_TOPIC_FALLBACK=1 で手動復帰させる (毎日の事前 probe は
# それ自体が無駄なので行わない)。
FALLBACK_ENABLED = (os.environ.get("NEWS_TOPIC_FALLBACK", "0") or "0").lower() in ("1", "true", "yes")

# スキーマ検証で必須にする項目
REQUIRED_FIELDS = ("ticker", "catalyst_type", "durability", "impact_magnitude")


def _append_llm_call_log(row: dict) -> bool:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        return bool(_append(row))
    except Exception as exc:
        print(f"[news_topic] accounting log write failed: {exc}", file=sys.stderr)
        return False


def _log_adapter_usage(
    *,
    role: str,
    result: dict,
    started: float,
    prompt_chars: int,
    max_tokens: int,
    candidate_count: int,
    status: str,
    failure_kind: str | None,
    run_id: str,
    batch_id: str,
) -> None:
    """会計ログへ 1 行記録する。

    ⚠️ status は「API が 200 を返したか」ではなく「使える結果になったか」。
    parse/schema まで通ったときだけ ok。以前は API 成功だけを見ており、
    43 回連続で実質失敗しているのに全行 ok と記録されていた。
    """
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
        "status": status,
        "candidate_count": candidate_count,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "run_id": run_id,
        "batch_id": batch_id,
    }
    if failure_kind:
        row["failure_kind"] = failure_kind
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


def _load_candidates() -> tuple[list[dict[str, Any]], int, str | None]:
    """(選別済み候補, 入力総数, 入力の as_of) を返す。"""
    if not CANDIDATES_FILE.exists():
        print(f"[news_topic] {CANDIDATES_FILE.name} not found; nothing to analyze")
        return [], 0, None
    try:
        data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[news_topic] failed to parse candidates JSON: {e}", file=sys.stderr)
        return [], 0, None
    cands: list[dict[str, Any]] = data.get("candidates", [])
    source_as_of = data.get("generated_at") or data.get("as_of")
    filtered = [c for c in cands if abs(c.get("sentiment_score", 0)) >= SCORE_THRESHOLD]
    filtered.sort(key=lambda c: abs(c.get("sentiment_score", 0)), reverse=True)
    return filtered[:MAX_TICKERS], len(cands), source_as_of


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


def _chunk(rows: list, size: int) -> list[list]:
    size = max(1, int(size))
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def _run_one_batch(
    batch: list[dict[str, Any]],
    *,
    role: str,
    run_id: str,
    batch_id: str,
) -> dict:
    """1 バッチを 1 回だけ呼ぶ。結果を分類して返す (例外は投げない)。"""
    user_prompt = _build_user_prompt(batch)
    tickers = [c.get("ticker") for c in batch]
    started = time.monotonic()
    try:
        res = call_by_role(
            role,
            SYSTEM_PROMPT,
            user_prompt,
            max_tokens=MAX_TOKENS_PER_BATCH,
            temperature=0.2,
            json_mode=True,
        )
    except Exception as exc:  # アダプタが例外を投げた場合も run 全体は続ける
        res = {"content": "", "error": f"{type(exc).__name__}: {exc}"}

    err = res.get("error")
    usage = res.get("usage") or {}
    content = res.get("content", "")

    failure_kind: str | None = None
    rows: list[dict] = []

    if err:
        failure_kind = classify_error(err)
    else:
        parsed = extract_json(content)
        if parsed is None:
            # 出力が max_tokens に張り付いていたなら truncation、そうでなければ
            # 純粋な parse 失敗。原因が違えば対策も違う (前者はバッチ縮小、
            # 後者はプロンプト調整) ので区別して記録する。
            failure_kind = ERROR_TRUNCATION if looks_truncated(usage, MAX_TOKENS_PER_BATCH) else ERROR_PARSE
        else:
            schema = validate_rows(
                parsed,
                list_key="analyses",
                required_fields=REQUIRED_FIELDS,
                expected_tickers=tickers,
            )
            if not schema.ok:
                failure_kind = ERROR_SCHEMA
            else:
                rows = schema.rows

    status = "ok" if failure_kind is None else "error"
    _log_adapter_usage(
        role=role,
        result=res,
        started=started,
        prompt_chars=len(SYSTEM_PROMPT) + len(user_prompt),
        max_tokens=MAX_TOKENS_PER_BATCH,
        candidate_count=len(batch),
        status=status,
        failure_kind=failure_kind,
        run_id=run_id,
        batch_id=batch_id,
    )

    return {
        "batch_id": batch_id,
        "role": role,
        "tickers": tickers,
        "status": status,
        "failure_kind": failure_kind,
        "adapter": res.get("adapter"),
        "model": res.get("model"),
        "usage": usage or None,
        "rows": rows,
        "error": str(err)[:500] if err else None,
        "raw_response": (content[:800] if (failure_kind and content) else None),
    }


def analyze(dry_run: bool = False) -> dict:
    run_id = f"news-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    started_at = time.time()
    selected, input_count, source_as_of = _load_candidates()

    def _finish(record: dict, analyses: list[dict]) -> dict:
        out = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "analyses": analyses,
            **record,
        }
        if not dry_run:
            OUTPUT_FILE.write_text(
                json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[news_topic] run={run_id} status={record['run_status']} "
                  f"analyses={len(analyses)} "
                  f"({record['success_count']}/{record['selected_count']} batches ok)")
            write_heartbeat(LANE, record)
        return out

    if not selected:
        return _finish(build_run_record(
            lane=LANE, run_id=run_id, run_status=RUN_STATUS_NO_CANDIDATES,
            started_at=started_at, input_count=input_count,
            selected_count=0, success_count=0, batches=[],
            source_as_of=source_as_of,
        ), [])

    if call_by_role is None:
        return _finish(build_run_record(
            lane=LANE, run_id=run_id, run_status=RUN_STATUS_UNAVAILABLE,
            started_at=started_at, input_count=input_count,
            selected_count=len(selected), success_count=0, batches=[],
            source_as_of=source_as_of, error_code="llm_adapters unavailable",
        ), [])

    batches = _chunk(selected, BATCH_SIZE)
    print(f"[news_topic] run={run_id}: {len(selected)} tickers "
          f"in {len(batches)} batch(es) of <={BATCH_SIZE} via DeepSeek")

    results: list[dict] = []
    analyses: list[dict] = []
    circuit_open = False       # quota を掴んだら以降の呼び出しを止める
    error_code: str | None = None
    fallback_status = "disabled" if not FALLBACK_ENABLED else "not_attempted"

    for i, batch in enumerate(batches, start=1):
        batch_id = f"{run_id}#b{i}"
        if circuit_open:
            results.append({
                "batch_id": batch_id, "role": "news_topic_deepdive",
                "tickers": [c.get("ticker") for c in batch],
                "status": "skipped", "failure_kind": ERROR_QUOTA,
                "rows": [], "error": "skipped: quota circuit breaker open",
            })
            continue

        res = _run_one_batch(batch, role="news_topic_deepdive",
                             run_id=run_id, batch_id=batch_id)

        if res["failure_kind"] == ERROR_QUOTA:
            # 残高切れは待っても直らない。同じ run 内で叩き続けない。
            circuit_open = True
            error_code = ERROR_QUOTA

        if res["status"] != "ok" and FALLBACK_ENABLED and not circuit_open:
            fb = _run_one_batch(batch, role="news_topic_fallback",
                                run_id=run_id, batch_id=f"{batch_id}/fb")
            fallback_status = "used"
            if fb["failure_kind"] == ERROR_QUOTA:
                circuit_open = True
                error_code = ERROR_QUOTA
            if fb["status"] == "ok":
                res = fb
            else:
                results.append(fb)

        results.append(res)
        if res["status"] == "ok":
            analyses.extend(res["rows"])

    ok_batches = sum(1 for r in results if r["status"] == "ok")
    total_batches = len(batches)
    if ok_batches == total_batches:
        run_status = RUN_STATUS_SUCCESS
    elif ok_batches > 0:
        run_status = RUN_STATUS_PARTIAL
    else:
        run_status = RUN_STATUS_FAILED

    if error_code is None and run_status != RUN_STATUS_SUCCESS:
        kinds = [r.get("failure_kind") for r in results if r.get("failure_kind")]
        error_code = kinds[0] if kinds else ERROR_TRANSPORT

    record = build_run_record(
        lane=LANE, run_id=run_id, run_status=run_status,
        started_at=started_at, input_count=input_count,
        selected_count=len(selected), success_count=ok_batches,
        batches=results, source_as_of=source_as_of,
        error_code=error_code, fallback_status=fallback_status,
    )
    # ⚠️ 部分成功でも analyses は保存する (監査用)。ただし最終分析へ注入されるかは
    # format_for_prompt() の injection_gate が run_status を見て決める —— ここで
    # 「一部だけ分析できた結果」を全体の所見として通さない。
    return _finish(record, analyses)


def format_for_prompt(max_entries: int = 10) -> str:
    """Opus 合成プロンプトに差し込むコンテキスト文字列を返す。

    ⚠️ 成功・スキーマ有効・鮮度内の 3 条件を満たさなければ空へ倒す
    (fail-closed)。以前は generated_at を一切見ておらず、何日前の出力でも、
    また 0 件でエラーだけが入ったファイルでも、そのまま注入され得た。
    """
    data, reason = load_and_gate(OUTPUT_FILE, source=FRESHNESS_SOURCE)
    if data is None:
        if reason not in ("file not found",):
            print(f"[news_topic] context suppressed ({reason})", file=sys.stderr)
        return ""
    analyses = (data.get("analyses") or [])[:max_entries]
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

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
import sys
import time
from pathlib import Path
from typing import Any
from utils import LockBusy, process_lock

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

SOCIAL_FILE = BASE_DIR / "social_sentiment.json"
NEWS_FILE   = BASE_DIR / "news_signal_candidates.json"  # 補助: 同じ ticker の記事見出し参照
OUTPUT_FILE = BASE_DIR / "social_topic_analysis.json"

LANE = "social_topic"
FRESHNESS_SOURCE = "social_topic"

try:
    from llm_adapters import call_by_role       # type: ignore
except Exception as e:                          # pragma: no cover
    call_by_role = None                         # type: ignore
    print(f"[social_topic] llm_adapters import failed: {e}", file=sys.stderr)

# --- 熱狂候補の抽出基準 ---
MSG_THRESHOLD      = 200       # plan: message_count > 200
BULLISH_THRESHOLD  = 70.0      # plan: bullish_pct > 70
MAX_TICKERS        = 15

REQUIRED_FIELDS = (
    "ticker", "category", "confidence_pct", "action_bias", "one_liner",
)
_CATEGORIES = {"pump_meme", "earnings_driven", "product_catalyst", "macro_rotation", "mixed"}
_ACTION_BIASES = {"buy", "hold", "avoid", "short"}
FIELD_SPECS = {
    "ticker": (str, lambda v: bool(str(v).strip())),
    "category": (str, lambda v: str(v).lower() in _CATEGORIES),
    "confidence_pct": ((int, float), lambda v: not isinstance(v, bool) and 0 <= float(v) <= 100),
    "action_bias": (str, lambda v: str(v).lower() in _ACTION_BIASES),
    "one_liner": (str, lambda v: 0 < len(str(v)) <= 200),
}


def _append_llm_call_log(row: dict) -> bool:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        return bool(_append(row))
    except Exception as exc:
        print(f"[social_topic] accounting log write failed: {exc}", file=sys.stderr)
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
) -> bool:
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
    written = _append_llm_call_log(row)
    if not written:
        print(
            f"[social_topic] accounting row lost: {json.dumps(row, ensure_ascii=False)}",
            file=sys.stderr,
        )
    return written

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


def _source_meta() -> dict:
    """上流 social_sentiment.json のメタ情報 (読めなければ空)。"""
    if not SOCIAL_FILE.exists():
        return {}
    try:
        return json.loads(SOCIAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _source_as_of() -> str | None:
    return _source_meta().get("generated_at")


def _source_sample_size() -> int:
    st = _source_meta().get("stocktwits") or {}
    return len(st) if isinstance(st, dict) else 0


def _load_heated_input() -> tuple[list[dict[str, Any]], int, str | None, str]:
    from topic_lane_contract import INPUT_EMPTY, INPUT_MISSING, INPUT_OK, INPUT_UNREADABLE

    if not SOCIAL_FILE.exists():
        print(f"[social_topic] {SOCIAL_FILE.name} not found; nothing to analyze")
        return [], 0, None, INPUT_MISSING
    try:
        data = json.loads(SOCIAL_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[social_topic] parse error: {e}", file=sys.stderr)
        return [], 0, None, INPUT_UNREADABLE
    if not isinstance(data, dict) or not isinstance(data.get("stocktwits"), dict):
        print("[social_topic] stocktwits input is not an object", file=sys.stderr)
        return [], 0, (data.get("generated_at") if isinstance(data, dict) else None), INPUT_UNREADABLE
    stocktwits: dict[str, dict] = data["stocktwits"]
    heated: list[dict[str, Any]] = []
    for tk, info in stocktwits.items():
        if not isinstance(tk, str) or not tk.strip() or not isinstance(info, dict):
            return [], len(stocktwits), data.get("generated_at"), INPUT_UNREADABLE
        mc = info.get("message_count", 0) or 0
        bp = info.get("bullish_pct", 0.0) or 0.0
        if isinstance(mc, bool) or not isinstance(mc, (int, float)):
            return [], len(stocktwits), data.get("generated_at"), INPUT_UNREADABLE
        if isinstance(bp, bool) or not isinstance(bp, (int, float)):
            return [], len(stocktwits), data.get("generated_at"), INPUT_UNREADABLE
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
    selected = heated[:MAX_TICKERS]
    return (
        selected,
        len(stocktwits),
        data.get("generated_at"),
        INPUT_OK if selected else INPUT_EMPTY,
    )


def _load_heated() -> list[dict[str, Any]]:
    """Compatibility wrapper for callers/tests that only need selected rows."""
    return _load_heated_input()[0]


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


def _analyze_unlocked(dry_run: bool = False) -> dict:
    from topic_lane_contract import (
        ERROR_PARSE,
        ERROR_SCHEMA,
        ERROR_TRUNCATION,
        FAILING_INPUT_STATES,
        INPUT_OK,
        RUN_STATUS_FAILED,
        RUN_STATUS_NO_CANDIDATES,
        RUN_STATUS_PARTIAL,
        RUN_STATUS_SUCCESS,
        RUN_STATUS_UNAVAILABLE,
        build_run_record,
        classify_error,
        extract_json,
        looks_truncated,
        validate_rows,
        write_heartbeat,
    )
    from utils import atomic_write_json

    _started_at = time.time()
    _run_id = f"social-{time.strftime('%Y%m%d-%H%M%S')}"
    heated, input_count, source_as_of, input_state = _load_heated_input()

    def _finish(record: dict, evaluations: list[dict], *, meta: dict | None = None) -> dict:
        out = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "evaluations": evaluations,
            **(meta or {}),
            **record,
        }
        if not dry_run:
            atomic_write_json(OUTPUT_FILE, out)
            print(
                f"[social_topic] run={_run_id} status={record['run_status']} "
                f"input={record['input_state']} evaluations={len(evaluations)} "
                f"calls={record['call_count']}",
            )
            write_heartbeat(LANE, record)
        return out

    if input_state in FAILING_INPUT_STATES:
        return _finish(build_run_record(
            lane=LANE, run_id=_run_id, run_status=RUN_STATUS_FAILED,
            started_at=_started_at, input_count=input_count,
            selected_count=0, success_count=0, batches=[],
            source_as_of=source_as_of, input_state=input_state,
            error_code=f"input_{input_state}",
        ), [])

    if not heated:
        # ⚠️ 以前はここで何も print せずに戻っていたため、cron ログが
        # 0 バイトのままになり「動いていない」と「動いたが 0 件」を外から
        # 区別できなかった。実際にはこの分岐を 4 ヶ月間毎日通っていた
        # (選抜条件 message_count>200 が上流の 1 ページ標本 (最大30) に対して
        # 到達不能だったため)。0 件でも必ず状態と heartbeat を残す。
        record = build_run_record(
            lane=LANE, run_id=_run_id, run_status=RUN_STATUS_NO_CANDIDATES,
            started_at=_started_at, input_count=input_count,
            selected_count=0, success_count=0, batches=[],
            source_as_of=source_as_of, input_state=input_state,
        )
        return _finish(record, [], meta={
            "note": f"no tickers matched (msg>{MSG_THRESHOLD} & bullish>{BULLISH_THRESHOLD}%)",
        })

    if call_by_role is None:
        record = build_run_record(
            lane=LANE, run_id=_run_id, run_status=RUN_STATUS_UNAVAILABLE,
            started_at=_started_at, input_count=input_count,
            selected_count=len(heated), success_count=0, batches=[],
            source_as_of=source_as_of, input_state=INPUT_OK,
            error_code="llm_adapters unavailable",
        )
        return _finish(record, [], meta={"error": "llm_adapters unavailable"})

    news_map = _load_news_headlines()
    user_prompt = _build_user_prompt(heated, news_map)
    print(f"[social_topic] analyzing {len(heated)} heated tickers via DeepSeek V3…")

    selected_tickers = [h["ticker"] for h in heated]
    calls: list[dict] = []
    accounting_logged_count = 0

    def _run(role: str, batch_id: str) -> tuple[dict, list[dict]]:
        nonlocal accounting_logged_count
        started = time.monotonic()
        try:
            result = call_by_role(
                role, SYSTEM_PROMPT, user_prompt,
                max_tokens=2500, temperature=0.2, json_mode=True,
            )
        except Exception as exc:
            result = {"content": "", "error": f"{type(exc).__name__}: {exc}"}
        error = result.get("error")
        usage = result.get("usage") or {}
        parsed = extract_json(result.get("content") or "") if not error else None
        rows: list[dict] = []
        if error:
            failure_kind = classify_error(error)
        elif parsed is None:
            failure_kind = ERROR_TRUNCATION if looks_truncated(usage, 2500) else ERROR_PARSE
        else:
            schema = validate_rows(
                parsed,
                list_key="evaluations",
                required_fields=REQUIRED_FIELDS,
                expected_tickers=selected_tickers,
                field_specs=FIELD_SPECS,
            )
            failure_kind = None if schema.ok else ERROR_SCHEMA
            rows = schema.rows if schema.ok else []
        status = "ok" if failure_kind is None else "error"
        logged = _log_adapter_usage(
            role=role, result=result, started=started,
            prompt_chars=len(SYSTEM_PROMPT) + len(user_prompt),
            max_tokens=2500, candidate_count=len(heated),
            status=status, failure_kind=failure_kind,
            run_id=_run_id, batch_id=batch_id,
        )
        accounting_logged_count += 1 if logged else 0
        call = {
            "batch_id": batch_id,
            "role": role,
            "tickers": selected_tickers,
            "status": status,
            "failure_kind": failure_kind,
            "adapter": result.get("adapter"),
            "model": result.get("model"),
            "usage": usage or None,
            "error": str(error)[:500] if error else None,
        }
        calls.append(call)
        return result, rows

    res, evaluations = _run("social_topic_deepdive", f"{_run_id}#b1")
    fallback_status = "not_attempted"
    if not evaluations:
        first_failure = calls[-1].get("error") or calls[-1].get("failure_kind") or "parse error"
        print(f"[social_topic] DeepSeek failed ({first_failure}); fallback to Qwen")
        res, evaluations = _run("social_topic_fallback", f"{_run_id}#b1/fb")
        fallback_status = "used"

    run_status = RUN_STATUS_SUCCESS if evaluations else RUN_STATUS_FAILED
    accounting_incomplete = accounting_logged_count != len(calls)
    if accounting_incomplete and run_status == RUN_STATUS_SUCCESS:
        run_status = RUN_STATUS_PARTIAL
    error_code = None if run_status == RUN_STATUS_SUCCESS else (
        "accounting_incomplete" if accounting_incomplete
        else next((c.get("failure_kind") for c in reversed(calls) if c.get("failure_kind")), "transport_error")
    )
    record = build_run_record(
        lane=LANE, run_id=_run_id, run_status=run_status,
        started_at=_started_at, input_count=input_count,
        selected_count=len(heated), success_count=1 if evaluations else 0,
        batches=calls, batch_count=1, source_as_of=source_as_of,
        input_state=input_state, error_code=error_code,
        fallback_status=fallback_status, call_count=len(calls),
        output_tokens=sum(int((c.get("usage") or {}).get("completion_tokens") or 0) for c in calls),
        selected_tickers=selected_tickers,
    )
    record["accounting_logged_count"] = accounting_logged_count
    record["accounting_incomplete"] = accounting_incomplete
    return _finish(record, evaluations, meta={
        "tickers_evaluated": selected_tickers,
        "adapter": res.get("adapter"),
        "model": res.get("model"),
        "usage": res.get("usage"),
    })


def analyze(dry_run: bool = False) -> dict:
    """Serialize the cost-incurring lane across cron and manual callers."""
    try:
        with process_lock("social_topic_analysis", timeout=0):
            return _analyze_unlocked(dry_run=dry_run)
    except LockBusy:
        return {
            "schema_version": "topic_lane_v1",
            "lane": LANE,
            "run_status": "already_running",
            "write_suppressed": True,
            "evaluations": [],
        }


def format_for_prompt(max_entries: int = 8) -> str:
    """Opus 合成プロンプトに差し込むコンテキスト文字列を返す。

    ⚠️ 成功・スキーマ有効・鮮度内の 3 条件を満たさなければ空へ倒す
    (fail-closed)。以前は generated_at を一切見ておらず、合成した 2020 年の
    日付を持つファイルでも非空のプロンプトを返した。このレーンは現在
    候補 0 件で LLM を呼んでいない (選抜条件が到達不能だったため) が、
    将来非空の結果を出し始めたあとに cron が止まると、古い SNS 判断が
    期限なく再利用されるため、先に契約だけ入れておく。
    """
    from topic_lane_contract import load_and_gate

    data, reason = load_and_gate(
        OUTPUT_FILE,
        source=FRESHNESS_SOURCE,
        row_key="evaluations",
        required_fields=REQUIRED_FIELDS,
        field_specs=FIELD_SPECS,
    )
    if data is None:
        if reason not in ("file not found",):
            print(f"[social_topic] context suppressed ({reason})", file=sys.stderr)
        return ""
    evals = (data.get("evaluations") or [])[:max_entries]
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

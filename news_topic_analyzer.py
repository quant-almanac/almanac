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
    - 小バッチ (既定 3 銘柄 / 4000 トークン) へ分割し、出力量を上限内に収める
      (5 銘柄 / 3000 トークンでも 4 バッチ中 2 バッチが切れることを実測)
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
    FAILING_INPUT_STATES,
    INPUT_EMPTY,
    INPUT_MISSING,
    INPUT_OK,
    INPUT_STALE,
    INPUT_UNREADABLE,
    RUN_STATUS_FAILED,
    RUN_STATUS_NO_CANDIDATES,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_SUCCESS,
    RUN_STATUS_UNAVAILABLE,
    RunBudget,
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
# 成果物 (news_topic_analysis.json) の鮮度契約: 週末越しの再利用を許す 72h。
FRESHNESS_SOURCE = "news_topic"
# 上流 (news_signal_candidates.json) の鮮度契約: analysis_snapshot.py が
# 同じファイルへ課しているのと同じ 12h。両者は別の契約なので混同しない。
UPSTREAM_FRESHNESS_SOURCE = "news"

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

# ⚠️ バッチサイズと上限トークン。20 銘柄一括は必ず切れていたが、
# 5 銘柄 / 3000 トークンでも足りないことが隔離ライブの実測で判明した:
#   b1 2773 / b2 3000(切れ) / b3 3000(切れ) / b4 1861
# 1 銘柄あたりの出力量は理由文の長さでかなりばらつく (370〜600+)。
# 実測の最悪値に対して倍の余裕を取り、3 銘柄 / 4000 トークンにする。
# max_tokens は「上限」であって課金対象は実出力なので、広く取っても
# コストは増えない一方、切れると 1 バッチ丸ごと無駄になる。
BATCH_SIZE = int(os.environ.get("NEWS_TOPIC_BATCH_SIZE", "3") or 3)
MAX_TOKENS_PER_BATCH = int(os.environ.get("NEWS_TOPIC_MAX_TOKENS", "4000") or 4000)

# ⚠️ Qwen fallback は既定で無効。OpenRouter の残高切れ (402) が続いており、
# 有効にすると毎バッチで確実に失敗する呼び出しを 1 回ずつ足すだけになる。
# 残高を補充したら NEWS_TOPIC_FALLBACK=1 で手動復帰させる (毎日の事前 probe は
# それ自体が無駄なので行わない)。
FALLBACK_ENABLED = (os.environ.get("NEWS_TOPIC_FALLBACK", "0") or "0").lower() in ("1", "true", "yes")

# スキーマ検証で必須にする項目 (6件)。
# ⚠️ ripple_tickers は **任意** — 波及先が思い当たらない材料では空が自然で、
# 必須にすると正当な分析まで落ちる。ただし返ってきた場合は FIELD_SPECS で
# 型と要素数を検証する (「7フィールドを検証」という以前の説明は不正確だった)。
REQUIRED_FIELDS = ("ticker", "catalyst_type", "durability", "impact_magnitude",
                   "hold_horizon_days", "one_liner")

_CATALYST_TYPES = frozenset({
    "earnings", "guidance", "product", "macro", "regulatory",
    "m_and_a", "people", "litigation", "tech", "unknown",
})
_DURABILITIES = frozenset({"short", "medium", "long"})

# ⚠️ 「項目が存在するか」だけでは足りない。実測で catalyst_type="BOGUS"・
# impact_magnitude=9999 が素通りしていた (レビューで指摘)。型・enum・
# 数値範囲まで検証する。
FIELD_SPECS = {
    "ticker": (str, lambda v: bool(str(v).strip())),
    "catalyst_type": (str, lambda v: str(v).lower() in _CATALYST_TYPES),
    "durability": (str, lambda v: str(v).lower() in _DURABILITIES),
    "impact_magnitude": ((int, float), lambda v: not isinstance(v, bool)
                         and 0 <= float(v) <= 100),
    # 日数は整数のみ。bool は int のサブクラスなので明示的に除外する。
    "hold_horizon_days": (int, lambda v: not isinstance(v, bool) and 0 < v <= 400),
    # 200 は hard safety cap。プロンプトの「50文字以内」は soft target で、
    # 超過だけでバッチ全体を落とさない (表示側で短縮する)。
    "one_liner": (str, lambda v: 0 < len(str(v)) <= 200),
    "ripple_tickers": (list, lambda v: len(v) <= 10
                       and all(isinstance(x, str) for x in v)),
}

# run 全体の費用上限 (分割 retry があるので停止するだけでは足りない)
RUN_BUDGET = RunBudget(
    max_calls=int(os.environ.get("NEWS_TOPIC_MAX_CALLS", "20") or 20),
    max_output_tokens=int(os.environ.get("NEWS_TOPIC_MAX_TOTAL_TOKENS", "60000") or 60000),
    max_elapsed_sec=float(os.environ.get("NEWS_TOPIC_MAX_SEC", "600") or 600),
)


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
) -> bool:
    """会計ログへ 1 行記録し、書けたかどうかを返す。

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
    # ⚠️ 戻り値を捨てない。以前は書き込み失敗が完全に不可視で、
    # バッチは status=ok のまま stderr も空だった (レビューで実測)。
    # 「call_count == 会計行数」は通常時の観測結果であって不変条件ではない。
    written = _append_llm_call_log(row)
    if not written:
        print(f"[news_topic] accounting row lost, dumping to stderr: "
              f"{json.dumps(row, ensure_ascii=False)}", file=sys.stderr)
    return written


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


def _load_candidates() -> tuple[list[dict[str, Any]], int, str | None, str]:
    """(選別済み候補, 入力総数, 入力の as_of, 入力状態) を返す。

    ⚠️ 「候補 0 件」と「ファイルが無い / 壊れている / 古い」を区別する。
    以前は全部 ([], 0, None) に潰しており、入力ファイルが消えていても
    run_status=no_candidates / error_code=None / heartbeat=ok となって、
    外から見て完全に正常だった (レビューで指摘・実測)。
    """
    if not CANDIDATES_FILE.exists():
        print(f"[news_topic] {CANDIDATES_FILE.name} not found", file=sys.stderr)
        return [], 0, None, INPUT_MISSING
    try:
        data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[news_topic] failed to parse candidates JSON: {e}", file=sys.stderr)
        return [], 0, None, INPUT_UNREADABLE
    if not isinstance(data, dict):
        print("[news_topic] candidates JSON is not an object", file=sys.stderr)
        return [], 0, None, INPUT_UNREADABLE

    cands = data.get("candidates", [])
    source_as_of = data.get("generated_at") or data.get("as_of")

    # ⚠️ 構造検証を選別より前に置く。JSON として読めても中身が想定外だと
    # 選別中に AttributeError で落ち、heartbeat も run 記録も残らないまま
    # プロセスごと終わる (レビューで実測: candidates が dict、要素が str の
    # どちらでも 'str' object has no attribute 'get' でクラッシュした)。
    if not isinstance(cands, list):
        print(f"[news_topic] 'candidates' is {type(cands).__name__}, not a list",
              file=sys.stderr)
        return [], 0, source_as_of, INPUT_UNREADABLE
    for row in cands:
        if not isinstance(row, dict):
            print(f"[news_topic] a candidate is {type(row).__name__}, not an object",
                  file=sys.stderr)
            return [], len(cands), source_as_of, INPUT_UNREADABLE
        if not isinstance(row.get("ticker"), str) or not row.get("ticker").strip():
            print("[news_topic] a candidate has no usable ticker", file=sys.stderr)
            return [], len(cands), source_as_of, INPUT_UNREADABLE
        score = row.get("sentiment_score")
        # bool は int のサブクラスなので明示的に除外する。
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            print(f"[news_topic] {row.get('ticker')}: sentiment_score is "
                  f"{type(score).__name__}", file=sys.stderr)
            return [], len(cands), source_as_of, INPUT_UNREADABLE

    # ⚠️ 上流の鮮度は「上流自身の契約」で測る。以前は成果物側の 72h
    # (news_topic) を流用しており、24時間古いニュース入力が ok で通っていた
    # (レビューで指摘・実測)。analysis_snapshot.py は同じ入力を news=12h で
    # 失効させているので、そこに合わせる。
    #   news        (12h): news_signal_candidates.json 自体の鮮度
    #   news_topic  (72h): 生成済み topic 出力を週末越しに再利用する期限
    from freshness_policy import stale_after_hours
    upstream_limit = stale_after_hours(UPSTREAM_FRESHNESS_SOURCE)
    if not source_as_of:
        # 鮮度を確認できない入力を「新鮮」とはみなさない。
        print("[news_topic] upstream has no generated_at; cannot verify freshness",
              file=sys.stderr)
        return [], len(cands), None, INPUT_UNREADABLE
    parsed = _parse_source_ts(source_as_of)
    if parsed is None:
        print(f"[news_topic] unparseable upstream as_of: {source_as_of!r}",
              file=sys.stderr)
        return [], len(cands), source_as_of, INPUT_UNREADABLE
    age_h = (time.time() - parsed) / 3600.0
    if age_h > upstream_limit:
        print(f"[news_topic] upstream is stale: {age_h:.1f}h > {upstream_limit}h",
              file=sys.stderr)
        return [], len(cands), source_as_of, INPUT_STALE

    filtered = [c for c in cands if abs(c.get("sentiment_score", 0)) >= SCORE_THRESHOLD]
    filtered.sort(key=lambda c: abs(c.get("sentiment_score", 0)), reverse=True)
    selected = filtered[:MAX_TICKERS]
    return selected, len(cands), source_as_of, (INPUT_OK if selected else INPUT_EMPTY)


def _parse_source_ts(value: object) -> float | None:
    """上流 as_of を epoch 秒へ。読めなければ None。"""
    from datetime import datetime as _dt
    text = str(value or "").strip()
    for parser in (
        _dt.fromisoformat,
        lambda s: _dt.strptime(s[:19], "%Y-%m-%d %H:%M:%S"),
        lambda s: _dt.strptime(s[:16], "%Y-%m-%d %H:%M"),
    ):
        try:
            return parser(text).timestamp()
        except (ValueError, TypeError):
            continue
    return None


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
                field_specs=FIELD_SPECS,
            )
            if not schema.ok:
                failure_kind = ERROR_SCHEMA
            else:
                rows = schema.rows

    status = "ok" if failure_kind is None else "error"
    accounting_logged = _log_adapter_usage(
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
        "accounting_logged": accounting_logged,
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
    selected, input_count, source_as_of, input_state = _load_candidates()

    def _finish(record: dict, analyses: list[dict]) -> dict:
        out = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "analyses": analyses,
            **record,
        }
        if not dry_run:
            OUTPUT_FILE.write_text(
                json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
            # ⚠️ 分母はバッチ数。selected_count (銘柄数) を使うと
            # 「2/20 batches ok」のように実際の 4 バッチと食い違う。
            print(f"[news_topic] run={run_id} status={record['run_status']} "
                  f"input={record['input_state']} analyses={len(analyses)} "
                  f"({record['successful_leaf_count']}/{record['leaf_batch_count']} "
                  f"leaves ok, {record['call_count']} calls)")
            write_heartbeat(LANE, record)
        return out

    # ⚠️ 入力の異常 (欠損・破損・上流stale) は「候補0件」ではなく失敗。
    # 以前は同じ扱いで、入力ファイルが消えていても heartbeat=ok だった。
    if input_state in FAILING_INPUT_STATES:
        return _finish(build_run_record(
            lane=LANE, run_id=run_id, run_status=RUN_STATUS_FAILED,
            started_at=started_at, input_count=input_count,
            selected_count=0, success_count=0, batches=[],
            source_as_of=source_as_of, input_state=input_state,
            error_code=f"input_{input_state}",
        ), [])

    if not selected:
        return _finish(build_run_record(
            lane=LANE, run_id=run_id, run_status=RUN_STATUS_NO_CANDIDATES,
            started_at=started_at, input_count=input_count,
            selected_count=0, success_count=0, batches=[],
            source_as_of=source_as_of, input_state=input_state,
        ), [])

    if call_by_role is None:
        return _finish(build_run_record(
            lane=LANE, run_id=run_id, run_status=RUN_STATUS_UNAVAILABLE,
            started_at=started_at, input_count=input_count,
            selected_count=len(selected), success_count=0, batches=[],
            source_as_of=source_as_of, input_state=input_state,
            error_code="llm_adapters unavailable",
        ), [])

    batches = _chunk(selected, BATCH_SIZE)
    print(f"[news_topic] run={run_id}: {len(selected)} tickers "
          f"in {len(batches)} batch(es) of <={BATCH_SIZE} via DeepSeek")

    results: list[dict] = []
    analyses: list[dict] = []
    circuit_open = False       # quota を掴んだら以降の呼び出しを止める
    error_code: str | None = None
    fallback_status = "disabled" if not FALLBACK_ENABLED else "not_attempted"

    # ⚠️ truncation は分割して retry する。
    # 出力量は 1 バッチあたり 591〜4000+ トークンとばらつきが大きく (実測)、
    # 固定サイズだけでは時々どうしても上限へ張り付く。一方でこのレーンは
    # 「全バッチ成功時のみ注入」という契約なので、retry が無いと病的な
    # 1 バッチのせいで run 全体が永久に partial になり、結局一度も注入
    # されない —— 元の「毎日走るが何も生まない」状態を別の形で再現して
    # しまう。retry は失敗時にしか走らず、サイズ 1 まで割れば必ず止まる。
    pending: list[tuple[str, list]] = [
        (f"{run_id}#b{i}", b) for i, b in enumerate(batches, start=1)
    ]
    # ⚠️ 指標を分離する (レビューで指摘・実測: 実API呼出し3 / batch_count 2 /
    # batches配列 3 と三者三様だった)。
    #   call_count           : 実際に API を叩いた回数 = 会計ログの行数
    #   leaf_units           : 最終的に「これ以上割らない」と確定した単位の数
    #   ok_units             : そのうち成功した数
    #   retry_count          : 分割によって追加投入された単位の数
    #   skipped_count        : circuit/budget で API を叩かずに諦めた数
    leaf_units = 0
    ok_units = 0
    call_count = 0
    retry_count = 0
    skipped_count = 0
    output_tokens = 0
    accounting_logged_count = 0
    budget_stop: str | None = None

    while pending:
        batch_id, batch = pending.pop(0)

        # ⚠️ 分割 retry は必ず停止するが、停止することと費用が安全なことは別。
        # 20銘柄・3件バッチで全経路が1件まで割れる最悪ケースは呼び出し33回・
        # 要求トークン132,000 になる (レビューで指摘)。run 全体に上限を置き、
        # 超えたら残りを叩かずに fail-closed で partial/failed へ倒す。
        if budget_stop is None and not circuit_open:
            budget_stop = RUN_BUDGET.exceeded(
                calls=call_count, output_tokens=output_tokens,
                started_at=started_at)
            if budget_stop:
                print(f"[news_topic] budget stop: {budget_stop}", file=sys.stderr)

        if circuit_open or budget_stop:
            skipped_count += 1
            leaf_units += 1
            reason = ("quota circuit breaker open" if circuit_open
                      else f"run budget: {budget_stop}")
            results.append({
                "batch_id": batch_id, "role": "news_topic_deepdive",
                "tickers": [c.get("ticker") for c in batch],
                "status": "skipped",
                "failure_kind": ERROR_QUOTA if circuit_open else "budget_exhausted",
                "rows": [], "error": f"skipped: {reason}",
            })
            continue

        res = _run_one_batch(batch, role="news_topic_deepdive",
                             run_id=run_id, batch_id=batch_id)
        call_count += 1
        output_tokens += int((res.get("usage") or {}).get("completion_tokens") or 0)
        accounting_logged_count += 1 if res.get("accounting_logged") else 0

        if res["failure_kind"] == ERROR_QUOTA:
            # 残高切れは待っても直らない。同じ run 内で叩き続けない。
            circuit_open = True
            error_code = ERROR_QUOTA

        # truncation かつ 2 銘柄以上なら、半分に割って入れ直す。
        # サイズ 1 でなお切れるなら、その銘柄自体が病的なので通常の失敗として扱う。
        if (res["failure_kind"] == ERROR_TRUNCATION and len(batch) > 1
                and not circuit_open):
            mid = len(batch) // 2
            results.append(res)          # 監査のため失敗も残す (leaf には数えない)
            retry_count += 2
            pending.insert(0, (f"{batch_id}/s2", batch[mid:]))
            pending.insert(0, (f"{batch_id}/s1", batch[:mid]))
            continue

        # ⚠️ fallback の前にも予算を確認する。しないと max_calls=1 でも
        # 2 回叩けてしまい hard cap にならない (レビューで指摘)。
        fb_budget = RUN_BUDGET.exceeded(calls=call_count, output_tokens=output_tokens,
                                        started_at=started_at)
        if res["status"] != "ok" and FALLBACK_ENABLED and not circuit_open and not fb_budget:
            fb = _run_one_batch(batch, role="news_topic_fallback",
                                run_id=run_id, batch_id=f"{batch_id}/fb")
            call_count += 1
            output_tokens += int((fb.get("usage") or {}).get("completion_tokens") or 0)
            accounting_logged_count += 1 if fb.get("accounting_logged") else 0
            fallback_status = "used"
            if fb["failure_kind"] == ERROR_QUOTA:
                circuit_open = True
                error_code = ERROR_QUOTA
            if fb["status"] == "ok":
                res = fb
            else:
                results.append(fb)

        leaf_units += 1
        results.append(res)
        if res["status"] == "ok":
            ok_units += 1
            analyses.extend(res["rows"])

    ok_batches = ok_units
    total_batches = leaf_units
    if ok_batches == total_batches:
        run_status = RUN_STATUS_SUCCESS
    elif ok_batches > 0:
        run_status = RUN_STATUS_PARTIAL
    else:
        run_status = RUN_STATUS_FAILED

    # ⚠️ 最後に件数の総和で突き合わせる。バッチ単位で全部 ok でも、
    # 選抜した銘柄数と分析件数が一致しないなら「全銘柄を分析できた」とは
    # 言えない (レビューで指摘: 現在の「全バッチ schema 成功」は正確には
    # 「各バッチに最低 1 件の行があった」でしかなかった)。
    # validate_rows 側で全数一致を要求済みだが、分割や重複排除を経ても
    # 崩れないことを run 全体でもう一度確かめる二重の担保。
    if run_status == RUN_STATUS_SUCCESS and len(analyses) != len(selected):
        print(f"[news_topic] count mismatch: {len(analyses)} analyses "
              f"!= {len(selected)} selected", file=sys.stderr)
        run_status = RUN_STATUS_PARTIAL
        error_code = error_code or "analysis_count_mismatch"

    # ⚠️ 会計が取れていない run は、費用の裏付けが無いまま結論だけ残る。
    # 「call_count == 会計行数」は通常時の観測結果であって不変条件ではないので、
    # 実際に書けた本数を数えて突き合わせる (レビューで指摘・実測: 書き込みを
    # False にしてもバッチは status=ok のまま stderr も空だった)。
    accounting_incomplete = accounting_logged_count != call_count
    if accounting_incomplete:
        print(f"[news_topic] accounting incomplete: {accounting_logged_count}/"
              f"{call_count} rows written; run is not injectable", file=sys.stderr)
        if run_status == RUN_STATUS_SUCCESS:
            run_status = RUN_STATUS_PARTIAL
        error_code = error_code or "accounting_incomplete"

    if error_code is None and run_status != RUN_STATUS_SUCCESS:
        kinds = [r.get("failure_kind") for r in results if r.get("failure_kind")]
        error_code = kinds[0] if kinds else ERROR_TRANSPORT

    record = build_run_record(
        lane=LANE, run_id=run_id, run_status=run_status,
        started_at=started_at, input_count=input_count,
        selected_count=len(selected), success_count=ok_batches,
        batches=results, batch_count=total_batches, source_as_of=source_as_of,
        input_state=input_state, error_code=error_code,
        fallback_status=fallback_status, call_count=call_count,
        retry_count=retry_count, skipped_count=skipped_count,
        output_tokens=output_tokens, budget_stop=budget_stop,
    )
    record["accounting_logged_count"] = accounting_logged_count
    record["accounting_incomplete"] = accounting_incomplete
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
    data, reason = load_and_gate(
        OUTPUT_FILE, source=FRESHNESS_SOURCE,
        upstream_source=UPSTREAM_FRESHNESS_SOURCE,
        row_key="analyses", required_fields=REQUIRED_FIELDS,
        field_specs=FIELD_SPECS)
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
        # 50文字は soft target。超過はバッチを落とさず表示時に短縮する
        # (hard cap 200 は FIELD_SPECS 側)。
        one = str(a.get("one_liner", ""))
        if len(one) > 50:
            one = one[:50] + "…"
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

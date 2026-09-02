"""topic_lane_contract.py — news_topic / social_topic 共通の実行状態・鮮度契約。

背景 (2026-08-28 の監査):
両レーンとも「LLM API が 200 を返したか」だけを status に記録しており、
その後の JSON parse・スキーマ検証の成否を区別していなかった。実測では
news_topic の 44 回すべてが ``status=ok`` で記録されている一方、43 回は
parse に失敗して fallback へ進み、最終的な分析結果は 0 件だった。
「API 成功」と「機能成功」が混同されていたため、毎日コストだけ払って
何も生産していない状態が会計ログ上は正常に見えていた。

さらに両レーンの ``format_for_prompt()`` は ``generated_at`` を一切見て
いなかったため、何日前の出力でも最終分析へそのまま注入され得た。

このモジュールは両レーンが共有する:
  - 失敗の分類 (transport / quota / truncation / parse / schema)
  - 実行状態 (run_status) の語彙と必須フィールド
  - 「成功・スキーマ有効・鮮度内」の 3 条件を満たさなければ空へ倒す判定
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

SCHEMA_VERSION = "topic_lane_v1"

# ── run_status の語彙 ────────────────────────────────────────────────
# success        : 全バッチが parse + schema 検証まで通った
# partial        : 一部バッチのみ成功。監査用に保存するが判断入力には使わない
# failed         : 1件も成功しなかった
# no_candidates  : 入力が 0 件 (正常。LLM は呼ばない)
# unavailable    : LLM アダプタ自体が使えない
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_PARTIAL = "partial"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_NO_CANDIDATES = "no_candidates"
RUN_STATUS_UNAVAILABLE = "unavailable"

# ── 上流入力の状態 ──────────────────────────────────────────────────
# ⚠️ 「候補が 0 件だった」と「入力ファイルが無い / 壊れている / 古い」を
# 同じ扱いにしてはいけない。以前は全部 ([], 0, None) に潰しており、
# 入力ファイルが消えていても run_status=no_candidates / error_code=None /
# heartbeat=ok となり、外から見て完全に正常だった (レビューで指摘・再現)。
INPUT_OK = "ok"                 # 読めて候補もある
INPUT_EMPTY = "empty"           # 読めたが閾値を通る候補が無い (正常)
INPUT_MISSING = "missing"       # ファイルが無い (異常)
INPUT_UNREADABLE = "unreadable"  # JSON が壊れている (異常)
INPUT_STALE = "stale"           # 読めるが上流が古すぎる (異常)

# missing / unreadable / stale は「候補が無い」ではなく失敗として扱う。
FAILING_INPUT_STATES = frozenset({INPUT_MISSING, INPUT_UNREADABLE, INPUT_STALE})

# 判断入力への注入を許すのは success だけ。partial を許すと
# 「20銘柄中3銘柄だけ分析された結果」が全体の所見のように扱われる。
INJECTABLE_RUN_STATUSES = frozenset({RUN_STATUS_SUCCESS})

# ── 失敗の分類 ───────────────────────────────────────────────────────
ERROR_TRANSPORT = "transport_error"    # ネットワーク/API がエラーを返した
ERROR_QUOTA = "quota_error"            # 残高・レート制限 (402/429)。リトライ無意味
ERROR_TRUNCATION = "truncation"        # max_tokens に張り付いて出力が切れた
ERROR_PARSE = "parse_error"            # JSON として読めない
ERROR_SCHEMA = "schema_error"          # JSON だが期待する形をしていない

# 402 / insufficient credits / quota 系。これを掴んだら同一 run 内で
# その adapter への追加呼び出しを止める (circuit breaker)。
_QUOTA_RE = re.compile(
    r"\b402\b|insufficient|more credits|can only afford|quota|rate.?limit|\b429\b",
    re.IGNORECASE,
)

# Some adapters return a structured stop reason through their ``error`` field
# instead of returning truncated content plus usage.  Treat those explicit
# output-limit signals as truncation so the caller can split the batch.  Quota
# detection must run first because credit errors can also mention max_tokens.
_TRUNCATION_ERROR_RE = re.compile(
    r"stop_reason\s*[=:]\s*['\"]?max_tokens|"
    r"finish_reason\s*[=:]\s*['\"]?(?:length|max_tokens)",
    re.IGNORECASE,
)


def classify_error(error: object) -> str:
    """アダプタが返した error 文字列を分類する。"""
    text = str(error or "")
    if _QUOTA_RE.search(text):
        return ERROR_QUOTA
    if _TRUNCATION_ERROR_RE.search(text):
        return ERROR_TRUNCATION
    return ERROR_TRANSPORT


def is_quota_error(error: object) -> bool:
    return classify_error(error) == ERROR_QUOTA


def looks_truncated(usage: Optional[dict], max_tokens: int) -> bool:
    """出力が max_tokens に張り付いているか。

    実測では news_topic の 44 回中 43 回が完了トークン 2999-3000 で、
    max_tokens=3000 に対して切れていた。切れた JSON は閉じ括弧を持たない
    ため、後段の抽出正規表現 ``\\{[\\s\\S]*\\}`` が構造的にマッチしない。
    """
    if not usage or not max_tokens:
        return False
    out = usage.get("completion_tokens") or usage.get("output_tokens")
    try:
        out = int(out)
    except (TypeError, ValueError):
        return False
    # 1 トークンの余裕を見る (アダプタによって数え方が 1 ずれる)
    return out >= int(max_tokens) - 1


# ── JSON 抽出 ────────────────────────────────────────────────────────
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def extract_json(text: str) -> Optional[dict]:
    """LLM 応答から JSON オブジェクトを取り出す。

    ⚠️ truncation は救わない。以前の実装は「末尾に ``}`` を足して再試行」
    していたが、正規表現が閉じ括弧を要求する以上、切れた出力はそもそも
    マッチせず再試行に到達しない。実測でも不完全 JSON・末尾カンマとも
    None になった。ここで無理に復元しようとすると「半分だけ読めた分析」が
    正常な結果として流れるので、読めないものは読めないままにする。
    truncation の検出は looks_truncated() が usage を見て別に行う。
    """
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    match = _JSON_BLOCK_RE.search(stripped)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


# ── スキーマ検証 ─────────────────────────────────────────────────────
@dataclass
class SchemaResult:
    ok: bool
    rows: list[dict] = field(default_factory=list)
    reason: str = ""


def validate_rows(
    parsed: Optional[dict],
    *,
    list_key: str,
    required_fields: Iterable[str],
    expected_tickers: Optional[Iterable[str]] = None,
    field_specs: Optional[dict] = None,
    require_full_coverage: bool = True,
) -> SchemaResult:
    """parse 済み JSON が期待する形をしているか検証する。

    「JSON として読めた」と「使える分析が入っている」は別物。ここを通って
    初めて status=ok を名乗れる。

    ⚠️ 「1 行でも正しければ成功」にしない (レビューで指摘・実測: 期待 2 銘柄に
    対し 1 銘柄だけ返した応答が ok=True で通り、バッチ成功として計上されて
    いた)。その状態で「全バッチ schema 成功」と言っても、実際には
    「各バッチに最低 1 件の最低限の行があった」でしかない。
    require_full_coverage=True なら、期待した銘柄集合と返却された銘柄集合が
    完全一致することを求める。

    field_specs は {field: (type_or_types, validator_or_None)} で、型と
    enum・数値範囲まで検証する。必須項目を「存在するか」だけで見ていると、
    catalyst_type="BOGUS" や impact_magnitude=9999 が素通りする (これも実測)。
    """
    if not isinstance(parsed, dict):
        return SchemaResult(False, [], "not a JSON object")
    rows = parsed.get(list_key)
    if not isinstance(rows, list):
        return SchemaResult(False, [], f"'{list_key}' is not a list")
    if not rows:
        return SchemaResult(False, [], f"'{list_key}' is empty")

    required = list(required_fields)
    allowed = {str(t).upper() for t in expected_tickers} if expected_tickers else None
    specs = field_specs or {}

    good: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return SchemaResult(False, [], "a row is not an object")
        missing = [f for f in required if row.get(f) in (None, "")]
        if missing:
            return SchemaResult(False, [], f"row missing {missing}")

        ticker = str(row.get("ticker", "")).upper()
        if allowed is not None and ticker not in allowed:
            # プロンプトに無い銘柄を LLM が創作していないか。scope 外の
            # 銘柄が最終分析へ混ざるのを防ぐ。
            return SchemaResult(False, [], f"ticker outside the prompt: {ticker}")
        if ticker in seen:
            # 同じ銘柄を 2 行返されると、後段の件数一致チェックが
            # 「全銘柄そろった」と誤認しうる。
            return SchemaResult(False, [], f"duplicate ticker: {ticker}")
        seen.add(ticker)

        for field_name, spec in specs.items():
            value = row.get(field_name)
            if value is None:
                if field_name in required:
                    return SchemaResult(False, [], f"{ticker}: {field_name} is null")
                continue
            expected_type, validator = spec
            if expected_type is not None and not isinstance(value, expected_type):
                return SchemaResult(
                    False, [],
                    f"{ticker}: {field_name} has type {type(value).__name__}")
            if validator is not None and not validator(value):
                return SchemaResult(
                    False, [], f"{ticker}: {field_name}={value!r} is out of contract")
        good.append(row)

    if allowed is not None and require_full_coverage and seen != allowed:
        missing = sorted(allowed - seen)
        return SchemaResult(False, [], f"missing tickers: {missing}")

    if not good:
        return SchemaResult(False, [], "no row satisfied the required fields")
    return SchemaResult(True, good, "")


# ── 実行状態レコード ─────────────────────────────────────────────────
def build_run_record(
    *,
    lane: str,
    run_id: str,
    run_status: str,
    started_at: float,
    input_count: int,
    selected_count: int,
    success_count: int,
    batches: list[dict],
    batch_count: Optional[int] = None,
    source_as_of: Optional[str] = None,
    input_state: str = INPUT_OK,
    error_code: Optional[str] = None,
    fallback_status: str = "not_attempted",
    call_count: int = 0,
    retry_count: int = 0,
    skipped_count: int = 0,
    output_tokens: int = 0,
    budget_stop: Optional[str] = None,
    selected_tickers: Optional[list] = None,
) -> dict:
    """両レーン共通の実行状態。監視・監査はこの形だけを見ればよい。"""
    now = time.time()
    return {
        "schema_version": SCHEMA_VERSION,
        "lane": lane,
        "run_id": run_id,
        "run_status": run_status,
        "source_as_of": source_as_of,
        "started_at": _iso(started_at),
        "completed_at": _iso(now),
        "elapsed_sec": round(now - started_at, 2),
        "input_count": input_count,
        "input_state": input_state,
        "selected_count": selected_count,
        "success_count": success_count,
        # ⚠️ batches は fallback が失敗すると 1 バッチにつき 2 エントリ持つので、
        # 「成功/全体」の分母には使えない。実際に投げたバッチ数を別に持つ。
        "batch_count": len(batches) if batch_count is None else int(batch_count),
        # ⚠️ 指標を分離する (レビューで指摘・実測: 実 API 呼出し 3 / batch_count 2 /
        # batches 配列 3 と三者三様だった)。会計ログの行数と一致すべきなのは
        # call_count —— 分割 retry で切れた親バッチも会計には残るが、
        # leaf の成功数には入らない。
        "call_count": int(call_count),
        "leaf_batch_count": len(batches) if batch_count is None else int(batch_count),
        "successful_leaf_count": int(success_count),
        "retry_count": int(retry_count),
        "skipped_count": int(skipped_count),
        "output_tokens": int(output_tokens),
        "budget_stop": budget_stop,
        "fallback_status": fallback_status,
        "error_code": error_code,
        "batches": batches,
        # consumer 側 (injection_gate) が、保存後に ticker がすり替わって
        # いないかを突き合わせるための選抜時点の集合。
        "selected_tickers": list(selected_tickers) if selected_tickers else None,
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


# ── run 全体の費用上限 ───────────────────────────────────────────────
@dataclass
class RunBudget:
    """run 単位の呼び出し・トークン・時間の上限。

    ⚠️ 分割 retry は必ず停止するが、「停止すること」と「費用が安全なこと」は
    別 (レビューで指摘)。20 銘柄・3 件バッチで全経路が 1 件まで割れる最悪
    ケースは呼び出し 33 回・要求トークン上限 132,000 になる。実際にそこまで
    行くことは稀でも、上限が無いままにはしない。超過したら fail-closed で
    partial/failed に倒す。
    """
    max_calls: int = 20
    max_output_tokens: int = 60_000
    max_elapsed_sec: float = 600.0

    def exceeded(self, *, calls: int, output_tokens: int,
                 started_at: float) -> Optional[str]:
        if calls >= self.max_calls:
            return f"call budget exhausted ({calls} >= {self.max_calls})"
        if output_tokens >= self.max_output_tokens:
            return (f"output-token budget exhausted "
                    f"({output_tokens} >= {self.max_output_tokens})")
        elapsed = time.time() - started_at
        if elapsed >= self.max_elapsed_sec:
            return f"time budget exhausted ({elapsed:.0f}s >= {self.max_elapsed_sec}s)"
        return None


# ── 注入可否 (fail-closed) ───────────────────────────────────────────
def injection_gate(
    data: Optional[dict],
    *,
    source: str,
    upstream_source: Optional[str] = None,
    row_key: str = "analyses",
    required_fields: Optional[Iterable[str]] = None,
    field_specs: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """最終分析へ注入してよいかを判定する。

    3条件すべてを満たしたときだけ True:
      1. run_status が success (partial は監査用であって判断入力ではない)
      2. スキーマ検証を通った行が 1 件以上ある
      3. generated_at が freshness_policy の stale 閾値内

    どれか欠ければ空へ倒す (fail-closed)。以前は3つとも見ておらず、
    合成した 2020 年の日付を持つファイルでも非空のプロンプトが返った。
    """
    if not isinstance(data, dict):
        return False, "no data"

    status = data.get("run_status")
    if status is None:
        # 契約導入前に書かれた古い成果物。成功と断定できないので通さない。
        return False, "missing run_status (pre-contract artifact)"
    if status not in INJECTABLE_RUN_STATUSES:
        return False, f"run_status={status}"

    generated_at = data.get("generated_at") or data.get("completed_at")
    if not generated_at:
        return False, "missing generated_at"

    parsed_at = _parse_ts(generated_at)
    if parsed_at is None:
        return False, f"unparseable generated_at: {generated_at!r}"

    from freshness_policy import stale_after_hours

    limit = stale_after_hours(source)
    reference = now or datetime.now()
    age_h = (reference - parsed_at).total_seconds() / 3600.0
    # 未来の timestamp は「新しすぎる」ではなく「壊れている」として扱う
    # (dashboard.py / analysis_snapshot.py と同じ 1 時間の許容幅)。
    if age_h < -1.0:
        return False, f"generated_at is in the future ({age_h:.1f}h)"
    if age_h > limit:
        return False, f"stale: {age_h:.1f}h > {limit}h"

    # ⚠️ 自分の generated_at が新しいだけでは足りない。分析した「元データ」が
    # 古ければ、古いニュースを今日読み直しただけの結果を fresh として
    # 注入することになる (レビューで実測: source_as_of=30日前 / generated_at=現在
    # の成果物が injection_gate=True で通っていた)。
    # 上流の鮮度は consumer の現在時刻ではなく、その run が走った時刻
    # (started_at) との差で測る —— 実行後に時間が経ったことと、実行時点で
    # 既に古い入力を読んでいたことは別の問題。
    # ⚠️ 上流には上流自身の契約を使う。成果物側の限界 (72h) を流用すると、
    # 24時間古いニュース入力が通ってしまう (レビューで指摘・実測)。
    upstream_limit = stale_after_hours(upstream_source) if upstream_source else limit
    source_as_of = data.get("source_as_of")
    if source_as_of:
        source_dt = _parse_ts(source_as_of)
        if source_dt is None:
            return False, f"unparseable source_as_of: {source_as_of!r}"
        run_started = _parse_ts(data.get("started_at")) or parsed_at
        source_age_h = (run_started - source_dt).total_seconds() / 3600.0
        if source_age_h < -1.0:
            return False, f"source_as_of is in the future ({source_age_h:.1f}h)"
        if source_age_h > upstream_limit:
            return False, (f"upstream stale at run time: "
                           f"source_as_of was {source_age_h:.1f}h old > {upstream_limit}h")

    # ⚠️ run_status を信用するだけでは足りない。保存後にファイルが書き換わって
    # いても gate は通ってしまう (レビューで実測: catalyst_type="BOGUS" へ
    # 書き換えた成果物が通過した)。consumer 側でも行スキーマを再検証する。
    if required_fields:
        rows = data.get(row_key)
        schema = validate_rows(
            {row_key: rows} if rows is not None else None,
            list_key=row_key,
            required_fields=required_fields,
            field_specs=field_specs,
            require_full_coverage=False,   # 期待 ticker 集合は producer 側の契約
        )
        if not schema.ok:
            return False, f"stored rows fail schema: {schema.reason}"

        # ⚠️ 監査メタデータは「あれば検証する」ではなく「無ければ拒否する」。
        # 以前は各フィールドを個別に optional 扱いしており、レビューで
        # source_as_of 欠損・started_at 欠損・selected_count 欠損・会計3項目
        # 全欠損・call_count と accounting_logged_count の不一致・ticker の
        # すり替え、のいずれも injection_gate=True で通ることが実測された。
        # producer が正しく保存しても、保存後の欠損・部分書換えを検出できて
        # いなかった。ここでは全項目の「存在」まで含めて必須にする。
        if data.get("schema_version") != SCHEMA_VERSION:
            return False, f"schema_version mismatch: {data.get('schema_version')!r}"
        if not data.get("source_as_of"):
            return False, "missing source_as_of"
        if not data.get("started_at"):
            return False, "missing started_at"

        selected = data.get("selected_count")
        if isinstance(selected, bool) or not isinstance(selected, int) or selected < 0:
            return False, f"selected_count missing or invalid: {selected!r}"
        if len(schema.rows) != selected:
            return False, f"row count {len(schema.rows)} != selected_count {selected}"

        call_count = data.get("call_count")
        logged_count = data.get("accounting_logged_count")
        if call_count is None or logged_count is None:
            return False, "missing call_count/accounting_logged_count"
        if call_count != logged_count:
            return False, (f"call_count {call_count} != "
                           f"accounting_logged_count {logged_count}")
        # ⚠️ 「truthy でなければ拒否」ではなく「明示的に False でなければ拒否」。
        # フィールド自体が欠損している run (accounting_incomplete が無い) も
        # False と区別がつかず fail-open していた。
        if data.get("accounting_incomplete") is not False:
            return False, (f"accounting_incomplete is not False: "
                           f"{data.get('accounting_incomplete')!r}")

        # ⚠️ スキーマが正しくても、行の ticker が producer の選抜した集合と
        # 一致するとは限らない (レビューで実測: 正しい形式のまま ticker だけ
        # 別銘柄へ差し替えた成果物が通過した)。選抜時点の ticker 集合を
        # producer 側で保存させ、ここで突き合わせる。
        expected_tickers = data.get("selected_tickers")
        if not isinstance(expected_tickers, list) or not expected_tickers:
            return False, "missing selected_tickers"
        got = {str(r.get("ticker", "")).upper() for r in schema.rows}
        want = {str(t).upper() for t in expected_tickers}
        if got != want:
            return False, f"analysed tickers {sorted(got)} != selected_tickers {sorted(want)}"

    return True, ""


def _parse_ts(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for parser in (
        datetime.fromisoformat,
        lambda s: datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S"),
        lambda s: datetime.strptime(s[:16], "%Y-%m-%d %H:%M"),
    ):
        try:
            dt = parser(text)
        except (ValueError, TypeError):
            continue
        return dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone().replace(tzinfo=None)
    return None


def load_and_gate(path: Path, *, source: str, now: Optional[datetime] = None,
                  **gate_kwargs) -> tuple[Optional[dict], str]:
    """成果物を読み、注入可否まで判定して返す。"""
    if not Path(path).exists():
        return None, "file not found"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"unreadable: {exc}"
    ok, reason = injection_gate(data, source=source, now=now, **gate_kwargs)
    return (data if ok else None), reason


def write_heartbeat(lane: str, record: dict) -> None:
    """0 件・失敗でも必ず生存シグナルを残す。

    以前は候補 0 件のとき何も print せず戻っていたため、cron ログが
    0 バイトのままになり「動いていない」と「動いたが 0 件」を外から
    区別できなかった (social_topic は 4 ヶ月これに該当していた)。
    """
    try:
        from utils import heartbeat
    except Exception:
        return
    run_status = record.get("run_status")
    # 0 件 (no_candidates) は異常ではないので ok。partial/failed は warn/error。
    if run_status in (RUN_STATUS_SUCCESS, RUN_STATUS_NO_CANDIDATES):
        hb_status = "ok"
    elif run_status == RUN_STATUS_PARTIAL:
        hb_status = "warn"
    else:
        hb_status = "error"
    try:
        heartbeat(lane, status=hb_status, error=record.get("error_code"), extra={
            "run_status": run_status,
            "success_count": record.get("success_count"),
            "selected_count": record.get("selected_count"),
            "error_code": record.get("error_code"),
        })
    except Exception:
        # heartbeat の失敗で本処理を落とさない。ただし握り潰しの跡は残す。
        pass

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
from typing import Any, Iterable, Optional

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


def classify_error(error: object) -> str:
    """アダプタが返した error 文字列を分類する。"""
    text = str(error or "")
    if _QUOTA_RE.search(text):
        return ERROR_QUOTA
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
) -> SchemaResult:
    """parse 済み JSON が期待する形をしているか検証する。

    「JSON として読めた」と「使える分析が入っている」は別物。ここを通って
    初めて status=ok を名乗れる。
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
    good: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if any(row.get(f) in (None, "") for f in required):
            continue
        if allowed is not None:
            ticker = str(row.get("ticker", "")).upper()
            # プロンプトに無い銘柄を LLM が創作していないか。scope 外の
            # 銘柄が最終分析へ混ざるのを防ぐ。
            if ticker not in allowed:
                continue
        good.append(row)

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
    source_as_of: Optional[str] = None,
    error_code: Optional[str] = None,
    fallback_status: str = "not_attempted",
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
        "selected_count": selected_count,
        "success_count": success_count,
        "fallback_status": fallback_status,
        "error_code": error_code,
        "batches": batches,
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


# ── 注入可否 (fail-closed) ───────────────────────────────────────────
def injection_gate(
    data: Optional[dict],
    *,
    source: str,
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


def load_and_gate(path: Path, *, source: str, now: Optional[datetime] = None) -> tuple[Optional[dict], str]:
    """成果物を読み、注入可否まで判定して返す。"""
    if not Path(path).exists():
        return None, "file not found"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"unreadable: {exc}"
    ok, reason = injection_gate(data, source=source, now=now)
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

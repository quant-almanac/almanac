"""DeepSeek-driven feature extractor for public disclosures / news.

Phase 0 of the ALMANAC public-disclosure feature pipeline plan.

This is the "調査員" (investigator), not a trader: it reads ONE public
disclosure or news item and turns "何が変わったか" into numeric, evidence-backed
features. It sends only public text to a cheap model (DeepSeek V4 Flash) through
the :mod:`almanac.llm_safety` choke-point, then stores the result as an
``observe_only`` row in ``data/disclosure_features.jsonl``.

Two disciplines from the plan are honored here:

- **Public-only.** The model is given the disclosure text and (optionally) a
  prior-period excerpt and a price-reaction summary — never the book. The call
  goes through ``call_external_llm`` with a ``public_disclosure`` /
  ``public_news`` payload, so any accidental book token fail-closes.
- **Null over guess.** Context features whose point-in-time inputs are not
  supplied (e.g. ``expectation_gap`` with no consensus, ``narrative_delta`` with
  no prior excerpt) are returned ``null`` rather than fabricated. The prompt is
  explicit about this and the extractor enforces it.

The extractor is transport-injectable so the parse → validate → store path is
unit-testable without the network (or any spend).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from almanac.llm_safety import Payload, call_external_llm
from almanac.observability.append_only_log import append_jsonl_safe
from almanac.observability.disclosure_features import (
    AI_CONTEXT_FEATURES,
    FEATURE_SCHEMA_VERSION,
    MVP_CORE_FEATURES,
    append_feature,
    make_feature,
)
from almanac.observability.ids import compute_source_event_id, new_row_id
from llm_cost_accounting import estimate_cost_usd

__all__ = ["PROMPT_VERSION", "extract_features", "build_prompt"]

# Bump whenever the prompt or feature semantics change. Stored on every row so
# the validation harness only ever compares like-for-like extractions.
PROMPT_VERSION = "ds-disclosure-0.2.0"

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_DEBUG_LOG_PATH = Path(__file__).resolve().parent / "logs" / "disclosure_extract_debug.jsonl"
_DEBUG_RESPONSE_CHARS = 2000

_ALL_FEATURE_NAMES = set(MVP_CORE_FEATURES) | set(AI_CONTEXT_FEATURES)
_UNIT = {
    "directional_confidence",
    "catalyst_specificity",
    "narrative_delta",
    "risk_emergence",
    "guidance_credibility",
    "crowding_hype_score",
    "non_obvious_negative",
}
_SIGNED = {"directional_score", "expectation_gap", "price_reaction_divergence"}


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM = (
    "あなたは開示資料・ニュースを読み、検証可能な数値特徴に変換する調査員です。売買判断はしない。"
    "与えられた公開情報だけで判断し、判断材料が無い特徴のみ推測せず null を返す。"
    "directional_score / catalyst_specificity / risk_emergence など『その開示自体の内容』から"
    "判断できる特徴は、前回資料が無くても本文（YoY/QoQ 等の比較や業績水準を含む）から読み取って値を返す。"
    "narrative_delta / expectation_gap / price_reaction_divergence は外部情報（前回資料・コンセンサス・"
    "開示後の値動き）が無い場合にのみ null。出力は厳密な JSON のみ。"
)

_FEATURE_SPEC = """\
抽出する特徴（提供された公開情報から判断できないものは null）:
- directional_score: その開示が示す業績/事業の方向性 [-1.0=強い弱気 .. +1.0=強い強気]。
  開示本文の内容（売上・利益の水準や YoY/QoQ 比較、ガイダンス、事業イベント等）から判断する。
  前回資料が無くても本文に方向性が読み取れれば値を返し、本当に方向が不明な時のみ null。
- directional_confidence: その確信度 [0.0 .. 1.0]
- catalyst_specificity: カタリストの具体性（数字・期日の明確さ）[0.0 .. 1.0]
- contradiction_count: 過去開示/同一資料内の矛盾の数（整数 >= 0）
- expectation_gap: 市場期待との差 [-1.0 .. +1.0]（コンセンサス情報が無ければ null）
- narrative_delta: 前回からの語り口の変化度 [0.0 .. 1.0]（前回資料が無ければ null）
- risk_emergence: 新たに前面化したリスクの強さ [0.0 .. 1.0]
- guidance_credibility: ガイダンスの整合性/信頼度 [0.0 .. 1.0]（ガイダンスが無ければ null）
- second_order_impact: 他の上場銘柄への波及 [{"ticker":"...","sign":-1|0|1}, ...]（無ければ []）
- crowding_hype_score: 既に語られすぎ/織り込み済み度 [0.0 .. 1.0]
- non_obvious_negative: 見出しは良いが中身に悪材料がある度合い [0.0 .. 1.0]
- price_reaction_divergence: 特徴の強さと開示後の値動きの乖離 [-1.0 .. +1.0]（値動き情報が無ければ null）
- summary: 「何が変わったか」を一文で
- evidence: [{"quote":"根拠となる原文の短い引用","claim":"その引用が支える主張"}, ...]
"""


def build_prompt(item: dict[str, Any]) -> tuple[str, str]:
    """Return ``(system, user)`` for ``item``. Public text only."""
    ticker = item.get("ticker", "")
    title = item.get("title", "") or ""
    body = item.get("body", "") or ""
    prior = item.get("prior_excerpt") or ""
    price_reaction = item.get("price_reaction")

    parts = [
        f"### 対象\nticker: {ticker}\ndisclosure_type: {item.get('disclosure_type', 'other')}",
        f"### 今回の開示（公開情報）\n見出し: {title}\n本文:\n{body[:6000]}",
    ]
    if prior:
        parts.append(f"### 前回の関連資料（抜粋・narrative_delta / expectation_gap 用）\n{prior[:3000]}")
    if price_reaction:
        parts.append(
            "### 開示後の値動き（price_reaction_divergence 用・公開情報）\n"
            f"{json.dumps(price_reaction, ensure_ascii=False)}"
        )
    parts.append(_FEATURE_SPEC)
    parts.append(
        "上記の特徴を JSON オブジェクト一つで返せ。各特徴名をキーにし、判断できないものは "
        "null（second_order_impact は [])。コードブロックや説明文は不要。"
    )
    return _SYSTEM, "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Parsing & coercion
# ---------------------------------------------------------------------------


def _parse_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON object extraction from a model response."""
    if not raw:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if not m:
        m = re.search(r"(\{.*\})", raw, re.DOTALL)
    if not m:
        return None
    try:
        out = json.loads(m.group(1))
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _coerce_features(parsed: dict[str, Any]) -> dict[str, Any]:
    """Whitelist + range-clamp model output so a minor overshoot does not crash.

    Unknown keys are dropped. Unit/signed features are clamped to their range,
    ``contradiction_count`` to a non-negative int, ``second_order_impact`` to a
    list of ``{ticker, sign}``. Non-numeric / unparseable values become null.
    """
    feats: dict[str, Any] = {}
    for name in _ALL_FEATURE_NAMES:
        if name not in parsed:
            continue
        val = parsed[name]
        if val is None:
            feats[name] = None
            continue
        if name in _UNIT:
            try:
                feats[name] = _clamp(float(val), 0.0, 1.0)
            except (TypeError, ValueError):
                feats[name] = None
        elif name in _SIGNED:
            try:
                feats[name] = _clamp(float(val), -1.0, 1.0)
            except (TypeError, ValueError):
                feats[name] = None
        elif name == "contradiction_count":
            try:
                feats[name] = max(0, int(val))
            except (TypeError, ValueError):
                feats[name] = None
        elif name == "second_order_impact":
            if isinstance(val, list):
                clean = []
                for e in val:
                    if isinstance(e, dict) and e.get("ticker"):
                        try:
                            raw_sign = int(e.get("sign", 0))
                        except (TypeError, ValueError):
                            raw_sign = 0
                        # Schema constrains sign to {-1, 0, 1}; collapse anything
                        # the model returns (e.g. 99) to its direction.
                        sign = 1 if raw_sign > 0 else (-1 if raw_sign < 0 else 0)
                        clean.append({"ticker": str(e["ticker"]), "sign": sign})
                feats[name] = clean
            else:
                feats[name] = []
    return feats


def _debug_log_path(
    *,
    debug_log_path: Path | str | None,
    log_path: Path | str | None,
) -> Path:
    if debug_log_path is not None:
        return Path(debug_log_path)
    if log_path is not None:
        return Path(log_path).with_name("disclosure_extract_debug.jsonl")
    return _DEFAULT_DEBUG_LOG_PATH


def _source_event_id_for_item(item: dict[str, Any]) -> str | None:
    try:
        return compute_source_event_id(
            str(item.get("source") or ""),
            native_doc_id=item.get("native_doc_id"),
            source_url=item.get("source_url"),
        )
    except ValueError:
        return None


def _append_extract_debug(
    *,
    item: dict[str, Any],
    feature_id: str,
    model_id: str,
    prompt_version: str,
    reason: str,
    raw_response: str,
    parsed: dict[str, Any] | None,
    debug_log_path: Path | str | None,
    log_path: Path | str | None,
    fsync: bool,
    excerpt_chars: int = _DEBUG_RESPONSE_CHARS,
) -> None:
    """Append raw-response diagnostics without storing prompts or book context."""
    append_jsonl_safe(
        _debug_log_path(debug_log_path=debug_log_path, log_path=log_path),
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "feature_id": feature_id,
            "source_event_id": _source_event_id_for_item(item),
            "ticker": item.get("ticker"),
            "source": item.get("source"),
            "market": item.get("market"),
            "disclosure_type": item.get("disclosure_type", "other"),
            "model_id": model_id,
            "prompt_version": prompt_version,
            "parsed_keys": sorted(parsed.keys()) if isinstance(parsed, dict) else None,
            "raw_response_excerpt": str(raw_response or "")[:excerpt_chars],
            "raw_response_truncated": len(str(raw_response or "")) > excerpt_chars,
        },
        fsync=fsync,
    )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_features(
    item: dict[str, Any],
    *,
    api_key: str | None = None,
    base_url: str = _DEFAULT_BASE_URL,
    model_id: str = _DEFAULT_MODEL,
    transport: Callable[..., tuple[str, dict[str, Any]]] | None = None,
    store_path: Path | str | None = None,
    append: bool = True,
    max_tokens: int = 1500,
    temperature: float = 0.2,
    prompt_version: str = PROMPT_VERSION,
    log_path: Path | str | None = None,
    debug_log_path: Path | str | None = None,
    fsync: bool = True,
) -> dict[str, Any]:
    """Extract observe_only features from one public disclosure / news ``item``.

    ``item`` keys: ``source`` (edgar/edinet/tdnet/news), ``ticker``, one of
    ``native_doc_id`` / ``source_url``, ``publish_time``, ``title``/``body``;
    optional ``ingest_time``, ``market``, ``language``, ``disclosure_type``,
    ``prior_excerpt``, ``price_reaction``, ``ticker_resolution_method`` /
    ``_confidence``.

    Returns ``{"ok": bool, "feature": DisclosureFeature|None, "append": dict|None,
    "error": str|None}``. Parse/validation failures return ``ok=False`` with the
    reason rather than raising, so a single bad item never breaks a batch.
    """
    source = item.get("source")
    ticker = item.get("ticker")
    if not source or not ticker or not item.get("publish_time"):
        return {"ok": False, "feature": None, "append": None,
                "error": "item missing required source/ticker/publish_time"}
    if not item.get("native_doc_id") and not item.get("source_url"):
        return {"ok": False, "feature": None, "append": None,
                "error": "item needs native_doc_id or source_url for idempotency"}

    # ingest_time = when we received the item (now, captured BEFORE the model
    # call); compute_time = after the model returns. This ordering guarantees
    # publish <= ingest <= compute (the no-look-ahead invariant) by construction.
    ingest_ts = item.get("ingest_time") or datetime.now(timezone.utc)

    system, user = build_prompt(item)
    kind = "public_news" if source == "news" else "public_disclosure"
    feature_id = new_row_id()

    res = call_external_llm(
        Payload(kind=kind, system=system, user=user, source_url=item.get("source_url")),
        base_url=base_url,
        api_key=api_key or "",
        model_id=model_id,
        role="disclosure_extractor",
        max_tokens=max_tokens,
        temperature=temperature,
        transport=transport,
        log_path=log_path,
        fsync=fsync,
    )

    parsed = _parse_json(res.content)
    if parsed is None:
        _append_extract_debug(
            item=item,
            feature_id=feature_id,
            model_id=res.model_id,
            prompt_version=prompt_version,
            reason="parse_json_none",
            raw_response=res.content,
            parsed=None,
            debug_log_path=debug_log_path,
            log_path=log_path,
            fsync=fsync,
        )
        return {"ok": False, "feature": None, "append": None,
                "error": "could not parse JSON from model response"}

    feats = _coerce_features(parsed)
    raw_directional = parsed.get("directional_score")
    if raw_directional is None or feats.get("directional_score") is None:
        _append_extract_debug(
            item=item,
            feature_id=feature_id,
            model_id=res.model_id,
            prompt_version=prompt_version,
            reason=(
                "directional_score_missing_or_null"
                if raw_directional is None
                else "directional_score_unusable"
            ),
            raw_response=res.content,
            parsed=parsed,
            debug_log_path=debug_log_path,
            log_path=log_path,
            fsync=fsync,
        )
    raw_text = f"{item.get('title', '')}\n{item.get('body', '')}"
    raw_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    cost_usd = estimate_cost_usd(res.model_id, res.input_tokens, res.output_tokens)

    try:
        feature = make_feature(
            source=source,
            ticker=ticker,
            publish_time=item["publish_time"],
            ingest_time=ingest_ts,
            compute_time=datetime.now(timezone.utc),
            disclosure_type=item.get("disclosure_type", "other"),
            market=item.get("market", "US"),
            native_doc_id=item.get("native_doc_id"),
            source_url=item.get("source_url"),
            availability_lag_min=int(item.get("availability_lag_min", 0)),
            language=item.get("language"),
            raw_text_sha256=raw_sha,
            raw_archive_ref=item.get("raw_archive_ref"),
            ticker_resolution_method=item.get("ticker_resolution_method"),
            ticker_resolution_confidence=item.get("ticker_resolution_confidence"),
            model_id=res.model_id,
            prompt_version=prompt_version,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
            cost_usd=cost_usd,
            features=feats,
            evidence=parsed.get("evidence") if isinstance(parsed.get("evidence"), list) else [],
            summary=str(parsed.get("summary", ""))[:500],
            observe_only=True,
            feature_id=feature_id,
        )
    except ValueError as e:
        return {"ok": False, "feature": None, "append": None, "error": str(e)}

    append_res = append_feature(feature, path=store_path) if append else None
    return {"ok": True, "feature": feature, "append": append_res, "error": None}

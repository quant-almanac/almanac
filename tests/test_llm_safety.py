"""Tests for almanac.llm_safety — the external-LLM privacy choke-point.

The contract under test (plan "Cross-cutting — external-LLM safety layer"):

- Only allowlisted ``Payload.kind`` values may leave the process; everything
  else (including the Phase-0.5-deferred ``public_social``) raises
  ``PrivacyViolation`` *before* any network call.
- A secondary regex backstop catches the legacy Red Team leak tokens
  (``value_jpy`` / ``unrealized_pct`` / ``pos_summary`` / JP balance terms).
- ``call_external_llm`` records model_id + token usage to the usage log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.llm_safety import (  # noqa: E402
    ALLOWED_KINDS,
    DEFERRED_KINDS,
    ExternalLLMResult,
    Payload,
    PrivacyViolation,
    assert_public_payload,
    call_external_llm,
    scan_text_for_pii,
)


def _clean_payload(kind: str = "public_disclosure") -> Payload:
    return Payload(
        kind=kind,
        system="You read public filings.",
        user="トヨタ(7203.T) は通期営業利益見通しを上方修正した。",
        source_url="https://example.com/edinet/doc123",
    )


# ---------------------------------------------------------------------------
# allowlist (primary guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(ALLOWED_KINDS))
def test_allowed_kinds_pass(kind: str) -> None:
    assert_public_payload(_clean_payload(kind))  # must not raise


def test_unknown_kind_rejected() -> None:
    with pytest.raises(PrivacyViolation, match="not in the allowlist"):
        assert_public_payload(_clean_payload("portfolio_snapshot"))


@pytest.mark.parametrize("kind", sorted(DEFERRED_KINDS))
def test_deferred_kind_rejected_with_phase_message(kind: str) -> None:
    with pytest.raises(PrivacyViolation, match="deferred to Phase 0.5"):
        assert_public_payload(_clean_payload(kind))


# ---------------------------------------------------------------------------
# PII backstop (secondary defense)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leak",
    [
        '{"value_jpy": 2500000}',
        '{"unrealized_pct": 15.3}',
        "pos_summary follows:",
        "口座残高は3000万円",
        "含み益が出ている",
        "評価額の内訳",
    ],
)
def test_pii_backstop_fires_on_book_tokens(leak: str) -> None:
    payload = Payload(kind="anonymized_market_gap", system="x", user=leak)
    with pytest.raises(PrivacyViolation, match="PII backstop"):
        assert_public_payload(payload)


def test_scan_returns_hits_and_empty() -> None:
    assert scan_text_for_pii("clean public text") == []
    hits = scan_text_for_pii("value_jpy and unrealized_pct here")
    assert any("value_jpy" in h for h in hits)
    assert any("unrealized_pct" in h for h in hits)


@pytest.mark.parametrize("kind", ["public_disclosure", "public_news", "public_market_context"])
@pytest.mark.parametrize("jp", ["評価額の内訳", "含み損を計上", "保有銘柄の状況", "口座残高"])
def test_public_kinds_allow_japanese_holdings_vocabulary(kind: str, jp: str) -> None:
    """EDINET/TDnet filings naturally use these words — must NOT fail-close."""
    payload = Payload(kind=kind, system="読め", user=f"当社の{jp}は次の通り。")
    assert_public_payload(payload)  # must not raise


@pytest.mark.parametrize("kind", ["public_disclosure", "public_news", "public_market_context"])
def test_internal_field_names_blocked_on_every_kind(kind: str) -> None:
    """Internal snake_case book fields leak the book on ANY kind → blocked."""
    payload = Payload(kind=kind, system="x", user='{"value_jpy": 100}')
    with pytest.raises(PrivacyViolation, match="PII backstop"):
        assert_public_payload(payload)


@pytest.mark.parametrize("jp", ["評価額", "含み益", "保有株"])
def test_anonymized_kinds_still_block_japanese_holdings(jp: str) -> None:
    """On book-derived (anonymized) kinds the JP vocabulary still signals a leak."""
    for kind in ("anonymized_market_gap", "anonymized_recommendations"):
        with pytest.raises(PrivacyViolation, match="PII backstop"):
            assert_public_payload(Payload(kind=kind, system="x", user=f"{jp}…"))


# ---------------------------------------------------------------------------
# call_external_llm — validation + logging via injected transport
# ---------------------------------------------------------------------------


def test_call_validates_before_transport(tmp_path: Path) -> None:
    """A bad payload must raise before the transport is ever invoked."""
    called = {"n": 0}

    def _tx(**kwargs):
        called["n"] += 1
        return "should not happen", {}

    with pytest.raises(PrivacyViolation):
        call_external_llm(
            _clean_payload("portfolio_snapshot"),
            base_url="https://api.deepseek.com",
            api_key="k",
            model_id="deepseek-chat",
            transport=_tx,
            log_path=tmp_path / "llm_calls.jsonl",
        )
    assert called["n"] == 0


def test_call_returns_result_and_logs_usage(tmp_path: Path) -> None:
    log = tmp_path / "llm_calls.jsonl"

    def _tx(**kwargs):
        assert kwargs["model_id"] == "deepseek-chat"
        return "extracted", {"input_tokens": 123, "output_tokens": 45}

    res = call_external_llm(
        _clean_payload("public_disclosure"),
        base_url="https://api.deepseek.com",
        api_key="k",
        model_id="deepseek-chat",
        role="disclosure_extractor",
        transport=_tx,
        log_path=log,
        fsync=False,
    )

    assert isinstance(res, ExternalLLMResult)
    assert res.content == "extracted"
    assert (res.input_tokens, res.output_tokens) == (123, 45)

    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["model"] == "deepseek-chat"
    assert row["role"] == "disclosure_extractor"
    assert row["kind"] == "public_disclosure"
    assert row["input_tokens"] == 123
    assert row["output_tokens"] == 45
    assert row["source_url"] == "https://example.com/edinet/doc123"


# ---------------------------------------------------------------------------
# call_book_aware_llm — Codex P2 #13 (audit path for book-bearing tier calls)
# ---------------------------------------------------------------------------


def test_book_aware_logs_and_skips_pii_scan(tmp_path: Path, monkeypatch) -> None:
    from almanac.llm_safety import (
        Payload, call_book_aware_llm, BOOK_AWARE_KIND, ExternalLLMResult,
    )
    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "multi_provider_book_aware")
    log = tmp_path / "llm_calls.jsonl"
    seen: dict = {}

    def _tx(**kwargs):
        seen.update(kwargs)
        return "tier-json", {"input_tokens": 10, "output_tokens": 5}

    # 本来 PII backstop に引っかかる book 内容を「意図的に」通す。
    payload = Payload(kind=BOOK_AWARE_KIND, system="保有銘柄と含み損を読め",
                      user='{"value_jpy": 100, "pos_summary": []}')
    res = call_book_aware_llm(payload, model_id="deepseek-v4-pro", transport=_tx,
                              role="tier_analysis_long", log_path=log, fsync=False)
    assert isinstance(res, ExternalLLMResult)
    assert res.content == "tier-json"
    assert seen["system"].startswith("保有")  # book が transport に到達 (ブロックされない)

    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["kind"] == "book_aware_tier"
    assert row["book_aware"] is True and row["contains_book"] is True
    assert row["model"] == "deepseek-v4-pro"
    assert (row["input_tokens"], row["output_tokens"]) == (10, 5)


def test_book_aware_rejects_non_book_kind(tmp_path: Path) -> None:
    from almanac.llm_safety import Payload, call_book_aware_llm
    with pytest.raises(PrivacyViolation):
        call_book_aware_llm(
            Payload(kind="public_disclosure", system="x", user="y"),
            model_id="m", transport=lambda **k: ("x", {}),
            log_path=tmp_path / "l.jsonl", fsync=False,
        )


def test_public_path_rejects_book_aware_kind(tmp_path: Path) -> None:
    from almanac.llm_safety import Payload, BOOK_AWARE_KIND
    with pytest.raises(PrivacyViolation):
        call_external_llm(
            Payload(kind=BOOK_AWARE_KIND, system="x", user="y"),
            base_url="b", api_key="k", model_id="m",
            transport=lambda **k: ("x", {}),
            log_path=tmp_path / "l.jsonl", fsync=False,
        )


def test_book_aware_logs_failed_call(tmp_path: Path, monkeypatch) -> None:
    """Codex re-review #13: 失敗した book-aware 通信も監査ログ (status=error) に残し、再送出する。"""
    from almanac.llm_safety import Payload, call_book_aware_llm, BOOK_AWARE_KIND
    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "multi_provider_book_aware")
    log = tmp_path / "llm_calls.jsonl"

    def _boom(**kwargs):
        raise RuntimeError("deepseek 500")

    with pytest.raises(RuntimeError):
        call_book_aware_llm(
            Payload(kind=BOOK_AWARE_KIND, system="s", user="u"),
            model_id="deepseek-v4-pro", transport=_boom,
            role="tier_analysis", log_path=log, fsync=False,
        )
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["status"] == "error"
    assert "deepseek 500" in row["error"]
    assert row["book_aware"] is True
    assert row["kind"] == "book_aware_tier"

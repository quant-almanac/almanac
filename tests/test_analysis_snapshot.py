"""Stage 1B: AnalysisSnapshot の2レーン凍結 (analysis_snapshot.py)。

背景: 日次分析は holdings/cash/prices/FX/macro/news/screening を一括取得
してから tier LLM を呼ぶが、「その分析の判断根拠として確定した瞬間に何が
真だったか」を示す監査記録が無かった。本テストは:

  (a) base_snapshot の7カテゴリが正しく鮮度・ハッシュを計算すること
  (b) enriched_snapshot がオプションデータを正しく取り込むこと
  (c) DecisionSnapshot の確定が immutable であること (2回目の freeze は
      上書きしない) — 同一 analysis_id を異なる時点で誤って再利用しても
      監査記録が最初の1回を権威として保つという Stage 1B の核心契約
  (d) execution_quote_snapshot が decision_snapshot と完全に別のレーンで
      あり、decision_snapshot_state.json に一切書き込まないこと
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import analysis_snapshot as snap  # noqa: E402


# ---------------------------------------------------------------------------
# _extract_json_timestamp / _freshness_status
# ---------------------------------------------------------------------------


def test_extract_timestamp_from_named_key(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"generated_at": "2026-07-27T10:00:00"}), encoding="utf-8")
    ts = snap._extract_json_timestamp(p, ("generated_at",))
    assert ts == datetime(2026, 7, 27, 10, 0, 0)


def test_extract_timestamp_falls_back_through_key_list(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"scan_time": "2026-07-27 09:30"}), encoding="utf-8")
    ts = snap._extract_json_timestamp(p, ("generated_at", "scan_time"))
    assert ts == datetime(2026, 7, 27, 9, 30)


def test_extract_timestamp_mtime_fallback(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{}", encoding="utf-8")
    ts = snap._extract_json_timestamp(p, ("__mtime__",))
    assert ts is not None
    assert (datetime.now() - ts).total_seconds() < 60


def test_extract_timestamp_missing_file_is_none(tmp_path):
    assert snap._extract_json_timestamp(tmp_path / "nope.json", ("generated_at",)) is None


def test_extract_timestamp_accepts_unix_seconds_and_milliseconds(tmp_path):
    seconds = 1_785_256_745.0
    _write(tmp_path, "seconds.json", {"as_of": seconds})
    _write(tmp_path, "milliseconds.json", {"as_of": seconds * 1000})

    expected = datetime.fromtimestamp(seconds)
    assert snap._extract_json_timestamp(tmp_path / "seconds.json", ("as_of",)) == expected
    assert snap._extract_json_timestamp(tmp_path / "milliseconds.json", ("as_of",)) == expected


def test_extract_timestamp_rejects_small_numeric_counters(tmp_path):
    _write(tmp_path, "counter.json", {"scanned": 86})

    assert snap._extract_json_timestamp(tmp_path / "counter.json", ("scanned",)) is None


def test_freshness_status_thresholds():
    now = datetime(2026, 7, 27, 12, 0, 0)
    assert snap._freshness_status(None, now=now, max_age_hours=24) == "unknown"
    assert snap._freshness_status(now - timedelta(hours=1), now=now, max_age_hours=24) == "fresh"
    assert snap._freshness_status(now - timedelta(hours=20), now=now, max_age_hours=24) == "degraded"
    assert snap._freshness_status(now - timedelta(hours=30), now=now, max_age_hours=24) == "stale"


# ---------------------------------------------------------------------------
# build_base_snapshot
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, content: dict) -> None:
    (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")


def test_base_snapshot_all_seven_categories_present(tmp_path):
    now = datetime(2026, 7, 27, 12, 0, 0)
    _write(tmp_path, "account.json", {"last_updated": "2026-07-27T08:00:00"})
    _write(tmp_path, "technical_state.json", {"cached_at": "2026-07-27T11:00:00"})
    _write(tmp_path, "macro_event_state.json", {"refreshed_at": "2026-07-27T06:00:00"})
    _write(tmp_path, "news_signal_candidates.json", {"generated_at": "2026-07-27T07:00:00"})
    _write(tmp_path, "short_candidates.json", {"generated_at": "2026-07-27T05:00:00"})
    _write(tmp_path, "margin_long_candidates.json", {"generated_at": "2026-07-27T05:30:00"})
    _write(tmp_path, "long_term_screen_results.json", {"as_of": "2026-07-27"})
    (tmp_path / "holdings.json").write_text("{}", encoding="utf-8")

    base = snap.build_base_snapshot(base_dir=tmp_path, now=now)

    for field_name in ("holdings", "cash", "prices", "fx", "macro", "news", "screening"):
        prov = getattr(base, field_name)
        assert isinstance(prov, snap.SourceProvenance)
        assert prov.artifact_hash != snap._MISSING_HASH


def test_base_snapshot_fx_uses_rate_timestamp_before_account_date(tmp_path):
    now = datetime(2026, 7, 29, 1, 45, 0)
    rate_as_of = datetime(2026, 7, 29, 1, 30, 0)
    _write(
        tmp_path,
        "account.json",
        {
            "last_updated": "2026-07-28",
            "fx_rate_usdjpy": 163.67,
            "fx_rate_usdjpy_as_of": rate_as_of.timestamp(),
        },
    )

    base = snap.build_base_snapshot(base_dir=tmp_path, now=now)

    assert base.fx.source_as_of == rate_as_of.isoformat()
    assert base.fx.freshness_status == "fresh"


def test_base_snapshot_missing_files_are_unknown_not_fresh(tmp_path):
    """存在しないファイルを暗黙に fresh 扱いしない (fail-closed)。"""
    now = datetime(2026, 7, 27, 12, 0, 0)
    base = snap.build_base_snapshot(base_dir=tmp_path, now=now)
    assert base.macro.freshness_status == "unknown"
    assert base.macro.artifact_hash == snap._MISSING_HASH
    assert base.news.freshness_status == "unknown"
    assert base.screening.artifact_hash == snap._MISSING_HASH


def test_screening_takes_the_oldest_of_its_component_files(tmp_path):
    """screening は複数ファイル合成。最も古い as_of を採用する (楽観的に
    新しい方を採用して鮮度を過大評価しない)。"""
    now = datetime(2026, 7, 27, 12, 0, 0)
    _write(tmp_path, "short_candidates.json", {"generated_at": "2026-07-27T10:00:00"})
    _write(tmp_path, "margin_long_candidates.json", {"generated_at": "2026-07-20T10:00:00"})  # 古い
    _write(tmp_path, "long_term_screen_results.json", {"as_of": "2026-07-27"})

    base = snap.build_base_snapshot(base_dir=tmp_path, now=now)
    assert base.screening.source_as_of == "2026-07-20T10:00:00"
    assert base.screening.freshness_status == "stale"  # 7日前 > 72h


def test_screening_uses_short_as_of_not_scanned_count(tmp_path):
    now = datetime(2026, 7, 29, 2, 0, 0)
    _write(
        tmp_path,
        "short_candidates.json",
        {"as_of": "2026-07-28 18:30", "scanned": 86},
    )
    _write(
        tmp_path,
        "margin_long_candidates.json",
        {"generated_at": "2026-07-28 19:16"},
    )
    _write(
        tmp_path,
        "long_term_screen_results.json",
        {"as_of": "2026-07-27 07:03"},
    )

    base = snap.build_base_snapshot(base_dir=tmp_path, now=now)

    assert base.screening.source_as_of == "2026-07-27T07:03:00"
    assert base.screening.freshness_status == "degraded"


def test_base_snapshot_hash_changes_when_file_content_changes(tmp_path):
    now = datetime(2026, 7, 27, 12, 0, 0)
    _write(tmp_path, "account.json", {"last_updated": "2026-07-27T08:00:00", "balance": 100})
    h1 = snap.build_base_snapshot(base_dir=tmp_path, now=now).cash.artifact_hash
    _write(tmp_path, "account.json", {"last_updated": "2026-07-27T08:00:00", "balance": 200})
    h2 = snap.build_base_snapshot(base_dir=tmp_path, now=now).cash.artifact_hash
    assert h1 != h2


def test_in_memory_payload_hash_changes_even_when_artifact_is_unchanged(tmp_path):
    now = datetime(2026, 7, 27, 12, 0, 0)
    _write(tmp_path, "account.json", {"last_updated": now.isoformat()})
    first = snap.build_base_snapshot_from_data(
        {"positions": [{"ticker": "AVGO", "shares": 5}], "cash_info": {}},
        base_dir=tmp_path,
        now=now,
    )
    second = snap.build_base_snapshot_from_data(
        {"positions": [{"ticker": "AVGO", "shares": 27}], "cash_info": {}},
        base_dir=tmp_path,
        now=now,
    )
    assert first.holdings.artifact_hash == second.holdings.artifact_hash
    assert first.holdings.payload_hash != second.holdings.payload_hash


def test_base_snapshot_freezes_market_regime_v2_payload(tmp_path):
    now = datetime(2026, 7, 27, 12, 0, 0)
    first = snap.build_base_snapshot_from_data(
        {"market_regime_v2": {"policy_version": "v1", "portfolio": {"score": 10}}},
        base_dir=tmp_path,
        now=now,
    )
    second = snap.build_base_snapshot_from_data(
        {"market_regime_v2": {"policy_version": "v1", "portfolio": {"score": 20}}},
        base_dir=tmp_path,
        now=now,
    )

    assert first.macro.payload_hash != second.macro.payload_hash


# ---------------------------------------------------------------------------
# build_enriched_snapshot
# ---------------------------------------------------------------------------


def test_enriched_snapshot_with_no_options_is_just_the_base(tmp_path):
    base = snap.build_base_snapshot(base_dir=tmp_path, now=datetime(2026, 7, 27))
    enriched = snap.build_enriched_snapshot(base, now=datetime(2026, 7, 27))
    assert enriched.base == base
    assert enriched.options_by_ticker == {}


def test_enriched_snapshot_captures_options_payload_per_ticker():
    base = snap.build_base_snapshot(base_dir=Path("/nonexistent"), now=datetime(2026, 7, 27))
    payload = {
        "AVGO": {
            "put_call_ratio": 0.8, "iv_rank": 45,
            "as_of": "2026-07-27T00:00:00",
        },
        "SPY": {
            "put_call_ratio": 1.1, "iv_rank": 30,
            "as_of": "2026-07-27T00:00:00",
        },
    }
    enriched = snap.build_enriched_snapshot(base, options_by_ticker_raw=payload, now=datetime(2026, 7, 27))
    assert set(enriched.options_by_ticker.keys()) == {"AVGO", "SPY"}
    assert enriched.options_by_ticker["AVGO"].source == "options_fetcher:AVGO"
    assert enriched.options_by_ticker["AVGO"].freshness_status == "fresh"
    assert enriched.options_by_ticker["AVGO"].artifact_hash != snap._MISSING_HASH


def test_enriched_snapshot_options_hash_reflects_payload_content():
    base = snap.build_base_snapshot(base_dir=Path("/nonexistent"), now=datetime(2026, 7, 27))
    e1 = snap.build_enriched_snapshot(base, options_by_ticker_raw={"AVGO": {"iv_rank": 45}})
    e2 = snap.build_enriched_snapshot(base, options_by_ticker_raw={"AVGO": {"iv_rank": 99}})
    assert e1.options_by_ticker["AVGO"].artifact_hash != e2.options_by_ticker["AVGO"].artifact_hash


def test_decision_freshness_issues_include_unknown_option_inputs(tmp_path):
    base = snap.build_base_snapshot(base_dir=tmp_path, now=datetime(2026, 7, 27))
    enriched = snap.build_enriched_snapshot(
        base,
        options_by_ticker_raw={"AVGO": {"iv_rank": 45}},
        now=datetime(2026, 7, 27),
    )
    issues = snap.decision_freshness_issues(enriched)
    categories = {row["category"] for row in issues}
    assert "holdings" in categories
    assert any(
        row.get("category") == "options"
        and row.get("ticker") == "AVGO"
        and row.get("status") == "unknown"
        for row in issues
    )


def test_decision_input_health_surfaces_macro_fallback_and_stale_options(tmp_path):
    now = datetime(2026, 7, 27, 12, 0, 0)
    _write(
        tmp_path,
        "macro_event_state.json",
        {"refreshed_at": "2026-07-27T11:00:00"},
    )
    base = snap.build_base_snapshot(base_dir=tmp_path, now=now)
    enriched = snap.build_enriched_snapshot(
        base,
        options_by_ticker_raw={
            "AVGO": {"as_of": "2026-07-26T11:00:00", "iv_rank": 50},
        },
        now=now,
    )
    health = snap.decision_input_health(
        enriched,
        macro_state={"status": "degraded", "errors": ["bls:403"]},
    )
    assert health["macro_event_calendar"]["status"] == "warn"
    assert health["macro_event_calendar"]["extra"]["source_errors"] == ["bls:403"]
    assert health["options_inputs"]["status"] == "error"
    assert health["options_inputs"]["extra"]["nonfresh_tickers"] == ["AVGO"]


def test_old_options_timestamp_is_stale_not_fresh():
    now = datetime(2026, 7, 27, 12, 0, 0)
    base = snap.build_base_snapshot(base_dir=Path("/nonexistent"), now=now)
    enriched = snap.build_enriched_snapshot(
        base,
        options_by_ticker_raw={
            "AVGO": {"iv_rank": 45, "as_of": "2026-07-25T00:00:00"},
        },
        now=now,
    )
    assert enriched.options_by_ticker["AVGO"].freshness_status == "stale"


# ---------------------------------------------------------------------------
# freeze_decision_snapshot — immutability契約
# ---------------------------------------------------------------------------


def _sample_enriched(tmp_path, now):
    base = snap.build_base_snapshot(base_dir=tmp_path, now=now)
    return snap.build_enriched_snapshot(base, now=now)


def test_freeze_persists_atomically(tmp_path):
    now = datetime(2026, 7, 27, 6, 0, 0)
    enriched = _sample_enriched(tmp_path, now)
    result = snap.freeze_decision_snapshot(
        enriched, decision_snapshot_id="abc123", stage="tier",
        analysis_id="analysis-123",
        code_revision="deadbeef", model_ids={"sonnet": "claude-sonnet-5"},
        now=now, base_dir=tmp_path,
    )
    assert result.decision_snapshot_id == "abc123"
    assert result.analysis_id == "analysis-123"
    assert result.stage == "tier"
    path = tmp_path / "decision_snapshot_state.json"
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["abc123"]["tier"]["analysis_id"] == "analysis-123"
    assert on_disk["abc123"]["tier"]["code_revision"] == "deadbeef"
    # 一時ファイルが残っていない (atomic replace されている)
    assert not (tmp_path / "decision_snapshot_state.tmp").exists()


def test_freeze_does_not_overwrite_same_id_and_stage(tmp_path):
    """本題: 同じ (decision_snapshot_id, stage) で2回目に呼んでも、
    最初の内容が権威のまま保たれる。"""
    now1 = datetime(2026, 7, 27, 6, 0, 0)
    now2 = datetime(2026, 7, 27, 9, 0, 0)
    enriched1 = _sample_enriched(tmp_path, now1)

    first = snap.freeze_decision_snapshot(
        enriched1, decision_snapshot_id="xyz", stage="tier",
        code_revision="rev1", now=now1, base_dir=tmp_path,
    )

    # 内容を変えて (holdings.json 更新) 2回目を呼ぶ
    (tmp_path / "holdings.json").write_text(json.dumps({"AVGO": {}}), encoding="utf-8")
    enriched2 = _sample_enriched(tmp_path, now2)
    second = snap.freeze_decision_snapshot(
        enriched2, decision_snapshot_id="xyz", stage="tier",
        code_revision="rev2", now=now2, base_dir=tmp_path,
    )

    assert second.code_revision == "rev1"  # rev2 ではない — 上書きされていない
    assert second.frozen_at == first.frozen_at
    assert second.enriched.base.holdings.artifact_hash == first.enriched.base.holdings.artifact_hash


def test_freeze_allows_independent_stages_for_the_same_id(tmp_path):
    now = datetime(2026, 7, 27, 6, 0, 0)
    enriched = _sample_enriched(tmp_path, now)
    snap.freeze_decision_snapshot(enriched, decision_snapshot_id="multi", stage="tier",
                                    code_revision="r1", now=now, base_dir=tmp_path)
    snap.freeze_decision_snapshot(enriched, decision_snapshot_id="multi", stage="synthesis",
                                    code_revision="r2", now=now, base_dir=tmp_path)
    on_disk = json.loads((tmp_path / "decision_snapshot_state.json").read_text(encoding="utf-8"))
    assert on_disk["multi"]["tier"]["code_revision"] == "r1"
    assert on_disk["multi"]["synthesis"]["code_revision"] == "r2"


def test_resolve_decision_snapshot_round_trips(tmp_path):
    now = datetime(2026, 7, 27, 6, 0, 0)
    enriched = _sample_enriched(tmp_path, now)
    snap.freeze_decision_snapshot(enriched, decision_snapshot_id="lookup-me", stage="tier",
                                    analysis_id="analysis-lookup",
                                    code_revision="r1", now=now, base_dir=tmp_path)

    full = snap.resolve_decision_snapshot("lookup-me", base_dir=tmp_path)
    assert "tier" in full
    tier_only = snap.resolve_decision_snapshot("lookup-me", stage="tier", base_dir=tmp_path)
    assert tier_only["analysis_id"] == "analysis-lookup"
    assert tier_only["code_revision"] == "r1"
    assert snap.resolve_decision_snapshot("no-such-id", base_dir=tmp_path) is None
    assert snap.resolve_decision_snapshot("lookup-me", stage="synthesis", base_dir=tmp_path) is None


def test_resolve_missing_file_returns_none(tmp_path):
    assert snap.resolve_decision_snapshot("anything", base_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# execution_quote_snapshot — decision_snapshot と別レーン
# ---------------------------------------------------------------------------


def test_execution_quote_snapshot_is_tagged_distinctly():
    q = snap.build_execution_quote_snapshot(
        "AVGO",
        price=410.5,
        spread=0.2,
        market_status="open",
        source_as_of="2026-07-28T09:00:00+09:00",
        decision_snapshot_id="decision-1",
        decision_snapshot_hash="abc123",
    )
    assert q["snapshot_kind"] == "execution_quote"
    assert q["ticker"] == "AVGO"
    assert q["price"] == 410.5
    assert q["source_as_of"] == "2026-07-28T09:00:00+09:00"
    assert q["decision_snapshot_id"] == "decision-1"
    assert q["decision_snapshot_hash"] == "abc123"
    assert len(q["quote_hash"]) == 64


def test_execution_quote_snapshot_never_touches_decision_snapshot_state(tmp_path, monkeypatch):
    """本題: execution_quote_snapshot はファイルI/Oを一切行わない
    (decision_snapshot_state.json に書き込む経路がそもそも無い)。"""
    monkeypatch.chdir(tmp_path)
    snap.build_execution_quote_snapshot("AVGO", price=410.5)
    assert not (tmp_path / "decision_snapshot_state.json").exists()

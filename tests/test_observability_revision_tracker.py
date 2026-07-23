"""Tests for almanac.observability.revision_tracker.

Coverage pins down:

- The four shipped regex patterns match realistic JP / EN headlines and
  reject unrelated text.
- Direction resolution handles unanimous, mixed, and tied bags of matches.
- :func:`compute_surprise_score` / :func:`compute_priced_in_penalty`
  honour their boundary behaviour (clipping, freshness window, reaction
  threshold).
- The mention ledger respects the 30-day cutoff and skips malformed
  rows without crashing.
- :func:`run` is end-to-end deterministic against a fixture
  ``news_signal_candidates.json``.
- :func:`run` does not call the optional snapshot / market-reaction
  providers when they are not supplied.
- ``revision_state.json`` writes atomically (no ``.tmp`` residue).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.revision_tracker import (  # noqa: E402
    REVISION_KEYWORDS,
    RevisionKeyword,
    RevisionMatch,
    _resolve_direction,
    append_mention_ledger,
    build_ticker_entry,
    compute_priced_in_penalty,
    compute_surprise_score,
    load_mention_ledger,
    match_headlines,
    run,
    write_revision_state,
)


# ---------------------------------------------------------------------------
# Keyword registry
# ---------------------------------------------------------------------------


def test_registry_has_one_pattern_per_locale_x_direction() -> None:
    """4 patterns: (ja, up), (en, up), (ja, down), (en, down)."""
    pairs = {(kw.locale, kw.direction) for kw in REVISION_KEYWORDS}
    assert pairs == {("ja", "up"), ("en", "up"), ("ja", "down"), ("en", "down")}


def test_every_keyword_compiles() -> None:
    for kw in REVISION_KEYWORDS:
        assert kw.compile().pattern == kw.pattern


# ---------------------------------------------------------------------------
# match_headlines — positive cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headline,direction",
    [
        ("サンプル企業が業績予想を上方修正", "up"),
        ("ソニーG 通期業績予想を上方修正、営業益最高更新", "up"),
        ("増額修正でストップ高比例配分", "up"),
        ("NVIDIA raises full-year guidance on AI demand", "up"),
        ("Company raised guidance for FY26", "up"),
        ("Upward revision lifts shares 5%", "up"),
        ("業績予想を下方修正、減益見通しを発表", "down"),
        ("通期業績予想を下方修正", "down"),
        ("減額修正で株価急落", "down"),
        ("Boeing cuts guidance amid supply issues", "down"),
        ("Lowers full-year forecast on weak orders", "down"),
        ("Company issues downward revision to FY guidance", "down"),
    ],
)
def test_match_recognizes_realistic_headlines(headline: str, direction: str) -> None:
    matches = match_headlines([headline])
    assert matches, f"expected match for {headline!r}"
    assert any(m.keyword.direction == direction for m in matches)


# ---------------------------------------------------------------------------
# match_headlines — negative / edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headline",
    [
        "Apple posts its strongest China quarter in years",
        "Tesla announces new gigafactory in Texas",
        "市場予想を上回る決算を発表",   # beat, not a guidance revision
        "Q1 earnings beat consensus",
        "",
        "       ",
    ],
)
def test_match_ignores_unrelated_headlines(headline: str) -> None:
    assert match_headlines([headline]) == []


def test_match_skips_none_and_empty_entries() -> None:
    """``top_headlines`` in production sometimes contains ``None`` / ``""``."""
    matches = match_headlines([None, "", "上方修正発表", None])  # type: ignore[list-item]
    assert len(matches) == 1
    assert matches[0].keyword.direction == "up"


def test_match_produces_multiple_hits_when_keywords_cross_locales() -> None:
    """A bilingual headline matching JP + EN patterns produces two matches."""
    h = "Toyota は通期業績予想を上方修正 / Toyota raises full-year guidance"
    matches = match_headlines([h])
    languages = {m.keyword.locale for m in matches}
    assert languages == {"ja", "en"}


def test_revision_match_headline_hash_is_stable() -> None:
    m = RevisionMatch(REVISION_KEYWORDS[0], "上方修正発表")
    again = RevisionMatch(REVISION_KEYWORDS[1], "上方修正発表")
    assert m.headline_hash == again.headline_hash
    assert len(m.headline_hash) == 16


# ---------------------------------------------------------------------------
# _resolve_direction
# ---------------------------------------------------------------------------


def _mk(direction: str) -> RevisionMatch:
    kw = next(k for k in REVISION_KEYWORDS if k.direction == direction)
    return RevisionMatch(kw, "headline")


def test_resolve_direction_empty_is_none() -> None:
    assert _resolve_direction([]) == ("none", 0.0)


def test_resolve_direction_unanimous_up_is_strength_one() -> None:
    assert _resolve_direction([_mk("up"), _mk("up")]) == ("up", 1.0)


def test_resolve_direction_majority_up() -> None:
    direction, strength = _resolve_direction([_mk("up"), _mk("up"), _mk("up"), _mk("down")])
    assert direction == "up"
    # (3 up - 1 down) / 4 total = 0.5
    assert strength == pytest.approx(0.5)


def test_resolve_direction_tie_collapses_to_none() -> None:
    """Ties remove all signal — we'd rather wait than guess."""
    assert _resolve_direction([_mk("up"), _mk("down")]) == ("none", 0.0)


# ---------------------------------------------------------------------------
# Surprise score & priced-in penalty
# ---------------------------------------------------------------------------


def test_surprise_zero_priors_fresh_and_unreacted_is_near_max() -> None:
    s = compute_surprise_score(prior_mentions_count=0, days_since_first_seen=0, market_reaction_pct=0.0)
    # 0.5*1 + 0.3 + 0.2*1 = 1.0
    assert s == pytest.approx(1.0)


def test_surprise_clips_into_unit_interval() -> None:
    # Crank everything; should never exceed 1.0.
    assert 0.0 <= compute_surprise_score(0, 0, 0.0) <= 1.0
    # Crank everything against; should not go below 0.
    assert 0.0 <= compute_surprise_score(prior_mentions_count=999, days_since_first_seen=999, market_reaction_pct=99.0) <= 1.0


def test_surprise_freshness_window_is_two_days_inclusive() -> None:
    on_day_2 = compute_surprise_score(prior_mentions_count=0, days_since_first_seen=2, market_reaction_pct=0.0)
    on_day_3 = compute_surprise_score(prior_mentions_count=0, days_since_first_seen=3, market_reaction_pct=0.0)
    assert on_day_2 > on_day_3
    # Drop should equal the 0.3 freshness bonus.
    assert (on_day_2 - on_day_3) == pytest.approx(0.3)


def test_priced_in_penalty_caps_at_zero_point_six() -> None:
    """Round 9 #7 — never veto a momentum hypothesis outright."""
    capped = compute_priced_in_penalty(
        prior_mentions_count=1000,
        days_since_first_seen=1000,
        market_reaction_pct=99.0,
    )
    assert capped == pytest.approx(0.6)


def test_priced_in_penalty_zero_on_fresh_no_mentions() -> None:
    assert compute_priced_in_penalty(0, 0, 0.0) == 0.0


def test_priced_in_penalty_negative_inputs_are_clamped_to_zero() -> None:
    assert compute_priced_in_penalty(-5, -1, 0.0) == 0.0


# ---------------------------------------------------------------------------
# build_ticker_entry
# ---------------------------------------------------------------------------


def test_build_entry_empty_matches_returns_direction_none() -> None:
    e = build_ticker_entry(
        ticker="NVDA",
        matches=[],
        prior_mentions_count=0,
        first_seen_at=None,
        today=date(2026, 5, 24),
    )
    assert e.direction == "none"
    assert e.strength == 0.0
    assert e.sources == []


def test_build_entry_populates_sources_from_matches() -> None:
    matches = match_headlines(["NVIDIA raises full-year guidance"])
    e = build_ticker_entry(
        ticker="NVDA",
        matches=matches,
        prior_mentions_count=0,
        first_seen_at="2026-05-22T05:00:00",
        today=date(2026, 5, 24),
        last_event_date="Fri, 22 May 2026",
    )
    assert e.direction == "up"
    assert len(e.sources) == 1
    s = e.sources[0]
    assert s["type"] == "news_keyword"
    assert s["direction"] == "up"
    assert s["locale"] == "en"
    assert s["headline_hash"]
    assert s["as_of"] == "2026-05-24"
    assert e.last_event_date == "Fri, 22 May 2026"


def test_build_entry_handles_garbage_first_seen() -> None:
    """Bad ledger entry must not crash the run."""
    matches = match_headlines(["上方修正発表"])
    e = build_ticker_entry(
        ticker="NVDA",
        matches=matches,
        prior_mentions_count=3,
        first_seen_at="not-a-date",
        today=date(2026, 5, 24),
    )
    # days_since_first_seen falls back to 0 → freshness bonus applies.
    assert e.surprise_score > 0.0


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------


def test_ledger_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_mention_ledger(tmp_path / "nope.jsonl") == []


def test_ledger_respects_30_day_cutoff(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    today = date(2026, 5, 24)
    rows = [
        {"ticker": "NVDA", "seen_at": (today - timedelta(days=5)).isoformat(), "headline_hash": "x"},
        {"ticker": "NVDA", "seen_at": (today - timedelta(days=35)).isoformat(), "headline_hash": "y"},
        {"ticker": "NVDA", "seen_at": (today - timedelta(days=29)).isoformat(), "headline_hash": "z"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    keep = load_mention_ledger(p, cutoff=today - timedelta(days=30))
    hashes = {r["headline_hash"] for r in keep}
    assert hashes == {"x", "z"}


def test_ledger_skips_malformed_rows(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    today = date(2026, 5, 24)
    p.write_text(
        "{not valid json\n"
        f'{{"ticker":"NVDA","seen_at":"{today.isoformat()}","headline_hash":"good"}}\n'
        '{"ticker":"NVDA","seen_at":"garbage","headline_hash":"bad_date"}\n'
        "\n"
    )
    keep = load_mention_ledger(p, cutoff=today - timedelta(days=30))
    assert len(keep) == 1
    assert keep[0]["headline_hash"] == "good"


def test_append_ledger_uses_safe_writer(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    append_mention_ledger(
        p,
        ticker="NVDA",
        keyword_pattern="上方修正",
        direction="up",
        headline_hash="abc",
        seen_at="2026-05-24T10:00:00+00:00",
    )
    row = json.loads(p.read_text().splitlines()[0])
    assert row == {
        "ticker": "NVDA",
        "keyword": "上方修正",
        "direction": "up",
        "headline_hash": "abc",
        "seen_at": "2026-05-24T10:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# write_revision_state — atomic
# ---------------------------------------------------------------------------


def test_write_state_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "revision_state.json"
    matches = match_headlines(["上方修正発表"])
    entry = build_ticker_entry(
        ticker="9999.T",
        matches=matches,
        prior_mentions_count=0,
        first_seen_at="2026-05-22T05:00:00",
        today=date(2026, 5, 24),
    )
    write_revision_state(p, as_of="2026-05-24T18:00:00Z", entries={"9999.T": entry})
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["as_of"] == "2026-05-24T18:00:00Z"
    assert "9999.T" in data["tickers"]
    e = data["tickers"]["9999.T"]
    assert e["direction"] == "up"
    assert 0.0 <= e["surprise_score"] <= 1.0
    assert 0.0 <= e["priced_in_penalty"] <= 0.6


def test_write_state_leaves_no_tmp_residue(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    write_revision_state(p, as_of="2026-05-24T18:00:00Z", entries={})
    assert not (tmp_path / "state.json.tmp").exists()


# ---------------------------------------------------------------------------
# run — orchestrator end-to-end
# ---------------------------------------------------------------------------


def _news_fixture(tmp_path: Path) -> Path:
    """Realistic ``news_signal_candidates.json`` shaped like production."""
    payload = {
        "generated_at": "2026-05-24T18:00:00Z",
        "total_tickers_scanned": 3,
        "candidates": [
            {
                "ticker": "NVDA",
                "name": "NVIDIA",
                "top_headlines": [
                    "NVIDIA raises full-year guidance on AI demand",
                    "Apple posts strongest China quarter in years",
                ],
                "last_article_at": "Fri, 22 May 2026",
            },
            {
                "ticker": "9999.T",
                "name": "Example Corp",
                "top_headlines": [
                    "サンプル企業 通期業績予想を上方修正",
                    "サンプル企業が配当性向の引き上げを発表",
                ],
                "last_article_at": "Thu, 21 May 2026",
            },
            {
                "ticker": "BA",
                "name": "Boeing",
                "top_headlines": [
                    "Boeing cuts guidance amid supply issues",
                ],
                "last_article_at": "Wed, 20 May 2026",
            },
            {
                "ticker": "AAPL",
                "name": "Apple",
                "top_headlines": [
                    "Apple posts strongest China quarter in years",
                    "iPhone shipments surge 20% in Q1",
                ],
                "last_article_at": "Fri, 17 Apr 2026",
            },
        ],
    }
    p = tmp_path / "news_signal_candidates.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return p


def test_run_emits_one_entry_per_matched_ticker(tmp_path: Path) -> None:
    entries = run(
        news_path=_news_fixture(tmp_path),
        state_path=tmp_path / "revision_state.json",
        ledger_path=tmp_path / "ledger.jsonl",
        today=date(2026, 5, 24),
    )
    assert set(entries.keys()) == {"NVDA", "9999.T", "BA"}
    # AAPL has no revision keyword → not in output.
    assert "AAPL" not in entries
    assert entries["NVDA"].direction == "up"
    assert entries["9999.T"].direction == "up"
    assert entries["BA"].direction == "down"


def test_run_writes_state_file_and_ledger(tmp_path: Path) -> None:
    state_path = tmp_path / "revision_state.json"
    ledger_path = tmp_path / "ledger.jsonl"
    run(
        news_path=_news_fixture(tmp_path),
        state_path=state_path,
        ledger_path=ledger_path,
        today=date(2026, 5, 24),
    )
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert "9999.T" in data["tickers"]
    # Ledger has at least 3 rows (one per matched headline).
    ledger_rows = [json.loads(l) for l in ledger_path.read_text().splitlines()]
    assert len(ledger_rows) >= 3
    assert {r["ticker"] for r in ledger_rows} >= {"NVDA", "9999.T", "BA"}


def test_run_prior_mentions_decay_surprise_on_second_day(tmp_path: Path) -> None:
    """Second-day run with same headline → prior_mentions_count grows,
    surprise_score drops."""
    news = _news_fixture(tmp_path)
    state = tmp_path / "revision_state.json"
    ledger = tmp_path / "ledger.jsonl"

    day_one = run(news_path=news, state_path=state, ledger_path=ledger, today=date(2026, 5, 24))
    s1 = day_one["NVDA"].surprise_score
    p1 = day_one["NVDA"].prior_mentions_count

    day_two = run(news_path=news, state_path=state, ledger_path=ledger, today=date(2026, 5, 25))
    s2 = day_two["NVDA"].surprise_score
    p2 = day_two["NVDA"].prior_mentions_count

    assert p1 == 0
    assert p2 >= 1
    assert s2 < s1, "surprise must decay as prior mentions accumulate"


def test_run_same_day_double_run_does_not_inflate_prior_count(tmp_path: Path) -> None:
    """Re-running the cron the same day must keep prior_mentions_count
    stable — only strictly-prior days are counted."""
    news = _news_fixture(tmp_path)
    state = tmp_path / "revision_state.json"
    ledger = tmp_path / "ledger.jsonl"
    today = date(2026, 5, 24)

    first = run(news_path=news, state_path=state, ledger_path=ledger, today=today)
    second = run(news_path=news, state_path=state, ledger_path=ledger, today=today)

    # Both runs see the same prior count (today's appends are excluded).
    assert first["NVDA"].prior_mentions_count == 0
    assert second["NVDA"].prior_mentions_count == 0


def test_run_same_day_double_run_does_not_duplicate_ledger_rows(tmp_path: Path) -> None:
    """Codex Round 12 P2 #3 — same-day re-run must NOT append duplicate
    ledger rows; otherwise tomorrow's prior_mentions_count is inflated."""
    news = _news_fixture(tmp_path)
    state = tmp_path / "revision_state.json"
    ledger = tmp_path / "ledger.jsonl"
    today = date(2026, 5, 24)

    run(news_path=news, state_path=state, ledger_path=ledger, today=today)
    rows_after_first = ledger.read_text(encoding="utf-8").splitlines()

    run(news_path=news, state_path=state, ledger_path=ledger, today=today)
    rows_after_second = ledger.read_text(encoding="utf-8").splitlines()

    # Second same-day run must add zero rows (idempotent).
    assert rows_after_first == rows_after_second


def test_count_prior_counts_distinct_headline_hashes(tmp_path: Path) -> None:
    """Codex Round 12 P2 #3 — a single catalyst event repeated across N
    days should count as 1 prior, not N. Aggregating distinct
    headline_hashes keeps the score stable."""
    from almanac.observability.revision_tracker import _count_prior  # noqa: WPS433

    today = date(2026, 5, 24)
    ledger = [
        # Same headline mentioned on three different days → 1 distinct hash.
        {"ticker": "NVDA", "headline_hash": "h1",
         "seen_at": (today - timedelta(days=5)).isoformat()},
        {"ticker": "NVDA", "headline_hash": "h1",
         "seen_at": (today - timedelta(days=3)).isoformat()},
        {"ticker": "NVDA", "headline_hash": "h1",
         "seen_at": (today - timedelta(days=1)).isoformat()},
        # A second distinct headline → 1 more.
        {"ticker": "NVDA", "headline_hash": "h2",
         "seen_at": (today - timedelta(days=2)).isoformat()},
    ]
    count, first = _count_prior(ledger, ticker="NVDA", today=today)
    assert count == 2, f"expected 2 distinct priors, got {count}"
    # earliest seen_at is the 5-days-ago row.
    assert first.startswith((today - timedelta(days=5)).isoformat())


def test_count_prior_legacy_rows_without_headline_hash_count_once(tmp_path: Path) -> None:
    """Rows lacking ``headline_hash`` (legacy / partial writers) must not
    be silently dropped — they collapse to a single sentinel bucket so
    priors are at least counted once."""
    from almanac.observability.revision_tracker import _count_prior  # noqa: WPS433

    today = date(2026, 5, 24)
    ledger = [
        {"ticker": "NVDA", "seen_at": (today - timedelta(days=5)).isoformat()},
        {"ticker": "NVDA", "seen_at": (today - timedelta(days=3)).isoformat()},
    ]
    count, _ = _count_prior(ledger, ticker="NVDA", today=today)
    assert count == 1  # both legacy rows collapse to the sentinel bucket


def test_run_does_not_call_snapshot_provider_when_not_supplied(tmp_path: Path) -> None:
    """Default config (no snapshot_dir, no provider) must skip snapshots."""
    snap_dir = tmp_path / "snapshots"
    run(
        news_path=_news_fixture(tmp_path),
        state_path=tmp_path / "revision_state.json",
        ledger_path=tmp_path / "ledger.jsonl",
        today=date(2026, 5, 24),
        snapshot_dir=snap_dir,
        snapshot_provider=None,
    )
    # snapshot_dir is supplied but provider is None → directory not created.
    assert not snap_dir.exists()


def test_run_snapshot_provider_writes_one_file_per_match(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snapshots"
    called: list[str] = []

    def provider(ticker: str) -> dict:
        called.append(ticker)
        return {"price": 100.0, "ticker": ticker}

    run(
        news_path=_news_fixture(tmp_path),
        state_path=tmp_path / "revision_state.json",
        ledger_path=tmp_path / "ledger.jsonl",
        today=date(2026, 5, 24),
        snapshot_dir=snap_dir,
        snapshot_provider=provider,
    )
    # Provider called once per matched ticker.
    assert set(called) == {"NVDA", "9999.T", "BA"}
    snap_files = sorted(snap_dir.glob("*_2026-05-24.json"))
    assert {p.name for p in snap_files} == {
        "NVDA_2026-05-24.json",
        "9999.T_2026-05-24.json",
        "BA_2026-05-24.json",
    }


def test_run_swallows_snapshot_provider_exception(tmp_path: Path) -> None:
    """Snapshot is forward-looking only; failure must not abort the run."""
    def boom(ticker: str) -> dict:
        raise RuntimeError("yfinance unreachable")

    entries = run(
        news_path=_news_fixture(tmp_path),
        state_path=tmp_path / "revision_state.json",
        ledger_path=tmp_path / "ledger.jsonl",
        today=date(2026, 5, 24),
        snapshot_dir=tmp_path / "snapshots",
        snapshot_provider=boom,
    )
    # Run completed; revision_state still emitted.
    assert entries
    assert (tmp_path / "revision_state.json").exists()


def test_run_rejects_malformed_news_payload(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"candidates": "not a list"}))
    with pytest.raises(ValueError, match="'candidates' must be a list"):
        run(
            news_path=p,
            state_path=tmp_path / "out.json",
            ledger_path=tmp_path / "ledger.jsonl",
            today=date(2026, 5, 24),
        )


def test_run_handles_empty_candidates_list(tmp_path: Path) -> None:
    p = tmp_path / "news.json"
    p.write_text(json.dumps({"candidates": []}))
    entries = run(
        news_path=p,
        state_path=tmp_path / "out.json",
        ledger_path=tmp_path / "ledger.jsonl",
        today=date(2026, 5, 24),
    )
    assert entries == {}
    # State file still written so consumers can rely on its existence.
    data = json.loads((tmp_path / "out.json").read_text())
    assert data["tickers"] == {}

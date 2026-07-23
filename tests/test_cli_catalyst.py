"""Tests for almanac.cli.catalyst.

The CLI is a thin wrapper, but enough cron entries depend on its exit
codes and skip semantics that drift would be silently corrosive.
Coverage focuses on:

- Opt-in gate: ALMANAC_ENABLE_CATALYST=1 OR --force enables; otherwise
  every subcommand prints a skip notice and exits 0.
- Each subcommand happy path with handcrafted fixtures.
- File-not-found / malformed-input paths return exit code 2.
- The pipeline subcommand aborts at the first non-zero step.
- Each subcommand surfaces its results as one stdout line (cron-friendly).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterator

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.cli.catalyst import ENV_FLAG, main  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts with the env flag cleared. Tests that need it on
    set it explicitly so opt-in/-out is always visible at the call site."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    yield


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A temp worktree-like directory we can pass as --root."""
    return tmp_path


def _news_fixture(root: Path) -> Path:
    p = root / "news_signal_candidates.json"
    p.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-24T18:00:00Z",
                "candidates": [
                    {
                        "ticker": "NVDA",
                        "top_headlines": [
                            "NVIDIA raises full-year guidance on AI demand",
                            "Other unrelated headline",
                        ],
                    },
                    {
                        "ticker": "9999.T",
                        "top_headlines": ["サンプル企業 通期業績予想を上方修正"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


def _beliefs_fixture(root: Path) -> Path:
    p = root / "beliefs" / "agent_beliefs.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "version": "1.0",
                "last_updated": "2026-05-24T00:00:00Z",
                "beliefs": [
                    {
                        "id": "b1",
                        "ticker": "NVDA",
                        "theme": "x",
                        "conviction_score": 60,
                        "base_conviction": 60,
                        "adjusted_conviction": 60,
                        "adjustment_log": [],
                        "expires_at": "2099-12-31T00:00:00",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return p


def _ai_analysis_fixture(root: Path) -> Path:
    p = root / "ai_portfolio_analysis.json"
    p.write_text(
        json.dumps(
            {
                "long_analysis": {
                    "priority_actions": [
                        {"rank": 1, "urgency": "high", "type": "buy",
                         "ticker": "NVDA", "action": "buy", "reason": "AI",
                         "confidence_pct": 75, "tier": "Long"},
                    ],
                },
                "synthesis": {
                    "priority_actions": [
                        {"rank": 1, "urgency": "high", "type": "buy",
                         "ticker": "NVDA", "action": "buy", "reason": "AI",
                         "confidence_pct": 80},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return p


def _proxy_seed_fixture(root: Path) -> Path:
    p = root / "proxy_seed_map.json"
    p.write_text(json.dumps({"openai": ["NVDA", "MSFT"]}), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Opt-in gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand",
    [
        "revision",
        "invalidate",
        "catalyst",
        "outcomes",
        "scenario-promotion",
        "reliability",
        "pipeline",
    ],
)
def test_subcommand_skips_when_env_flag_off(
    subcommand: str, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No env flag, no --force → exit 0 with one-line skip notice."""
    rc = main(["--root", str(root), subcommand])
    assert rc == 0
    out = capsys.readouterr().out
    # Pipeline calls revision first, so its skip notice is what shows up.
    expected = "revision" if subcommand == "pipeline" else subcommand
    assert f"[{expected}] skipped" in out
    assert "ALMANAC_ENABLE_CATALYST!=1" in out


def test_force_flag_overrides_env_off(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _news_fixture(root)
    rc = main(["--root", str(root), "--force", "revision"])
    assert rc == 0
    assert "[revision] today=" in capsys.readouterr().out


def test_env_flag_enables_subcommand(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _news_fixture(root)
    monkeypatch.setenv(ENV_FLAG, "1")
    rc = main(["--root", str(root), "revision"])
    assert rc == 0
    assert "[revision] today=" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# revision
# ---------------------------------------------------------------------------


def test_revision_happy_path(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _news_fixture(root)
    rc = main(["--root", str(root), "--force", "--today", "2026-05-24", "revision"])
    assert rc == 0
    line = capsys.readouterr().out.strip()
    assert "[revision] today=2026-05-24" in line
    assert "tickers=2" in line
    assert "up=2" in line
    # State file was written.
    state = json.loads((root / "revision_state.json").read_text(encoding="utf-8"))
    assert "NVDA" in state["tickers"]
    assert "9999.T" in state["tickers"]


def test_revision_missing_news_returns_2(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--root", str(root), "--force", "revision"])
    assert rc == 2
    assert "news input not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------


def test_invalidate_without_snapshot_only_runs_expired(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _beliefs_fixture(root)  # belief expires in 2099 → expiry won't fire
    rc = main(["--root", str(root), "--force", "--today", "2026-05-24", "invalidate"])
    assert rc == 0
    line = capsys.readouterr().out.strip()
    assert "[invalidate] today=2026-05-24" in line
    assert "wrote=0" in line
    assert "snapshot tickers=0" in line


def test_invalidate_with_snapshot_fires_ma20_rule(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _beliefs_fixture(root)
    snap = root / "market_snapshot.json"
    # price < ma20 → MA20-break delta fires
    snap.write_text(json.dumps({"NVDA": {"price": 100.0, "ma20": 120.0}}))
    rc = main([
        "--root", str(root), "--force", "--today", "2026-05-24",
        "invalidate", "--market-snapshot", str(snap),
    ])
    assert rc == 0
    line = capsys.readouterr().out.strip()
    assert "wrote=1" in line
    # Adjustment row was written
    adj = root / "belief_adjustments.jsonl"
    rows = [json.loads(line) for line in adj.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["reason"] == "invalidation:ma20_break"


def test_invalidate_missing_beliefs_returns_2(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--root", str(root), "--force", "invalidate"])
    assert rc == 2
    assert "beliefs input not found" in capsys.readouterr().err


def test_invalidate_missing_snapshot_returns_2(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _beliefs_fixture(root)
    rc = main([
        "--root", str(root), "--force", "invalidate",
        "--market-snapshot", str(root / "nope.json"),
    ])
    assert rc == 2
    assert "market snapshot not found" in capsys.readouterr().err


def test_invalidate_malformed_snapshot_returns_2(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _beliefs_fixture(root)
    snap = root / "snap.json"
    snap.write_text(json.dumps([1, 2, 3]))  # list, not dict
    rc = main([
        "--root", str(root), "--force", "invalidate",
        "--market-snapshot", str(snap),
    ])
    assert rc == 2
    assert "snapshot must be a dict" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# catalyst
# ---------------------------------------------------------------------------


def test_catalyst_happy_path_writes_log(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ai_analysis_fixture(root)
    _proxy_seed_fixture(root)
    rc = main([
        "--root", str(root), "--force", "--today", "2026-05-24",
        "catalyst", "--top-n", "3",
    ])
    assert rc == 0
    line = capsys.readouterr().out.strip()
    assert "[catalyst] today=2026-05-24" in line
    assert "total=" in line and "top=" in line
    log = root / "catalyst_hypothesis_log.jsonl"
    assert log.exists()
    # At least one event was written from the legacy ai_portfolio_analysis input
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(rows) >= 1


def test_catalyst_dry_run_skips_write(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ai_analysis_fixture(root)
    rc = main([
        "--root", str(root), "--force", "--today", "2026-05-24",
        "catalyst", "--dry-run",
    ])
    assert rc == 0
    assert "(dry-run)" in capsys.readouterr().out
    assert not (root / "catalyst_hypothesis_log.jsonl").exists()


def test_catalyst_with_no_inputs_returns_empty_run(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When no input files exist, the run still completes (zero hypotheses)."""
    rc = main([
        "--root", str(root), "--force", "--today", "2026-05-24",
        "catalyst",
    ])
    assert rc == 0
    line = capsys.readouterr().out.strip()
    assert "total=0" in line


# ---------------------------------------------------------------------------
# reliability / scenario-promotion
# ---------------------------------------------------------------------------


def test_reliability_with_empty_logs_writes_snapshot(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--root", str(root), "--force", "reliability"])
    assert rc == 0
    line = capsys.readouterr().out.strip()
    assert "[reliability]" in line
    out = root / "agent_reliability.json"
    assert out.exists()


def test_scenario_promotion_with_empty_logs_writes_snapshot(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--root", str(root), "--force", "scenario-promotion"])
    assert rc == 0
    line = capsys.readouterr().out.strip()
    assert "[scenario-promotion]" in line
    out = root / "scenario_promotion_summary.json"
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["by_scenario"] == {}


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def test_pipeline_runs_all_observability_subcommands(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _news_fixture(root)
    _beliefs_fixture(root)
    _ai_analysis_fixture(root)
    _proxy_seed_fixture(root)
    rc = main([
        "--root", str(root), "--force", "--today", "2026-05-24", "pipeline",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Every step emitted its summary line.
    for step in (
        "revision",
        "invalidate",
        "catalyst",
        "outcomes",
        "scenario-promotion",
        "reliability",
    ):
        assert f"[{step}]" in out, f"step {step} did not log a summary"


def test_pipeline_aborts_on_first_failure(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Don't create the news fixture → revision returns 2 → pipeline aborts.
    rc = main([
        "--root", str(root), "--force", "--today", "2026-05-24", "pipeline",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "[pipeline] aborted at revision" in err


def test_pipeline_respects_opt_out(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without env or --force, every step skips and the pipeline returns 0."""
    rc = main(["--root", str(root), "pipeline"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[revision] skipped" in out


# ---------------------------------------------------------------------------
# Argument routing
# ---------------------------------------------------------------------------


def test_subcommand_required() -> None:
    """No subcommand → argparse exits 2 (its convention)."""
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_unknown_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["unknown"])
    assert exc.value.code == 2


def test_today_iso_override(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _news_fixture(root)
    rc = main([
        "--root", str(root), "--force", "--today", "2030-01-15", "revision",
    ])
    assert rc == 0
    assert "today=2030-01-15" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


def _catalyst_hypothesis_fixture(
    root: Path,
    *,
    event_at: str = "2026-05-01T09:00:00",
    hypothesis_id: str = "h1",
    ticker: str = "NVDA",
    price_at_event: float | None = 100.0,
) -> Path:
    """Write a minimal catalyst_hypothesis_log.jsonl for outcome tests."""
    from almanac.observability.logs import write_catalyst_hypothesis_generated

    hlog = root / "catalyst_hypothesis_log.jsonl"
    write_catalyst_hypothesis_generated(
        hlog,
        hypothesis_id=hypothesis_id,
        analysis_id="a1",
        analysis_date=event_at[:10],
        event_at=event_at,
        hypothesis_type="bull_pullback",
        primary_ticker=ticker,
        catalyst_score=0.7,
        scenario_readiness=0.6,
        priced_in_penalty=0.1,
        surprise_score=0.5,
        gross_expected_return_bps=200,
        conviction_at_generation=70,
        price_at_event=price_at_event,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_price_at_event={"QQQ": 400.0},
        usdjpy_at_event=156.0,
        fsync=False,
    )
    return hlog


class _FakePriceProvider:
    """In-process stub — no yfinance HTTP calls."""

    def __init__(self, prices: dict[tuple[str, str], float]) -> None:
        self._prices = {(t.upper(), d): p for (t, d), p in prices.items()}

    def price_on_or_after(self, ticker: str, after_date: object) -> float | None:
        key = (str(ticker).upper(), str(after_date))
        return self._prices.get(key)


def test_outcomes_skips_when_no_horizons_due(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When today is only 1 day after event, horizon=3 is not yet due → 0 written."""
    # event_at is yesterday; horizon=3 business days is not due until 3 bdays later.
    _catalyst_hypothesis_fixture(root, event_at="2026-05-24T09:00:00")
    rc = main([
        "--root", str(root), "--force", "--today", "2026-05-25",
        "outcomes", "--horizons", "3",
    ])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert "[outcomes] today=2026-05-25 catalyst=0 sell=0 horizons=3" in out
    # No outcome log created for empty run.
    assert not (root / "catalyst_outcome_log.jsonl").exists()


def test_outcomes_happy_path_writes_due_row(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When a horizon is due and prices are available, one outcome row is written."""
    # event_at=2026-05-01 (Friday); horizon=3 bdays → measure_date=2026-05-06 (Wed).
    # today=2026-05-25 > measure_date → row is due.
    _catalyst_hypothesis_fixture(root, event_at="2026-05-01T09:00:00")

    import almanac.observability.outcome_updater as ou

    fake = _FakePriceProvider({
        ("NVDA", "2026-05-06"): 110.0,   # +10% from 100.0
        ("QQQ",  "2026-05-06"): 408.0,   # +2% from 400.0
    })
    monkeypatch.setattr(ou, "YFinancePriceProvider", lambda *a, **kw: fake)

    rc = main([
        "--root", str(root), "--force", "--today", "2026-05-25",
        "outcomes", "--horizons", "3",
    ])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert "[outcomes] today=2026-05-25 catalyst=1 sell=0 horizons=3" in out

    olog = root / "catalyst_outcome_log.jsonl"
    assert olog.exists(), "catalyst_outcome_log.jsonl was not created"
    rows = [json.loads(line) for line in olog.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["hypothesis_id"] == "h1"
    assert rows[0]["horizon_days"] == 3
    assert rows[0]["return_pct"] == pytest.approx(0.1, abs=1e-6)
    assert rows[0]["benchmark_return_pct"] == pytest.approx(0.02, abs=1e-6)


def test_outcomes_no_duplicate_on_second_run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running outcomes twice does not double-write already-measured rows."""
    _catalyst_hypothesis_fixture(root, event_at="2026-05-01T09:00:00")

    import almanac.observability.outcome_updater as ou

    fake = _FakePriceProvider({
        ("NVDA", "2026-05-06"): 110.0,
        ("QQQ",  "2026-05-06"): 408.0,
    })
    monkeypatch.setattr(ou, "YFinancePriceProvider", lambda *a, **kw: fake)

    argv = ["--root", str(root), "--force", "--today", "2026-05-25", "outcomes", "--horizons", "3"]
    assert main(argv) == 0
    assert main(argv) == 0
    capsys.readouterr()  # discard both stdout captures

    olog = root / "catalyst_outcome_log.jsonl"
    rows = [json.loads(line) for line in olog.read_text().splitlines() if line.strip()]
    assert len(rows) == 1, "second run must not append a duplicate row"


def test_outcomes_multiple_horizons_writes_multiple_rows(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multiple due horizons each produce one outcome row."""
    # event_at=2026-05-01 → horizon 3 bdays=2026-05-06, horizon 5 bdays=2026-05-08
    _catalyst_hypothesis_fixture(root, event_at="2026-05-01T09:00:00")

    import almanac.observability.outcome_updater as ou

    fake = _FakePriceProvider({
        ("NVDA", "2026-05-06"): 110.0,
        ("NVDA", "2026-05-08"): 112.0,
        ("QQQ",  "2026-05-06"): 408.0,
        ("QQQ",  "2026-05-08"): 412.0,
    })
    monkeypatch.setattr(ou, "YFinancePriceProvider", lambda *a, **kw: fake)

    rc = main([
        "--root", str(root), "--force", "--today", "2026-05-25",
        "outcomes", "--horizons", "3,5",
    ])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert "catalyst=2" in out
    assert "horizons=3,5" in out

    olog = root / "catalyst_outcome_log.jsonl"
    rows = [json.loads(line) for line in olog.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    horizons_written = {r["horizon_days"] for r in rows}
    assert horizons_written == {3, 5}


def test_catalyst_wires_disclosure_features(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Go-live 配線: data/disclosure_features.jsonl があれば catalyst run が
    observe_only な disclosure 仮説を log に書く (他入力ゼロなので total は disclosure 由来のみ)。"""
    from almanac.observability.disclosure_features import make_feature
    feats = {"directional_score": 0.6, "directional_confidence": 0.8,
             "catalyst_specificity": 0.7, "crowding_hype_score": 0.2}
    row = make_feature(
        source="edgar", ticker="AAPL",
        publish_time="2026-05-20T13:00:00+00:00",
        ingest_time="2026-05-20T13:05:00+00:00",
        compute_time="2026-05-20T13:10:00+00:00",
        disclosure_type="earnings", market="US", native_doc_id="acc-1",
        model_id="deepseek-chat", prompt_version="p1",
        summary="raised FY guidance above consensus", features=feats,
    ).to_row()
    ddir = root / "data"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "disclosure_features.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    rc = main(["--root", str(root), "--force", "--today", "2026-06-01", "catalyst"])
    assert rc == 0
    assert "total=0" not in capsys.readouterr().out
    log = root / "catalyst_hypothesis_log.jsonl"
    assert log.exists()
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(rows) >= 1

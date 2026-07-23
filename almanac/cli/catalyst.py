"""Command-line entry points for the catalyst observability pipeline.

A thin wrapper over the underlying ``almanac.observability`` runners so
the daily cron can invoke each step independently. Subcommands:

  revision     — run revision_tracker against news_signal_candidates.json
  invalidate   — run invalidation_rules against agent_beliefs.json
  catalyst     — run catalyst_layer.run and write the hypothesis log
  outcomes     — append due catalyst/sell outcome rows
  scenario-promotion
               — snapshot scenario promotion stats from catalyst outcomes
  reliability  — snapshot agent_reliability.json from append-only logs
  pipeline     — run all above in dependency order

Conventions
-----------

- **Opt-in gate**: every subcommand checks ``ALMANAC_ENABLE_CATALYST=1``
  in the environment. Without the flag the command logs a one-line
  notice and exits 0 (cron-friendly — no spurious failures while the
  layer is dormant). Pass ``--force`` to override the gate for manual
  testing / back-fill.
- **Exit codes**: 0 on success or opt-out skip; 2 on validation / I/O
  failure; 1 on unexpected runtime error.
- **Output streams**: human-readable run summaries go to stdout
  (one line per subcommand result); diagnostics / errors go to stderr.
- **Paths**: every input/output path defaults to the worktree root.
  Override with ``--root <dir>`` or per-path flags so the same cli can
  drive both production and a back-fill rig.

Example cron entries::

    ALMANAC_ENABLE_CATALYST=1
    # daily 18:00  — refresh revision_state from news_signal_candidates
    0 18 * * 1-5  cd ~/portfolio-bot && python -m almanac.cli.catalyst revision
    # daily 18:15  — apply invalidation deltas to beliefs adjustments
    15 18 * * 1-5 cd ~/portfolio-bot && python -m almanac.cli.catalyst invalidate
    # daily 18:30  — synthesise the catalyst hypothesis log + top-N
    30 18 * * 1-5 cd ~/portfolio-bot && python -m almanac.cli.catalyst catalyst
    # daily 18:50  — append due outcomes and refresh scenario promotion view
    50 18 * * 1-5 cd ~/portfolio-bot && python -m almanac.cli.catalyst outcomes
    55 18 * * 1-5 cd ~/portfolio-bot && python -m almanac.cli.catalyst scenario-promotion
    # weekly Sun  — refresh agent_reliability snapshot
    0 7 * * 0     cd ~/portfolio-bot && python -m almanac.cli.catalyst reliability
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from almanac.runtime_config import get_env

__all__ = [
    "ENV_FLAG",
    "main",
    "cmd_revision",
    "cmd_invalidate",
    "cmd_catalyst",
    "cmd_outcomes",
    "cmd_scenario_promotion",
    "cmd_reliability",
    "cmd_pipeline",
]

#: Env var that gates every subcommand. Without it (and without ``--force``)
#: the command skips and exits 0.
ENV_FLAG = "ALMANAC_ENABLE_CATALYST"

logger = logging.getLogger("almanac.cli.catalyst")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_gate(args: argparse.Namespace, subcommand: str) -> bool:
    """Return True when the subcommand is allowed to run.

    Honours ``ALMANAC_ENABLE_CATALYST=1`` and ``--force``. Emits a
    one-line stdout notice on skip so cron logs show the opt-out
    explicitly.
    """
    if getattr(args, "force", False):
        return True
    if get_env(ENV_FLAG) == "1":
        return True
    print(
        f"[{subcommand}] skipped: {ENV_FLAG}!=1 and --force not set",
        file=sys.stdout,
        flush=True,
    )
    return False


def _resolve(args: argparse.Namespace, name: str, default: str) -> Path:
    """Resolve a path from CLI args, falling back to ``root / default``."""
    explicit = getattr(args, name, None)
    if explicit:
        return Path(explicit)
    return Path(args.root) / default


def _today_iso(args: argparse.Namespace) -> str:
    """Return the analysis date — CLI override beats wall clock."""
    if getattr(args, "today", None):
        return args.today
    return date.today().isoformat()


def _load_optional_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_revision(args: argparse.Namespace) -> int:
    """``revision`` — refresh revision_state from news_signal_candidates."""
    if not _check_gate(args, "revision"):
        return 0
    from almanac.observability.revision_tracker import run as revision_run

    news = _resolve(args, "news_path", "news_signal_candidates.json")
    state = _resolve(args, "state_path", "revision_state.json")
    ledger = _resolve(args, "ledger_path", "revision_mention_ledger.jsonl")

    if not news.exists():
        print(f"[revision] news input not found: {news}", file=sys.stderr)
        return 2

    today = _today_iso(args)
    try:
        entries = revision_run(
            news_path=news,
            state_path=state,
            ledger_path=ledger,
            today=date.fromisoformat(today),
        )
    except (ValueError, OSError) as exc:
        print(f"[revision] failed: {exc}", file=sys.stderr)
        return 2

    n_up = sum(1 for e in entries.values() if e.direction == "up")
    n_down = sum(1 for e in entries.values() if e.direction == "down")
    print(
        f"[revision] today={today} tickers={len(entries)} up={n_up} down={n_down} "
        f"state={state}",
        flush=True,
    )
    return 0


def cmd_invalidate(args: argparse.Namespace) -> int:
    """``invalidate`` — apply belief invalidation rules.

    Market data is optional: without ``--market-snapshot`` only the
    ``expired`` rule fires (the RSI / MA20 checks need price data and
    silently skip without it).
    """
    if not _check_gate(args, "invalidate"):
        return 0
    from almanac.observability.invalidation_rules import (
        MarketIndicators,
        apply_invalidations,
    )

    beliefs = _resolve(args, "beliefs_path", "beliefs/agent_beliefs.json")
    adjustments = _resolve(
        args, "adjustments_path", "belief_adjustments.jsonl"
    )

    if not beliefs.exists():
        print(f"[invalidate] beliefs input not found: {beliefs}", file=sys.stderr)
        return 2

    snapshot: dict[str, MarketIndicators] = {}
    snap_path = getattr(args, "market_snapshot", None)
    if snap_path:
        snap_file = Path(snap_path)
        if not snap_file.exists():
            print(
                f"[invalidate] market snapshot not found: {snap_file}",
                file=sys.stderr,
            )
            return 2
        try:
            raw = json.loads(snap_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[invalidate] market snapshot unreadable: {exc}", file=sys.stderr)
            return 2
        if not isinstance(raw, dict):
            print(
                f"[invalidate] market snapshot must be a dict, got "
                f"{type(raw).__name__}",
                file=sys.stderr,
            )
            return 2
        for ticker, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            snapshot[ticker] = MarketIndicators(
                ticker=ticker,
                price=fields.get("price"),
                ma20=fields.get("ma20"),
                rsi_14=fields.get("rsi_14"),
            )

    today = _today_iso(args)
    try:
        row_ids = apply_invalidations(
            beliefs,
            adjustments,
            today=date.fromisoformat(today),
            market_snapshot=snapshot,
        )
    except (ValueError, OSError) as exc:
        print(f"[invalidate] failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"[invalidate] today={today} wrote={len(row_ids)} adjustments "
        f"(snapshot tickers={len(snapshot)})",
        flush=True,
    )
    return 0


def cmd_catalyst(args: argparse.Namespace) -> int:
    """``catalyst`` — synthesise hypotheses + write the catalyst log."""
    if not _check_gate(args, "catalyst"):
        return 0
    from almanac.observability.catalyst_layer import run as catalyst_run

    revision_state = _resolve(args, "revision_state_path", "revision_state.json")
    scenario_state = _resolve(args, "scenario_state_path", "scenario_state.json")
    proxy_map = _resolve(args, "proxy_seed_map_path", "proxy_seed_map.json")
    legacy = _resolve(args, "legacy_analysis_path", "ai_portfolio_analysis.json")
    catalyst_log = _resolve(
        args, "catalyst_log_path", "catalyst_hypothesis_log.jsonl"
    )
    # Go-live wiring: observe_only な disclosure 特徴量を catalyst run に渡し、
    # observe_only hypotheses として記録 → outcome_updater が forward リターンを計測する。
    # ファイル未生成 (未稼働) なら None を渡し no-op (read_features も欠落で空を返す)。
    disclosure_features = _resolve(
        args, "disclosure_features_path", "data/disclosure_features.jsonl"
    )
    screener_payloads = {
        "short": _load_optional_json(_resolve(args, "short_candidates_path", "short_candidates.json")),
        "margin_long": _load_optional_json(
            _resolve(args, "margin_candidates_path", "margin_long_candidates.json")
        ),
        "pair": _load_optional_json(_resolve(args, "pair_candidates_path", "pair_trade_candidates.json")),
        "squeeze": _load_optional_json(_resolve(args, "squeeze_candidates_path", "squeeze_candidates.json")),
    }

    today = _today_iso(args)
    try:
        output = catalyst_run(
            revision_state_path=revision_state if revision_state.exists() else None,
            scenario_state_path=scenario_state if scenario_state.exists() else None,
            proxy_seed_map_path=proxy_map if proxy_map.exists() else None,
            legacy_analysis_path=legacy if legacy.exists() else None,
            catalyst_log_path=catalyst_log,
            disclosure_features_path=disclosure_features if disclosure_features.exists() else None,
            screener_payloads=screener_payloads,
            analysis_id=f"cli-catalyst-{today}",
            analysis_date=today,
            top_n=args.top_n,
            write_log=not args.dry_run,
        )
    except (ValueError, OSError) as exc:
        print(f"[catalyst] failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"[catalyst] today={today} total={output.n_hypotheses_total} "
        f"top={output.n_hypotheses_top} by_type={dict(output.by_type)} "
        f"log={catalyst_log if not args.dry_run else '(dry-run)'}",
        flush=True,
    )
    return 0


def cmd_reliability(args: argparse.Namespace) -> int:
    """``reliability`` — snapshot per-agent stats from append-only logs."""
    if not _check_gate(args, "reliability"):
        return 0
    from almanac.observability.agent_reliability import snapshot_to_file

    attribution = _resolve(
        args, "attribution_log_path", "agent_attribution_log.jsonl"
    )
    outcomes = _resolve(args, "outcome_log_path", "catalyst_outcome_log.jsonl")
    output_path = _resolve(args, "output_path", "agent_reliability.json")

    try:
        snapshot = snapshot_to_file(
            attribution_log_path=attribution,
            outcome_log_path=outcomes,
            output_path=output_path,
            horizon_days=args.horizon_days,
        )
    except (ValueError, OSError) as exc:
        print(f"[reliability] failed: {exc}", file=sys.stderr)
        return 2

    n_agents = len(snapshot.get("agents", {})) if isinstance(snapshot, dict) else 0
    print(
        f"[reliability] horizon={args.horizon_days}d agents={n_agents} "
        f"output={output_path}",
        flush=True,
    )
    return 0


def cmd_scenario_promotion(args: argparse.Namespace) -> int:
    """``scenario-promotion`` — snapshot scenario stats from catalyst outcomes."""
    if not _check_gate(args, "scenario-promotion"):
        return 0
    from almanac.observability.scenario_promotion import snapshot_to_file

    catalyst_log = _resolve(args, "catalyst_log_path", "catalyst_hypothesis_log.jsonl")
    outcome_log = _resolve(args, "outcome_log_path", "catalyst_outcome_log.jsonl")
    output_path = _resolve(args, "output_path", "scenario_promotion_summary.json")
    horizon_days = int(
        getattr(args, "scenario_horizon_days", None)
        or getattr(args, "horizon_days", 20)
    )

    try:
        snapshot = snapshot_to_file(
            hypothesis_log_path=catalyst_log,
            outcome_log_path=outcome_log,
            output_path=output_path,
            primary_horizon_days=horizon_days,
        )
    except (ValueError, OSError) as exc:
        print(f"[scenario-promotion] failed: {exc}", file=sys.stderr)
        return 2

    by_scenario = snapshot.get("by_scenario", {}) if isinstance(snapshot, dict) else {}
    ready = [
        sid for sid, row in by_scenario.items()
        if isinstance(row, dict) and row.get("promotion_ready") is True
    ]
    print(
        f"[scenario-promotion] horizon={horizon_days}d "
        f"scenarios={len(by_scenario)} ready={len(ready)} output={output_path}",
        flush=True,
    )
    return 0


def _parse_horizons(value: str | None) -> tuple[int, ...]:
    """Parse comma-separated horizon days for outcome measurement."""
    if not value:
        return (3, 5, 10, 20, 60)
    horizons: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        horizons.append(int(part))
    return tuple(horizons)


def cmd_outcomes(args: argparse.Namespace) -> int:
    """``outcomes`` — append due catalyst/sell outcome measurements."""
    if not _check_gate(args, "outcomes"):
        return 0
    from almanac.observability.outcome_updater import (
        update_catalyst_outcomes,
        update_sell_outcomes,
    )

    today = _today_iso(args)
    try:
        horizons = _parse_horizons(getattr(args, "horizons", None))
        catalyst = update_catalyst_outcomes(
            hypothesis_log_path=_resolve(
                args, "catalyst_log_path", "catalyst_hypothesis_log.jsonl"
            ),
            outcome_log_path=_resolve(
                args, "catalyst_outcome_log_path", "catalyst_outcome_log.jsonl"
            ),
            today=date.fromisoformat(today),
            horizons=horizons,
        )
        sell = update_sell_outcomes(
            sell_decision_log_path=_resolve(
                args, "sell_decision_log_path", "sell_decision_log.jsonl"
            ),
            sell_outcome_log_path=_resolve(
                args, "sell_outcome_log_path", "sell_outcome_log.jsonl"
            ),
            today=date.fromisoformat(today),
            horizons=horizons,
        )
    except (ValueError, OSError) as exc:
        print(f"[outcomes] failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"[outcomes] today={today} catalyst={catalyst} sell={sell} "
        f"horizons={','.join(map(str, horizons))}",
        flush=True,
    )
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    """``pipeline`` — run revision → invalidate → catalyst → outcomes → reliability.

    A non-zero exit in any step aborts the pipeline and propagates the
    code. Skipped (opt-out) steps return 0 and the pipeline continues.
    """
    for name, fn in (
        ("revision", cmd_revision),
        ("invalidate", cmd_invalidate),
        ("catalyst", cmd_catalyst),
        ("outcomes", cmd_outcomes),
        ("scenario-promotion", cmd_scenario_promotion),
        ("reliability", cmd_reliability),
    ):
        rc = fn(args)
        if rc != 0:
            print(f"[pipeline] aborted at {name} (exit {rc})", file=sys.stderr)
            return rc
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="almanac.cli.catalyst",
        description=(
            "Catalyst observability cron entry points. Subcommands gate on "
            f"{ENV_FLAG}=1 unless --force is given."
        ),
    )
    p.add_argument(
        "--root",
        default=".",
        help="worktree root for default path resolution (default: cwd)",
    )
    p.add_argument(
        "--today",
        help="ISO date to use instead of wall clock (back-fill / testing)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=f"run even if {ENV_FLAG} is not set",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable INFO logging on stderr",
    )

    sub = p.add_subparsers(dest="subcommand", required=True)

    sp = sub.add_parser("revision", help="refresh revision_state from news")
    sp.add_argument("--news-path", dest="news_path")
    sp.add_argument("--state-path", dest="state_path")
    sp.add_argument("--ledger-path", dest="ledger_path")
    sp.set_defaults(func=cmd_revision)

    sp = sub.add_parser("invalidate", help="apply belief invalidation rules")
    sp.add_argument("--beliefs-path", dest="beliefs_path")
    sp.add_argument("--adjustments-path", dest="adjustments_path")
    sp.add_argument(
        "--market-snapshot",
        dest="market_snapshot",
        help="JSON file mapping ticker → {price, ma20, rsi_14}",
    )
    sp.set_defaults(func=cmd_invalidate)

    sp = sub.add_parser("catalyst", help="synthesise catalyst hypothesis log")
    sp.add_argument("--revision-state-path", dest="revision_state_path")
    sp.add_argument("--scenario-state-path", dest="scenario_state_path")
    sp.add_argument("--proxy-seed-map-path", dest="proxy_seed_map_path")
    sp.add_argument("--legacy-analysis-path", dest="legacy_analysis_path")
    sp.add_argument("--catalyst-log-path", dest="catalyst_log_path")
    sp.add_argument("--short-candidates-path", dest="short_candidates_path")
    sp.add_argument("--margin-candidates-path", dest="margin_candidates_path")
    sp.add_argument("--pair-candidates-path", dest="pair_candidates_path")
    sp.add_argument("--squeeze-candidates-path", dest="squeeze_candidates_path")
    sp.add_argument("--top-n", type=int, default=10)
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="skip writing the hypothesis log",
    )
    sp.set_defaults(func=cmd_catalyst)

    sp = sub.add_parser("outcomes", help="append due catalyst/sell outcomes")
    sp.add_argument("--catalyst-log-path", dest="catalyst_log_path")
    sp.add_argument("--catalyst-outcome-log-path", dest="catalyst_outcome_log_path")
    sp.add_argument("--sell-decision-log-path", dest="sell_decision_log_path")
    sp.add_argument("--sell-outcome-log-path", dest="sell_outcome_log_path")
    sp.add_argument(
        "--horizons",
        default="3,5,10,20,60",
        help="comma-separated business-day horizons (default: 3,5,10,20,60)",
    )
    sp.set_defaults(func=cmd_outcomes)

    sp = sub.add_parser("reliability", help="snapshot per-agent reliability")
    sp.add_argument("--attribution-log-path", dest="attribution_log_path")
    sp.add_argument("--outcome-log-path", dest="outcome_log_path")
    sp.add_argument("--output-path", dest="output_path")
    sp.add_argument("--horizon-days", type=int, default=10)
    sp.set_defaults(func=cmd_reliability)

    sp = sub.add_parser(
        "scenario-promotion",
        help="snapshot scenario promotion stats from catalyst outcomes",
    )
    sp.add_argument("--catalyst-log-path", dest="catalyst_log_path")
    sp.add_argument("--outcome-log-path", dest="outcome_log_path")
    sp.add_argument("--output-path", dest="output_path")
    sp.add_argument("--horizon-days", type=int, default=20)
    sp.set_defaults(func=cmd_scenario_promotion)

    sp = sub.add_parser("pipeline", help="run all catalyst observability steps in order")
    # Pipeline inherits any per-step paths through the namespace; passing
    # specific paths here is uncommon so we don't duplicate every flag.
    # If you need overrides, run the subcommands individually.
    sp.add_argument("--top-n", type=int, default=10)
    sp.add_argument("--horizon-days", type=int, default=10)
    sp.add_argument("--scenario-horizon-days", type=int, default=20)
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="skip writing logs (applies to catalyst step only)",
    )
    sp.set_defaults(func=cmd_pipeline)

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the subcommand exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        print("[catalyst] interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level safety net
        logger.exception("unexpected error in %s", args.subcommand)
        print(f"[{args.subcommand}] crashed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

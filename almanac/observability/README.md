# almanac/observability — Catalyst Observability Layer

## Purpose

This package is the **measurement and orchestration layer** for the ALMANAC
catalyst pipeline. It does **not** generate trade signals; it observes the
ones the existing producers already generate, normalises them, scores
them, writes append-only audit logs, and surfaces a top-N list ready for
the daily Opus prompt.

The layer was designed across 12 rounds of structured dialectic (Claude
↔ Codex) recorded in `.claude/plans/codex-claude-ai-sunny-babbage.md`.
Two non-negotiable invariants emerged from that dialectic and constrain
every module here:

1. **Producer surfaces are not rewired.** `analyst/__init__.py` (the
   14×-touched Sonnet/Opus synthesis hot path) and the DeepSeek margin /
   short producers continue to emit their existing JSON formats. This
   layer reads them; it does not modify them.
2. **Logs are strict append-only.** Status transitions, outcome updates,
   and follow-up measurements all enter as additional rows joined by
   stable IDs (`hypothesis_id`, `cash_decision_id`, `sell_decision_id`).
   There is no mutate API. See Round 9 #3.

## Architecture

```
            ┌──────────────────── INPUT SOURCES ────────────────────┐
            │ revision_state.json      scenario_state.json          │
            │ proxy_seed_map.json      agent_beliefs.json (v2)      │
            │ ai_portfolio_analysis.json  signal_history.json       │
            │ news_signal_candidates.json                           │
            └────────────────────────────┬──────────────────────────┘
                                         │
        ┌────────────────────────────────┼────────────────────────────┐
        │                                ▼                            │
        │   INGESTION (pure)                                          │
        │     revision_tracker       — news → revision_state          │
        │     candidate_extractor    — legacy producer → packet       │
        │     proxy_mapper           — entity → ticker (4-layer)      │
        │     proxy_llm_provider     — Sonnet impl of LLMProvider     │
        │                                ▼                            │
        │   INTEGRATION                                               │
        │     catalyst_layer.run()   — synthesise, dedupe, rank       │
        │                                ▼                            │
        │   ADJUSTMENT (background)                                   │
        │     invalidation_rules     — daily belief delta writes      │
        │     regime_shift_detector  — halve reliability on shifts    │
        │     agent_reliability      — per-agent EV snapshots         │
        │                                ▼                            │
        │   PERSISTENCE (append-only JSONL except *.json snapshots)   │
        │     catalyst_hypothesis_log.jsonl   catalyst_outcome_log    │
        │     sell_decision_log.jsonl         sell_outcome_log        │
        │     agent_attribution_log.jsonl     portfolio_decision_log  │
        │     cash_deployment_log.jsonl       belief_adjustments      │
        │     revision_mention_ledger.jsonl   proxy_audit_log         │
        │     agent_reliability.json (daily snapshot)                 │
        │     revision_state.json (daily snapshot)                    │
        │                                ▼                            │
        │   ANALYTICS (read-side, pure)                               │
        │     verifier_extensions    — EV/payoff/excess aggregation   │
        └─────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
              CatalystOutput.top  →  analyzer.py Opus prompt
                                 (single-file surgical integration —
                                  out of scope for this package)
```

## Modules at a glance

| Module | Purpose | Key public exports |
|---|---|---|
| `ids` | Stable + ephemeral ID generation | `compute_hypothesis_id`, `new_row_id`, `new_analysis_id`, `new_cash_decision_id` |
| `status` | 3-axis status taxonomy | `CandidateStatus`, `ExecutionState`, `PortfolioDecisionState` |
| `append_only_log` | flock+fsync JSONL writer + currency helpers | `append_jsonl_safe`, `normalize_to_jpy`, `normalize_to_usd`, `MeasurementQuality` |
| `logs` | 11 typed writers (catalyst / sell / attribution / portfolio / cash / belief) | `write_catalyst_hypothesis_generated`, `write_catalyst_outcome`, `write_sell_decision`, `write_agent_attribution`, `write_portfolio_decision`, `write_cash_critic_triggered`, `write_belief_adjustment` (+ 4 more) |
| `signal_history` | Back-compat reader/writer for the legacy `signal_history.json` | `apply_legacy_defaults`, `make_record`, `read_history` |
| `invalidation_rules` | 3 deterministic belief invalidation rules + idempotent runner | `check_expired`, `check_rsi_overheat`, `check_ma20_break`, `evaluate_beliefs`, `apply_invalidations` |
| `revision_tracker` | EN/JP keyword detection + surprise / priced-in scoring + mention ledger | `match_headlines`, `compute_surprise_score`, `compute_priced_in_penalty`, `run` |
| `candidate_extractor` | READ-side adapter from legacy producers → `candidate_packet` | `extract_from_sonnet_tier`, `extract_from_synthesis`, `extract_from_catalyst_layer`, `extract_all` |
| `proxy_mapper` | 4-layer proxy mapping (seed → LLM propose → LLM skeptic → self-consistency) | `LLMProvider`, `ProxyResult`, `propose_proxies`, `lookup_seed`, `jaccard_intersection` |
| `proxy_llm_provider` | Production Sonnet `LLMProvider` implementation | `SonnetProxyProvider`, `default_llm_call`, `TICKER_REGEX` |
| `agent_reliability` | Per-agent EV stats with n-threshold-gated reliability weight | `aggregate_agent_reliability`, `derive_weight`, `snapshot_to_file` |
| `scenario_promotion` | Scenario-level promotion view from catalyst outcomes | `aggregate_scenario_promotion`, `snapshot_to_file` |
| `regime_shift_detector` | Macro regime change detection → reliability_weight halving | `RegimeShift`, `RegimeShiftReport`, `detect_shift`, `compute_active_multiplier`, `run` |
| `catalyst_layer` | **Crown jewel** orchestrator — synthesise → dedupe → rank | `CatalystHypothesis`, `CatalystOutput`, `compute_catalyst_score`, `synthesize_from_*`, `dedupe_by_hypothesis_id`, `rank_by_catalyst_score`, `run` |
| `verifier_extensions` | EV / payoff / excess-return rollups from the append-only logs | `read_hypothesis_events`, `read_outcomes`, `compute_group_stats`, `aggregate_by_dimensions`, `summarize` |

## Locked invariants

The 12 rules below are pinned by tests; **changing them requires
updating both the test and a docstring R-citation**.

- **R8 #1 / R9 #1** — `hypothesis_id` is date-independent. Same logical
  hypothesis on day 1 and day 30 yields the same id, which is the join
  key for opportunity-cost and attribution aggregation. Enforced by an
  `inspect.signature` assertion in `tests/test_observability_ids.py`.
- **R8 #2 / R9 #2** — `user_not_executed` lives only on `ExecutionState`,
  never on `CandidateStatus`. The 3 axes are disjoint by membership tests.
- **R9 #3** — Append-only discipline. No `update_*` / `mutate_*` /
  `patch_*` API exists in `logs.py.__all__`; a meta-test guards this.
  Status transitions and outcomes enter as new rows joined by ID.
- **R9 #6** — Currency normalisation required when a benchmark basket
  mixes currencies. `normalize_to_jpy` / `normalize_to_usd` raise on
  unsupported codes — silent degradation is dangerous.
- **R9 #7** — `surprise_score` and `priced_in_penalty` are scoring-only,
  never hard filters. The penalty is capped at 0.6 (not 1.0) so a strong
  momentum signal cannot be vetoed by "priced-in" alone.
- **R11 #1** — `agent_attribution` rows are flat: one row per agent per
  hypothesis. Reports rebuild the `agents` list via group-by, never via
  mutate. Regression test forbids any `agents` array field on row.
- **R11 #4** — `no_action` is excluded from `CandidateStatus` and
  `ExecutionState`. It belongs to `PortfolioDecisionState` only.
  `infer_action_type("no_action")` returns `None`.
- **R11 #C** — `bull_pullback` and every `event_playbook` entry carry
  a two-axis enable flag: `enabled_for_decision` (gate) and
  `observe_only` (log-but-do-not-act). Both default to safe values.
- **R12 P1 #1** — `invalidation_rules.apply_invalidations` is idempotent
  per `(belief_id, reason, day)`. A re-run of the daily cron does not
  stack duplicate `-10/-15` deltas onto the same belief.
- **R12 P1 #2** — Every monetary amount in playbooks carries an explicit
  `currency` field. `.T` tickers are `JPY`; others are `USD`. A test
  rejects any entry that lacks `currency` or uses the deprecated
  `allocation_usd` key.
- **R12 P2 #3** — `revision_tracker` mention ledger is same-day
  idempotent (no duplicate appends). `_count_prior` collapses to
  distinct `headline_hash` values so multi-day repeats of the same
  catalyst count as 1 prior.
- **R12 P2 #4** — `verifier_extensions.aggregate_by_dimensions` falls
  back to `primary_ticker` when `ticker` is absent (the catalyst-layer
  rows use `primary_ticker`).

## Data contracts

All logs live at the worktree / repo root. Schemas are defined in plan
sections; the writers in `logs.py` enforce required fields.

### Append-only JSONL

| File | Plan §  | Writer | Notes |
|---|---|---|---|
| `catalyst_hypothesis_log.jsonl` | §6.6 | `write_catalyst_hypothesis_{generated,status_transition,filtered}` | event-typed; never mutated — outcomes go to a separate log |
| `catalyst_outcome_log.jsonl` | §6.14 | `write_catalyst_outcome` | joined to hypothesis log by `hypothesis_id × horizon_days` |
| `sell_decision_log.jsonl` | §6.8 | `write_sell_decision` | recommended/ordered/executed/cancelled timestamps separated |
| `sell_outcome_log.jsonl` | §6.8 | `write_sell_outcome` | counterfactual missed-gain measurement |
| `agent_attribution_log.jsonl` | §6.10 | `write_agent_attribution` | **1 row per agent per hypothesis** (R11 #1) |
| `portfolio_decision_log.jsonl` | §6.11 | `write_portfolio_decision` | daily portfolio-level decision (`action_taken` / `cash_retained` / …) |
| `cash_deployment_log.jsonl` | §6.12 | `write_cash_critic_triggered`, `write_cash_follow_up_outcome` | event_type split; joined by `cash_decision_id` |
| `belief_adjustments.jsonl` | §6.3 | `write_belief_adjustment` | `invalidation_rules` writes here; deltas reconstruct `adjusted_conviction` |
| `revision_mention_ledger.jsonl` | §6.7 | `append_mention_ledger` | `revision_tracker` priors; 30-day rolling window |
| `proxy_audit_log.jsonl` | §6.5 | `proxy_mapper.propose_proxies` | every LLM call's input/output for audit |

### Daily snapshots (atomic write, not append-only)

| File | Writer | Notes |
|---|---|---|
| `revision_state.json` | `revision_tracker.write_revision_state` | per-ticker daily revision direction / surprise / priced-in |
| `agent_reliability.json` | `agent_reliability.snapshot_to_file` | per-agent EV stats; consumed as advisory prompt context only |
| `scenario_promotion_summary.json` | `scenario_promotion.snapshot_to_file` | scenario-level observe-only promotion stats from catalyst outcomes; record-only |

## How to integrate (analyzer.py wiring guide)

The canonical call from `analyzer.py` (or any orchestrator) is:

```python
from datetime import date
from pathlib import Path
import os

from almanac.observability.candidate_extractor import extract_all
from almanac.observability.catalyst_layer import run

if os.environ.get("ALMANAC_ENABLE_CATALYST") == "1":
    today = date.today().isoformat()
    output = run(
        revision_state_path="revision_state.json",
        scenario_state_path="scenario_state.json",
        proxy_seed_map_path="proxy_seed_map.json",
        legacy_analysis_path="ai_portfolio_analysis.json",
        catalyst_log_path="catalyst_hypothesis_log.jsonl",
        analysis_id=f"analyzer-{today}",
        analysis_date=today,
        top_n=10,
        write_log=True,
    )
    # output.top → list[CatalystHypothesis] ready for prompt injection
    # output.by_type → telemetry for the daily summary
    # output.all_hypotheses → for downstream verification
```

**Default-off feature flag**: until validation, gate every call site on
`ALMANAC_ENABLE_CATALYST=1` so the layer is silent in production until an
operator opts in.

For the **reliability** snapshot (advisory prompt context only; deterministic
hard gates and caps remain non-overridable):

```python
from almanac.observability.agent_reliability import snapshot_to_file

snapshot_to_file(
    attribution_log_path="agent_attribution_log.jsonl",
    outcome_log_path="catalyst_outcome_log.jsonl",
    output_path="agent_reliability.json",
)
```

## Testing approach

Every module follows the same pattern, locked by the test files in
`tests/`:

1. **Pure-functional core** — rules, scoring, parsing, sanitisation are
   pure functions tested directly with handcrafted inputs covering happy
   path + edge cases + boundary values.
2. **I/O wrapped** — file-touching code lives in thin `run()` /
   `apply_*` orchestrators that accept injected paths, dates, and
   provider callables.
3. **No real LLM calls in tests** — `proxy_llm_provider` tests inject a
   fake `llm_call`. The Protocol contract is exercised; the SDK is not.
4. **Real production file round-trips** — every module that consumes a
   live worktree file (`agent_beliefs.json`, `scenario_state.json`,
   `signal_history.json`, `ai_portfolio_analysis.json`,
   `news_signal_candidates.json`) has a `test_real_*` smoke test that
   reads the actual file and asserts non-trivial output.

Current state: **728 tests, ~0.7 s wall clock, zero LLM API calls.**

## Migrations

All three migrations are **idempotent** (re-runs are no-ops), write a
timestamped `.bak` before touching anything, and commit via
`.tmp + os.replace` (atomic on POSIX).

| Script | Purpose |
|---|---|
| `almanac/migrations/agent_beliefs_v1_to_v2.py` | Splits `conviction_score` into `base_conviction` + `adjusted_conviction` + `adjustment_log[]`. Adds `schema_version: 2`. Existing `conviction_score` field is preserved for backwards-compat with ~14 readers in `analyst/__init__.py`. |
| `almanac/migrations/add_bull_pullback_playbook.py` | Appends the `bull_pullback` macro regime playbook to `scenario_playbook.json` with three-phase actions (Conservative / Aggressive / Tactical) and R11 #C feature flags. |
| `almanac/migrations/add_event_playbooks.py` | Creates `event_playbook.json` with the ticker/event-level `ipo_proxy_event` and `earnings_revision_drift` playbooks. |

Run any of them via `python -m almanac.migrations.<name> <path>`; pass
`--verbose` for INFO logging.

## File-by-file API summary

Generated from the live `__all__` of each module (truncated to public
names; private helpers omitted).

```
ids                       compute_hypothesis_id, new_row_id, new_analysis_id, new_cash_decision_id
status                    CandidateStatus, ExecutionState, PortfolioDecisionState,
                          LEGACY_CANDIDATE_STATUS, LEGACY_EXECUTION_STATE
append_only_log           append_jsonl_safe, normalize_to_jpy, normalize_to_usd, MeasurementQuality
logs                      write_catalyst_hypothesis_generated, _status_transition, _filtered,
                          write_catalyst_outcome, write_sell_decision, write_sell_outcome,
                          write_agent_attribution, write_portfolio_decision,
                          write_cash_critic_triggered, write_cash_follow_up_outcome,
                          write_belief_adjustment
signal_history            EXTENDED_FIELDS, LEGACY_HYPOTHESIS_TYPE, SignalRecord,
                          apply_legacy_defaults, make_record, read_history
invalidation_rules        RULE_VERSION, EXPIRY_DELTA, RSI_OVERHEAT_DELTA, MA20_BREAK_DELTA,
                          MarketIndicators, InvalidationAdjustment,
                          check_expired, check_rsi_overheat, check_ma20_break,
                          RULES, evaluate_belief, evaluate_beliefs, apply_invalidations
revision_tracker          REVISION_KEYWORDS, RevisionKeyword, RevisionMatch, TickerEntry,
                          match_headlines, compute_surprise_score, compute_priced_in_penalty,
                          build_ticker_entry, load_mention_ledger, append_mention_ledger,
                          write_revision_state, run
candidate_extractor       AGENT_* (8 constants), infer_action_type, infer_direction,
                          DEFAULT_HORIZON_DAYS, extract_from_sonnet_tier,
                          extract_from_synthesis, extract_from_deepseek_margin,
                          extract_from_deepseek_short, extract_from_catalyst_layer,
                          extract_all
proxy_mapper              LLMProvider (Protocol), ProxyResult, load_seed_map, lookup_seed,
                          jaccard_intersection, propose_proxies
proxy_llm_provider        SonnetProxyProvider, default_llm_call, TICKER_REGEX
agent_reliability         GroupStats, aggregate_agent_reliability, derive_weight,
                          snapshot_to_file
regime_shift_detector     RegimeShift, RegimeShiftReport, classify_severity,
                          detect_shift, compute_active_multiplier, run
catalyst_layer            CatalystHypothesis, CatalystOutput, compute_catalyst_score,
                          synthesize_from_revision_state, synthesize_from_active_scenarios,
                          synthesize_from_proxy_predictions, synthesize_from_legacy_producers,
                          dedupe_by_hypothesis_id, rank_by_catalyst_score, run
verifier_extensions       read_hypothesis_events, read_outcomes, latest_candidate_status,
                          compute_group_stats, aggregate_by_dimensions, summarize
```

## Out-of-scope (intentional)

- **Wiring into `analyzer.py`** — single-file surgical change on a
  14×-touched hot path; needs its own plan + review.
- **EDGAR / TDnet / OpenBB ingestion** — Phase 3 conditional task in the
  plan; only triggered if MVP data shows we need them.
- **Real broker execution** — the layer recommends sizes; it does not
  place orders. `sell_decision_log` records the recommendation; the
  human operator (or a future executor module) actually transacts.

---

Last refreshed for the 728-test baseline after Round 12 P1/P2 fixes,
Phase 2 completion, the catalyst_layer dict-shape patch, and the
production `proxy_llm_provider` ship. See
`.claude/plans/codex-claude-ai-sunny-babbage.md` for the full design
dialectic.

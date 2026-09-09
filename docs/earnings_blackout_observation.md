# S3 earnings blackout observations

This is an observation-only implementation. It neither promotes a controller
nor changes a trading threshold, prompt, direction, amount, or fallback.
S4 gate migration requires a separate decision after reviewing observations.

## Input and comparison contract

- After earnings self-heal, freeze one S2-validated snapshot, full holdings,
  classification registries, and override evidence. Before/after file identity
  and content checks reject concurrent changes. New shadow queries never reread.
- Affirmative conflicting classifications are unknown. Confirmed funds, ETFs,
  and cash are non-earnings even outside the producer's coverage. Missing
  classification is unknown. Confirmed stocks outside coverage are not-covered.
- Each legacy consumer retains its original read, window and return values.
  Capture actual prompt/post-filter/funnel/policy sets rather than recomputing
  legacy sets later. A missing consumer record means not observed, not clear.
  `legacy_resolved=null` means no validation status exposed by that loader.
- Windows 5, 7 and the actual post-filter/prompt windows share frozen dates.
  `input_hash` identifies full inputs; `query_hash` also includes queried windows.
- Candidate diagnostics capture the pre-policy list before rejection, the
  candidate funnel's screened rows, and final/rejected/deferred synthesis
  actions. They do not claim to enumerate every upstream screener. Unheld symbols
  are reported separately as not-covered and never added to the frozen state.
- ContextVars propagate through the existing thread submission helper. The
  recorder closes at finalization; late timed-out workers cannot change a saved
  observation or the next run's recorder.

## Persistence and monitoring

`earnings_blackout_observation.jsonl` is private ignored runtime data, included
in optional backup TARGETS. One serialized check-and-append per analysis ID;
flush/fsync before reporting success. Corrupt history or write failures are
reported, not repaired or discarded silently.

Distinct JST weekdays with validated inputs count toward ten observation days;
symbol-level unknowns count, input/capture failures do not. Multiple runs per
day count once. Cache reuse never appends. Ten days only sets `review_ready`;
`automatic_promotion` remains false.

Diagnostics reside outside synthesis and are not added to prompts. Failure is
merged into the terminal `portfolio_analyst` warning without hiding Telegram
failure. It must never update the scheduled producer's `earnings_proximity`
heartbeat. Analysis success/exit code remains independent of shadow failure.

## Rollout boundary

This review branch is not production main. No scheduled job, live AI run,
public sync, or deployment is performed by this implementation. After review
and deployment, inspect the first formal run's diagnostic, JSONL ID, consumer
records and terminal heartbeat before relying on the observation-day count.

## Implementation verification (2026-09-09)

- Base: `d5cfd9a`, on the review worktree, not production main.
- S2 prerequisite fixes: reject non-array result collections; validate skipped
  row day counts before comparisons so one malformed row cannot remove the
  entire prompt block. Twelve new parameterized cases cover these failures.
- Related regression set: 233 passed with both JST and UTC environments.
  This includes exact post-filter output parity, unchanged prompt text,
  retained event-trade cap, and real terminal heartbeat/watchdog evaluation.
- Full regression in the isolated tracked-source copy: 4,154 passed,
  11 skipped, 9 failed. All nine failures also reproduced on pristine
  `d5cfd9a` in a separate copy (missing untracked ticker/market input files).
  They were not suppressed or fixed as part of S3.
- Four isolated mutations were detected: missing-classification fallback,
  torn-input acceptance, terminal warning overwritten by success, and invalid
  result arrays accepted by S2. Mutation copies were restored; source worktree
  was not mutated for these experiments.
- New module and changed non-analyst modules pass Ruff. The analyst module
  retains the same nine pre-existing E402 diagnostics as pristine HEAD.
- No paid AI execution, production deployment, commit, or public push.
- Final follow-up: capture pre-policy candidates before rejection and funnel
  candidates before filtering; candidate extraction failures remain inside the
  observation error boundary. Four additional regression cases cover rejected
  symbols, malformed collections, actual funnel wiring and late workers.

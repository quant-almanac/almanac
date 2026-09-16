# Candidate lineage: integration contract (not yet implemented)

This is separate from `candidate_output_manifest`, which seals final saved
content but cannot prove pre-save conservation across stages.

## Confirmed boundaries

- The primary policy call is in `analyst.run_analysis`; fallback candidates
  also pass through a separate `apply_policy_gate` call.
- `policy_filtered_actions` contains BOTH rejected entries (`action`) and
  modification history (`original`, `modified`, `modifications`). A modification
  is not a second candidate and is not a rejection.
- Phase1 can run more than once after fallback injection. Its per-call
  `accepted#N` tags cannot be reused as persistent candidate identities.
- The allocator and rollout quarantine can change final readiness after Phase1.
  A Phase1 snapshot is therefore not the final execution disposition.
- The final cache result currently has an `as_of` but does not explicitly copy
  the host analysis ID to the top level. A lineage contract must carry the real
  run identity; timestamp or ticker guessing is not a substitute.

## Required contract

1. Assign a host-owned, run-scoped candidate ID before each primary/fallback
   policy entry. Record an origin lane and immutable input digest. Never trust
   IDs supplied by the model, and never assign IDs again merely because Phase1
   is rerun. Missing run identity means unavailable diagnostics, not invented
   evidence.
2. Record policy dispositions as accepted/rejected. A modified accepted row
   retains the same candidate ID and records its before/after digests. Rejected
   rows need an explicit terminal disposition. Modification history must not
   consume an extra candidate.
3. Record each Phase1 invocation with its own invocation ID, its input IDs and
   kept/filtered/deferred outcomes. Preserve earlier invocations; subsequent
   reruns do not erase prior rejected/deferred candidates from the audit.
4. Capture allocator/quarantine changes and the final output snapshot. New
   fallback candidates need a new origin record; unexplained candidates must
   produce a diagnostic, not be retrospectively blessed as initial inputs.
5. Bind IDs to content digests, not IDs alone. Separate allowed transformations
   from replacement of a candidate's security/account/scope. Do not assume that
   every semantic field is immutable: some routing/type changes are legitimate.
6. Persist a versioned lineage manifest beside the final-output manifest in the
   same atomic cache publication. No ledger write, execution authorization or
   risk-limit change is part of this work. Missing legacy lineage is explicitly
   unverified. Independent tamper-proof witnessing is a separate future concern.

## Acceptance matrix before integration

- Real policy modify/pass/reject -> real Phase1 -> allocator -> save -> verifier.
- Buy-to-add and account normalization retain provenance with a recorded change.
- Primary plus fallback, repeated Phase1, all rejected and no candidates.
- Drop, duplicate, same-count substitution, copied ID with altered ticker,
  cross-run replay, missing run identity and malformed lineage.
- Audit failure leaves trading decisions unchanged and produces a visible
  unavailable diagnostic; it cannot silently claim complete lineage.
- Compare candidate selection, quantities, readiness and guard decisions with
  tracing enabled/disabled on the same synthetic inputs.

Implementation should be a separate commit series: pure contract and tests,
primary/fallback policy adapters, Phase1 invocation adapters, then final save
and verifier integration. Do not label a partial adapter chain as complete.

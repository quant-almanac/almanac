# Candidate output audit v1

## Scope

This contract detects changes to a saved analysis's candidate content without
changing allocation, risk policy, approval or execution authority. It is **not**
cross-stage candidate lineage and does not authenticate the issuer of a file.

`analyst.cache.save_cache` seals a separate snapshot immediately before cache
publication. This is after legitimate Phase1 and allocator changes. Incoming
manifests are replaced by the host; callers' dictionaries are not modified.
The manifest and analysis are published by the same existing atomic write.

## Persisted contract

Top-level `candidate_output_manifest` contains integer `schema_version: 1`,
`status: sealed`, SHA256 of the run's `analysis_id` and `as_of`, a SHA256 for
each complete candidate row in each of these ordered groups, and a digest of
`decision_summary`:

- `priority_actions`
- `_filtered_actions`
- `order_intent_deferred_actions`
- `policy_filtered_actions`

Canonical JSON uses sorted keys, compact separators, UTF-8 and rejects NaN and
Infinity. A missing group means an empty list; an explicitly invalid group is
not silently converted to empty. Group position is an output locator, not an
identity that survives transformations. Unknown fields in a row are included.

An unsupported/non-serializable audit input produces `status: unavailable`.
The audit does not veto cache publication; ordinary serialization requirements
still apply. The read-only post-run checker reports unavailable, malformed,
unsupported or mismatching manifests as errors. A missing manifest is explicitly
unverified legacy data (warning); old files are never retroactively sealed by
the verifier. A missing/unreadable analysis is an error.

## Limits and next contract

A ticker-only substitution with unchanged counts is detected if the original
manifest remains intact. Changing both payload and manifest defeats this check;
there is no signature, independent append-only witness or authenticity claim.
Deleting the manifest cannot be distinguished from legacy format here and is
reported as unverified, not proof of integrity. A later authorized `save_cache`
call establishes a new seal and is not evidence against the previous content.

Future lineage needs run-scoped candidate IDs assigned before policy evaluation,
explicit transformation/disposition records and binding of each stage's content
to those IDs. That contract remains unimplemented. Do not promote this digest
check into a statement that every original candidate reached a final bucket.

Tests cover the actual save/check boundary, JSON round-trip, caller isolation,
all four groups' substitution/drop/duplicate cases, summary/run binding,
invalid versus legacy inputs and the explicit lack of authentication.

## Development verification

The audit tests plus the six objective-repair suites and cache concurrency,
synthesis-failure and pipeline-observation suites pass: 442 tests in each of
UTC and Asia/Tokyo, with isolated state directories. Replacing the content
digest with a constant in a separate Python process made all four ticker-swap
cases fail on their expected assertions; no source mutation was retained.

Three existing observation assertions initially rejected the new top-level
metadata. They now verify the manifest separately and still require exact
equality of every original analysis field. Input immutability, atomic cache
publication and observation-write failure tests remain enabled.

No full-repository suite, production deployment, push, paid analysis or trading
operation was performed for this change.

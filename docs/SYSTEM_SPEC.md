# ALMANAC system specification

*[日本語](SYSTEM_SPEC.ja.md) · [README](../README.md) · [Module catalog](MODULE_CATALOG.md)*

This document describes the contracts implemented by the current source tree. It is deliberately more precise than the README: it distinguishes a calculation that exists, a shadow observation, an advisory rule, and a value that can reach a human-executable recommendation. Local portfolio files, broker evidence, model bundles, logs and secrets are not part of the public repository.

## 1. Scope, authority and status words

ALMANAC is decision support, not an order-routing system. No module holds broker credentials or submits an order. The authority chain is:

`measurement → AI candidate → deterministic normalization → policy → post-filter → readiness → human order → human/broker fill record`

The status words have fixed meanings:

| Status | Contract |
|---|---|
| live | Used by the normal daily path |
| optional | Used only after explicit configuration |
| shadow | Runs and records a counterfactual; cannot mutate the live action |
| advisory | May alter a recommendation or size, but still cannot place an order |
| review | Evidence is incomplete; a person must verify it |
| blocked | The current action must not be executed |
| unwired | Code exists but the daily decision path does not consume it |
| retired | Kept only for compatibility, audit or historical use |

The checked-in product defaults are mixed by design: cash securities and non-leveraged funds are available; margin and short proposals are conditional; options are signals only; Kelly, concentration policy and FX hedging are shadow observations; GINN is default-denied until promotion; the execution-plan gate starts in observe mode. `objective.md` is the authority for investment-policy limits.

## 2. Daily analysis transaction

`portfolio_analyst.py --force` calls `analyst.run_analysis()` and performs one logical analysis transaction:

1. load secrets and reject an unusable privacy mode;
2. refresh macro events, technicals, news, earnings, VIX, DCA, execution plan and scenario state when required;
3. verify/grade historical recommendations;
4. load the portfolio and market context;
5. calculate risk, regime and observation-only policies;
6. build and freeze the enriched decision snapshot;
7. run five specialist lanes with bounded timeouts;
8. run adversarial review, disagreement scoring and the optional judge;
9. synthesize structured actions with Claude Opus;
10. bind provenance, account identity, quote and sizing context;
11. apply deterministic policy, exit sizing, post-filters and readiness;
12. run Kelly and FX counterfactual lanes without modifying actions;
13. rebuild narrative from the final readiness state;
14. persist the analysis, audit stages, recommendation observations and eligible action states.

A missing optional lane degrades that lane. A missing portfolio, a failed decision-snapshot freeze, a truncated final tool result or a failed post-filter contract cannot be treated as a successful recommendation set.

## 3. Identity and ownership boundaries

A ticker is not a position identity. The system uses three separate keys:

| Key | Fields | Use |
|---|---|---|
| `PositionIdentity` | owner + broker + account + canonical instrument | holdings, exits, action state, tax candidates |
| `AccountResourceIdentity` | owner + broker + account + currency | cash and buying power |
| `NisaCapacityIdentity` | owner + broker + account + NISA type + tax year | annual/lifetime allowance |

Aliases are normalized by `position_identity.py`; identity is propagated through tax candidates, conflict checks, recommendations, action IDs, action state, API rows, governance and broker reconciliation. Unknown ownership is not guessed. A zero holding requires a broker snapshot proving absence; “not found in a local dict” is not evidence of zero.

Household concentration may aggregate the same instrument across accounts for risk measurement, but an order remains scoped to one position identity. Signal statistics for Kelly use ticker + direction + horizon, while position sizing uses the full position identity.

## 4. Freshness authorities

Freshness is evidence-specific, not file-wide:

- a sell requires a broker-reconciled quantity for the exact position;
- a buy requires fresh cash for its account and currency;
- a NISA action requires fresh capacity for its owner, account, type and tax year;
- `submitted`, `recommended` and local `portfolio_applied` events do not advance broker freshness;
- a confirmed fill needs external execution ID, broker source, broker timestamp, quantity, price, reconciliation time and snapshot hash;
- duplicate external execution IDs are rejected.

Analysis-source freshness is separately carried for holdings, cash, prices, FX, macro, news, screening and per-ticker options. `fresh`, `degraded`, `stale` and `unknown` are based on source time and each source's maximum-age policy. A hash proves immutability, not freshness.

## 5. Decision and execution snapshots

The decision lane is two-stage:

1. `base_snapshot`: holdings, cash, prices, FX, macro, news and screening;
2. `enriched_snapshot`: base plus chart/options data for holdings, deterministic candidates and open-order instruments.

The enriched snapshot is frozen before the first tier LLM. It records source time, retrieval time, freshness, source label, artifact hash, payload hash, code revision, model IDs, prompt hash, policy/config versions, budget mode, tunable hash and analysis clock. The same snapshot ID and content hash must reach tier output, final synthesis, actions and policy.

`execution_quote_snapshot` is a different lane. It may refresh price, bid/ask spread, session state and expiry immediately before execution. It may recalculate a limit or downgrade readiness; it cannot rewrite the original thesis, confidence or expected return. A changed investment conclusion requires a new analysis ID.

## 6. LLM routing, structured output and privacy

`model_router.py` resolves roles, then budget mode may upgrade or downgrade the resolved model. The normal daily roles are:

- long, medium and held-swing analysis: Claude Sonnet;
- margin-long and short-sell analysis: DeepSeek;
- final synthesis: Claude Opus;
- low-cost extraction/search/guard tasks: Claude Haiku or provider-specific adapters;
- optional pseudonymized judge: DeepSeek reasoner.

Anthropic calls that reject sampling parameters never receive `temperature`, `top_p` or `top_k`. Opus 5 and Sonnet 5 receive `output_config.effort=low`; forced tools are used with adaptive thinking. `max_tokens` truncation is an error and selected paths retry with a larger limit.

`ALMANAC_PRIVACY_MODE` controls book-aware egress:

| Mode | Allowed book-aware providers |
|---|---|
| `strict_local` | none |
| `anthropic_book_aware` | Anthropic only |
| `multi_provider_book_aware` | configured providers |

Public/anonymized payloads pass a typed allowlist and a secondary PII scan. Public screeners have a separate no-book call-site contract. `run_with_secrets.sh` exports both `KEY=value` and `export KEY=value` assignments from `~/.almanac_secrets`.

## 7. Evidence lineage and claims

Evidence uses a tagged union:

- `external`: URL, publication time, observation date and retrieval time;
- `snapshot`: artifact/payload hash and source timestamp;
- `derived`: input claim IDs and calculation version;
- `unverified`: retained for display only.

Every action receives claim IDs. Numeric probability/confidence claims without a valid lineage downgrade readiness. Derived values such as GARCH volatility or IV rank do not invent URLs; they point to the claims and calculation version that produced them.

Tier-derived Black-Litterman views retain the same lineage as their tier output. Renaming a recycled view does not make it independent. If `independent_count=0`, no corroboration language is injected and the optimizer uses the prior.

## 8. Candidate lanes and admission

Candidate production is separate from portfolio analysis:

- momentum/fundamental screeners;
- long-term batch thesis generation and later batch collection;
- margin-long and short-sale screeners;
- news, social/options anomaly, pair, squeeze and overnight-gap lanes;
- EDINET, TDnet and EDGAR disclosure features;
- insider, IPO and scenario-playbook candidates.

Each lane records its universe, scanned count and candidates separately. A screen candidate is not an action. It must be admitted to a tier, survive final synthesis, bind to an account, pass policy/post-filter/readiness and remain non-invalidated.

Scenario playbooks are precommitted responses, not policy bypasses. Injected rows carry scenario status and caps; the execution-plan engine accepts the special path only when its attestation is complete.

## 9. Portfolio construction and Black-Litterman

The optimizer supports:

- `max_sharpe`;
- `min_cvar`;
- `equal_risk`, implemented as inverse-volatility weighting rather than equal risk contribution;
- optional Black-Litterman.

Independent Black-Litterman sources are analyst consensus, momentum and factor beta. `BL_USE_INDEPENDENT_ALPHA=0` keeps tier-derived rows as audit-only; `1` consumes independent sources; `mix` may store both but consumes only rows marked independent.

Optimization output is a target, not an order. Account eligibility, NISA limits, open intents, minimum lots, tax and readiness are resolved later.

## 10. Risk, volatility and model validation

Risk is reconstructed from current holdings and historical price returns. Cornish-Fisher VaR corrects the normal quantile for skew and kurtosis. Primary CVaR is historical Expected Shortfall; the Cornish-Fisher-threshold variant is auxiliary and fewer than ten tail observations is marked unstable.

GJR-GARCH is the operating volatility model. GINN is a two-layer LSTM (hidden 64, dropout 0.1) with linear + Softplus output and:

`MSE(predicted sigma, absolute residual) + 0.3 × MSE(predicted sigma, GARCH sigma)`

GINN is research-only unless a versioned bundle passes the frozen promotion policy, model/manifest checksums, data age, feature coverage, validation sample/ticker counts, GARCH comparison and inference schema. A missing manifest or current pointer is default-deny and returns a structured GJR-GARCH fallback with reason. The flat legacy model is retained for audit but cannot be silently loaded.

Current training still has an incomplete inference contract for per-ticker scalers, so candidates are not promotable. VIX/regime history and leakage-free rolling GARCH features remain future research. Held-out data used for promotion is called validation; only observations arriving after promotion count as forward evidence.

The VaR path records forecasts and applies a Kupiec proportion-of-failures test. Passing that test validates breach frequency for that VaR series, not the whole risk stack.

## 11. Five-level market regime, rates and cash

`market_regime_v2.py` scores US and Japan separately and combines them by invested equity value:

| Level | Cash target | New-buy multiplier | Leverage |
|---|---:|---:|---|
| strong bull | 3% | 1.00 | conditional |
| mild bull | 7% | 0.75 | no |
| neutral | 12% | 0.50 | no |
| mild bear | 20% | 0.25 | no |
| strong bear | 30% | 0.00 | no |

Inputs cover index distance from MA50/MA200, market breadth, VIX, HY OAS and rates. Rates distinguish a tightening shock, easing support, stress easing, restrictive real/nominal levels and curve inversion. Coverage, breadth observations, risk inputs and rate inputs must meet eligibility requirements. A two-evaluation confirmation prevents one noisy reading from flipping the committed level.

A separate shock overlay can stop discretionary buying but does not sell after a crash merely to raise cash to the target. All confirmed cash inside the represented accounts is investable surplus capital; the protected lifestyle reserve is zero, while tactical cash, settlement, collateral, fees, tax and existing order reservations still apply. The execution plan deploys only the confirmed balance above the tactical target and derives its monthly allowance by dividing that surplus over 2 / 3 / 6 / 12 months for strong bull / mild bull / neutral / mild bear. Strong bear or an active shock creates no ordinary buying allowance. Filled buys consume the allowance without reducing its basis twice; confirmed cash flows recalculate it. Cash authority remains event-based rather than expiring solely because time elapsed.

## 12. Policy, readiness and invalidation

`policy_engine.py` is deterministic. Major checks include drawdown/VaR/VIX gates, regime size caps, leverage rules, NISA/account eligibility, earnings and macro blackouts, order-intent conflicts, discretionary funding, concentration and minimum tradable units.

Readiness is additive and severity-monotonic: a later check cannot improve `blocked` to `review` or `ready`. Reasons are structured and retained. Stale/unknown position evidence, unverified claims, ambiguous accounts, unresolved sizing and imminent events downgrade or block according to contract.

`execution_invalidation_state.json` is an immutable overlay over historical analyses/actions. Today, API, backlog, notifications and action-state consumers use the same resolver. Invalidation does not delete source history; it prevents an old action from becoming executable or being revived by deduplication. Historical fill reporting remains possible.

## 13. Deterministic sizing and order intent

Exit size is calculated in this order:

`current weight → target/band → maximum step → tax effect → lot rounding → open orders`

`intent_key` prevents duplicate economic intent; `evaluation_key` identifies one snapshot evaluation. A changed snapshot creates a revision of the same intent, not cumulative quantity. Existing orders require cancel/replace or human review. Unknown tax input produces `review`, not a zero-size false answer.

The execution quote layer calculates bid/ask, spread, ATR, VWAP, support/resistance, expiry and market/limit eligibility. A no-trade band compares expected edge with spread, fees and measured implementation shortfall. Human-readable explanations are derived from the structured result.

Action state distinguishes recommendation, pending, placed, ordered, filled, cancelled, expired and invalidated. Only broker-confirmed fills update position freshness.

## 14. DCA and concentration policy

The drawdown ladder has independent triggers:

- T1: decay from a VIX peak;
- T2: drawdown at most -12%, VIX at least 25, Fear & Greed at most 25 and HY OAS at least 500 bps;
- T3: drawdown at most -18%, VIX at least 40, put/call above 1.2 or VIX above 40, plus RSI reversal.

All tranches also require breadth, volume, cooldown, annual cap (15% of net worth) and per-currency funding checks.

Household concentration is observed across owners/brokers/accounts by canonical instrument. Default caps are long 10%, medium 5%, swing 2% and employer stock 10%. Mixed tier assignments use the strictest tier and are surfaced for review. This lane is shadow-only: it records breaches and does not mutate actions.

## 15. Half-Kelly shadow contract

Half-Kelly is:

`0.5 × (p × b - q) / b`

The sample is recommendation performance, not trade performance. It includes buy/add/DCA signals, deduplicates by analysis + ticker + direction, separates 5/20/60-business-day horizons and requires at least 20 observations for the requested horizon. A signal can be evaluable even when execution was ineligible; stale/unknown input makes the signal itself non-evaluable.

The cap is an absolute target position: `max(0, Kelly target - current holding)`. The counterfactual path starts from the same frozen context as the real path, applies the cap before policy, reruns rounding and filters, and records the difference. It cannot write action state, recommendation logs, execution plans, notifications or the real action. Below one tradable unit becomes non-actionable rather than raising an assertion.

## 16. Economic currency and FX shadow contract

Three quantities are kept separate:

- gross asset currency allocation;
- net FX exposure after hedges;
- hedge overlay notional.

Listing currency is not economic currency. The instrument master stores underlying exposure, USD ratio, hedge ratio, leverage/inverse flags, replacement eligibility, source, as-of and validity date. Unknown or expired classifications fail closed; they are not treated as zero USD.

Actual hedge notional is read only from complete broker position snapshots. Missing actual evidence is `unknown`, not zero. Shadow, target and actual notionals are separate states. The target ratio is limited to 0–70%, and the change is at most 10 percentage points per evaluation date. Same-date/same-snapshot reruns are idempotent.

Hedged ETFs are replacements, not overlays, and may replace only corresponding unhedged index exposure after tax, cash and lot checks. Futures/FX are overlays with different margin, tax and order adapters. Modes are `off`, `shadow` and `advisory`; none auto-submit.

## 17. Tax, NISA and employee plans

The v2 tax schema always separates:

- economic realized P&L;
- taxable realized P&L;
- NISA realized P&L.

`ALMANAC_TAX_BASIS_MODE=legacy|compare|total_average` changes the calculation source, not the API fields. The total-average-like engine scopes owner, broker, account and instrument; handles fees, FX, splits/merges, transfers and opening balances; and fails closed on incomplete history. Broker tax values are authoritative when supplied; the internal ledger is a reconciliation value.

NISA gains/losses are not netted with taxable accounts. Annual investment capacity and the lifetime holding allowance restored in the following year are separate. A migration may never cross owner or broker identity. Lot IDs remain audit lineage; tax estimates use the position-level average basis, not a hand-picked low-gain lot.

`tax_lot.py` remains useful for inventory quantity, missing transaction and corporate-action audits. Specific-lot loss-harvest/gain-minimize proposals are not actionable under the Japanese total-average contract. Employee-plan modules have separate concentration, incentive and human-only exit logic.

## 18. Accounting, performance and governance

`event_ledger.py` is append-only and records trades, cash flows, dividends, fees, tax and FX-related events. Broker reconciliation compares external records with that ledger. Corrections are new events, not destructive edits.

Daily NAV and benchmark are separate series. Performance uses a Modified Dietz cash-flow-adjusted approximation and reports after-tax/fee JPY results against the configured 60/40 benchmark. Estimated NAV backfills restore chart continuity but are excluded as measured anchors for daily guardrails.

Recommendation outcomes, sell/catalyst outcomes, execution quality, action-stage coverage, agent attribution and monthly governance are different metrics. Candidate count, policy acceptance, final actions, filled actions and measured outcomes must not be merged into one success rate.

## 19. Automation, state and failure behavior

The public repository installs no schedule. Example LaunchAgents cover AI analysis, disclosure ingestion, NAV and benchmark; the example crontab covers screeners. All commands use an absolute interpreter or `venv/bin/python`, run through `run_with_secrets.sh`, and assume Asia/Tokyo unless changed.

State writers use atomic replacement or append-only logs as appropriate. Tests redirect state/model/report directories to temporary paths; subprocess tests need environment-level directory overrides because Python monkeypatching does not cross a process boundary.

Failure rules:

- stale/missing evidence is visible and normally review/block, never “clear”;
- GINN failure falls back to GJR-GARCH with the actual model label;
- optional model/lane failure degrades that lane;
- final synthesis truncation or malformed tool output fails;
- post-filter failure quarantines actions;
- shadow state never advances actual holdings;
- rollback of additive tax migration uses the read flag, not deletion;
- an unvalidated GINN safety gate must not be reverted to legacy loading.

## 20. API, dashboard, verification and public release

FastAPI exposes read views and authenticated write controls. Today resolves invalidation/readiness before presenting actions. Fill endpoints record broker evidence but do not place orders. Tax API fields stay v2 across calculation modes. The Next.js dashboard is a consumer of these contracts, not a second source of truth.

The minimum release sequence is:

```bash
python3 -m py_compile <changed Python files>
venv/bin/python -m pytest tests/ -q
python scripts/check_docs_consistency.py
python scripts/check_public_safety.py
git diff --check
```

For changes to the daily path, additionally run a live `portfolio_analyst.py --force`, then `post_run_verify.py`, and inspect only log rows created by that analysis ID. Verify model IDs, tool stop reasons, cost accounting, snapshot hashes, readiness reasons, provenance, GINN fallback/promotion, FX/Kelly shadow output and state mutation allowlists.

The public repository contains code, fixtures and example configuration only. Never copy broker exports, household identity maps, production model bundles/manifests, FX actual/shadow state, tax comparisons, crontab backups, local paths or state-hash manifests into it. See the exhaustive [module catalog](MODULE_CATALOG.md) for ownership boundaries.

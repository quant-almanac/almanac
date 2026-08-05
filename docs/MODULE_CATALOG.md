# ALMANAC module catalog

*[日本語](MODULE_CATALOG.ja.md) · [System specification](SYSTEM_SPEC.md)*

This is an ownership map, not a claim that every file is live. The lifecycle column is intentionally coarse; the source, tests and runtime mode remain authoritative. Every root-level Python module is listed between the catalog markers, and `scripts/check_docs_consistency.py` fails when the source tree and this inventory diverge.

<!-- ROOT_MODULES_START -->

## 1. Entrypoints, user interfaces and operations

| Lifecycle | Modules | Boundary |
|---|---|---|
| live | `portfolio_analyst.py`, `portfolio_agent.py`, `bot_commands.py`, `telegram_bot.py`, `generate_dashboard.py`, `weekly_report.py` | Daily CLI/API-facing analysis and human presentation |
| live | `daily_health_check.py`, `watchdog.py`, `alert.py`, `nightly_recheck.py`, `post_run_verify.py` | Health, stale-output detection and post-run consistency |
| optional | `decision_support.py` | On-demand decision support outside the primary Next.js console |
| shared | `utils.py` | Atomic I/O, secrets, heartbeats, FX cache and common utilities |

## 2. LLM routing, analysis and cost

| Lifecycle | Modules | Boundary |
|---|---|---|
| live | `analyzer.py`, `llm_adapters.py`, `model_router.py`, `llm_cost_accounting.py`, `llm_run_context.py`, `analysis_output_validation.py` | Provider transport, role routing, run identity, spend attribution and deterministic validation of financial prose |
| live/observe | `red_team_ledger.py`, `compare_harness.py`, `human_feedback_log.py` | Adversarial evidence, harness comparison and human labels |

The large orchestration implementation lives in the `analyst/` package; see §15.

## 3. Portfolio, accounts and household policy

| Lifecycle | Modules | Boundary |
|---|---|---|
| live | `portfolio_manager.py`, `portfolio_integrity.py`, `position_identity.py` | Portfolio construction, integrity and owner/broker/account identity |
| live | `discretionary_funding.py`, `contribution_ledger.py`, `contribution_recorder.py`, `contribution_schedule.py` | Authoritative new-money and external cash-flow contracts |
| advisory/shadow | `investment_policy.py`, `currency_policy.py` | Concentration/cash policy and legacy asset-currency target validation |
| human-only | `credit_card_investment.py`, `espp_plan_manager.py`, `employee_plan_exit.py` | Recurring cards and employee-plan concentration/exits |
| advisory | `nisa_allocator.py`, `nisa_migration_planner.py` | Owner-scoped NISA capacity and migration planning |
| live/advisory | `rebalance_engine.py`, `rebalance_planner.py` | Drift calculations and human-executable rebalance plans |

## 4. Execution lifecycle and safety

| Lifecycle | Modules | Boundary |
|---|---|---|
| live | `action_stage_log.py`, `action_state_tracker.py`, `execution_invalidation.py` | Stage audit, action lifecycle and invalidation overlay |
| live | `execution_readiness.py`, `execution_safety.py`, `execution_preflight.py`, `policy_engine.py` | Deterministic admission, freshness and signed pre-order safety gates |
| live | `feature_controls.py` | Runtime feature switches, effective-state reasons and fail-closed UI contract |
| live | `order_intent_resolver.py`, `exit_sizing.py`, `execution_explanation.py`, `action_amounts.py` | Idempotent intent, deterministic quantity and structured amount display |
| observe | `execution_plan_engine.py`, `execution_plan_observer.py`, `execution_quality.py` | Plan budgets, enforce-readiness evidence and implementation shortfall |
| live | `behavioral_guard.py`, `margin_manager.py` | Realized-P&L shock guard and margin-position controls |

## 5. Risk, allocation and quantitative research

| Lifecycle | Modules | Boundary |
|---|---|---|
| live | `risk_policy.py`, `risk_engine.py`, `portfolio_risk_returns.py`, `risk_model_validation.py` | Versioned fixed limits, VaR/CVaR, holding-based returns and Kupiec validation |
| optional | `portfolio_optimizer.py`, `optimize.py`, `bl_alpha_sources.py` | Allocation objectives and independent Black-Litterman views |
| live/advisory | `market_regime_v2.py`, `regime_params.py`, `vix_classification.py`, `vix_tracker.py` | Regime/rate/shock policy and volatility-state inputs |
| shadow/manual-promote | `drawdown_state_machine.py`, `drawdown_enforcement.py` | Flow-adjusted DD hysteresis and explicit enforcement promotion |
| live | `drawdown_dca_engine.py`, `leveraged_decay_monitor.py` | DCA ladder and leveraged-product decay |
| research/default-deny | `ginn_model.py` | Candidate training, promotion manifest and GARCH fallback |
| shadow | `kelly_sizing.py`, `kelly_shadow.py` | Recommendation statistics and counterfactual half-Kelly cap |
| shadow | `fx_exposure.py`, `fx_hedge_manager.py`, `fx_hedge_policy.py`, `fx_actual_hedge_state.py` | Look-through currency, target, broker actual and shadow decision |
| measurement | `factor_attribution.py` | Factor regression/attribution |

## 6. Market data, metadata and state producers

| Lifecycle | Modules | Boundary |
|---|---|---|
| live | `data_fetcher.py`, `technical_signals.py`, `chart_analyzer.py`, `options_fetcher.py` | Price history, technicals, decision chart and options context |
| live | `macro_fetcher.py`, `macro_event_calendar.py`, `event_calendar.py`, `follow_rate_analyzer.py` | Macro/rates and event calendars |
| live | `news_fetcher.py`, `geopolitical_monitor.py`, `earnings_proximity_manager.py`, `earnings_season.py` | News/geopolitics and earnings blackouts |
| live | `sector_rotation.py`, `sector_strength_updater.py` | Sector state |
| shared | `instrument_metadata.py`, `pseudo_tickers.py`, `download_tickers.py`, `expand_tickers.py` | Instrument identity, synthetic IDs and universe files |
| maintenance | `parquet_rebuilder.py`, `sync_jp_universe_prices.py` | Local price-store repair and JP-universe synchronization |
| live | `analysis_snapshot.py`, `freshness_policy.py`, `claim_provenance.py`, `holdings_freshness.py` | Frozen decision inputs, refresh/staleness invariants and claim lineage |

## 7. Screeners and candidate measurement

| Lifecycle | Modules | Boundary |
|---|---|---|
| live | `screener.py`, `short_screener.py`, `margin_long_screener.py`, `long_term_screener.py` | Primary long, short, margin and long-term candidate lanes |
| live | `news_screener.py`, `social_screener.py`, `pair_screener.py`, `squeeze_detector.py`, `overnight_gap_scanner.py` | News/social/pair/squeeze/gap lanes |
| shared | `screening_helpers.py`, `short_universe.py`, `jp_loanability.py`, `kabu_mini_eligibility.py` | Common screening, short inventory and JP tradability |
| observe | `screener_shadow_book.py`, `swing_lane_kpi.py`, `signal_tracker.py` | Candidate outcomes, swing KPI and signal history |
| maintenance/legacy | `screener_backup.py`, `screen_fix.py` | Compatibility and repair tools |

## 8. Disclosures, filings and feature promotion

| Lifecycle | Modules | Boundary |
|---|---|---|
| live | `edinet_fetcher.py`, `tdnet_fetcher.py`, `edgar_fetcher.py`, `ingest_disclosures.py` | Official-source ingestion |
| live/observe | `disclosure_enrich.py`, `deterministic_disclosure_features.py`, `disclosure_feature_extractor.py` | Filing text and deterministic/LLM features |
| observe | `disclosure_feature_promotion.py`, `disclosure_shadow_book.py`, `disclosure_push.py`, `brief_disclosures.py` | Promotion evidence, shadow P&L and display |
| live | `jp_buyback_parser.py`, `jp_dilution_parser.py`, `jp_guidance_parser.py`, `jp_monthly_sales_parser.py` | Deterministic JP filing parsers |
| audit | `extraction_audit_sampler.py`, `feature_validation.py` | Extraction sampling and feature validation |

## 9. Scenarios, catalysts, topics and alternatives

| Lifecycle | Modules | Boundary |
|---|---|---|
| live/advisory | `scenario_engine.py`, `scenario_strategy.py`, `scenario_invariants.py` | Scenario state, playbooks and invariant checks |
| observe | `catalyst_outcome_catchup.py`, `news_topic_analyzer.py`, `social_topic_analyzer.py` | Catalyst/topic outcome and classification |
| observe | `ipo_watch.py`, `insider_cluster.py`, `insider_tracker.py`, `insider_restrictions.py` | IPO and insider signals/restrictions |

## 10. Tax

| Lifecycle | Modules | Boundary |
|---|---|---|
| compare/advisory | `tax_lot.py`, `tax_harvest_scanner.py`, `tax_optimizer.py` | Inventory audit, total-average comparison and human-only tax ideas |

Tax display and action consumers also exist in API, analyst, NISA, ESPP and rebalance modules; changing only these three files is not a complete tax migration.

## 11. Ledger, NAV, benchmark and reconciliation

| Lifecycle | Modules | Boundary |
|---|---|---|
| live | `event_ledger.py`, `event_ledger_backfill.py`, `cash_transactions_backfill.py` | Append-only events and controlled backfill |
| live | `nav_recorder.py`, `benchmark_tracker.py` | Measured NAV and benchmark |
| maintenance | `nav_backfill.py`, `ledger_fx_reprice.py`, `ledger_trade_corrections.py`, `opening_balance_backfill_9432.py` | Explicit estimated/correction paths |
| live | `broker_balance_import.py`, `broker_position_import.py`, `broker_reconcile.py`, `broker_reconcile_cron.py`, `broker_cost_basis.py`, `execution_reconciliation.py` | Broker evidence, position/cash import, route-correction overlay, cost-basis admission and ledger comparison |
| maintenance | `sync_broker_short_us.py`, `sync_broker_short_us_index.py`, `sync_jp_loanable.py`, `sync_jp_short_taisyaku.py`, `sync_jsf_lending.py` | Borrowability and broker-universe synchronization |
| live | `config_clean_baseline.py`, `backup_manager.py` | Clean measurement boundary and recoverable backup |

## 12. Tuning, backtests and governance

| Lifecycle | Modules | Boundary |
|---|---|---|
| optional | `auto_tune.py`, `tunable_params.py`, `tuning_advisor.py`, `threshold_calibrator.py` | Guarded parameter evaluation/application |
| research | `backtest.py`, `backtest_full.py`, `backtest_wfo.py`, `backtest_comparison.py` | Historical and walk-forward research |
| measurement | `recommendation_verifier.py`, `recommendation_verifier_walk_forward.py`, `behavior_coverage_report.py`, `monthly_governance_report.py` | Outcome grading, pipeline coverage and governance |

## 13. One-off migrations and compatibility utilities

| Lifecycle | Modules | Boundary |
|---|---|---|
| one-off | `migrate_avgo_keys.py` | Historical key migration |
| development | `test.py`, `test_analyzer.py` | Legacy manual test entrypoints; the supported suite is under `tests/` |

## 14. Non-root packages

| Path | Responsibility |
|---|---|
| `analyst/` | Daily orchestration, data gathering, model calls, cache and order strategy |
| `almanac/` | Runtime naming, privacy safety, observability schemas, persistence helpers and migrations |
| `api/` | FastAPI read/write contracts, Today, actions, tax/performance and health |
| `frontend/` | Next.js dashboard consuming API contracts |
| `launchagents/` | Uninstalled macOS schedule examples |
| `examples/` | Sanitized state and crontab examples |
| `scripts/` | Initialization, public-safety and documentation checks |
| `tests/` | Unit, contract, integration and state-isolation tests |

<!-- ROOT_MODULES_END -->

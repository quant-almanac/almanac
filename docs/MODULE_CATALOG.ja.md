# ALMANAC モジュール台帳

*[English](MODULE_CATALOG.md) · [システム仕様](SYSTEM_SPEC.ja.md)*

これは責務の地図であり、全fileがliveという意味ではありません。lifecycleは大分類で、最終的な権威はsource、test、runtime modeです。root直下の全Python moduleをmarker内に列挙し、source treeと台帳がずれると`scripts/check_docs_consistency.py`が失敗します。

<!-- ROOT_MODULES_START -->

## 1. Entry point・UI・運用

| Lifecycle | Modules | 境界 |
|---|---|---|
| live | `portfolio_analyst.py`, `portfolio_agent.py`, `bot_commands.py`, `telegram_bot.py`, `generate_dashboard.py`, `weekly_report.py` | 日次CLI/API向け分析と人への表示 |
| live | `daily_health_check.py`, `watchdog.py`, `alert.py`, `nightly_recheck.py`, `post_run_verify.py` | health、stale出力検知、実行後整合 |
| optional | `decision_support.py` | 主Next.js console外のon-demand判断支援 |
| shared | `utils.py` | atomic I/O、secrets、heartbeat、FX cache、共通処理 |

大規模なorchestration本体は`analyst/` packageです。§15ではなく本台帳§14も参照してください。

## 2. LLM routing・分析・cost

| Lifecycle | Modules | 境界 |
|---|---|---|
| live | `analyzer.py`, `llm_adapters.py`, `model_router.py`, `llm_cost_accounting.py`, `llm_run_context.py`, `analysis_output_validation.py` | provider transport、role routing、実行ID、cost、金融説明の決定論的検証 |
| live/observe | `red_team_ledger.py`, `compare_harness.py`, `human_feedback_log.py` | 反証証拠、harness比較、人のlabel |

## 3. Portfolio・口座・household policy

| Lifecycle | Modules | 境界 |
|---|---|---|
| live | `portfolio_manager.py`, `portfolio_integrity.py`, `position_identity.py` | portfolio構築、integrity、owner/broker/account identity |
| live | `discretionary_funding.py`, `contribution_ledger.py`, `contribution_recorder.py`, `contribution_schedule.py` | 新規資金とexternal cash flowの権威 |
| advisory/shadow | `investment_policy.py`, `currency_policy.py` | 集中/cash policyと旧asset-currency target検証 |
| human-only | `credit_card_investment.py`, `espp_plan_manager.py`, `employee_plan_exit.py` | recurring card・持株会集中/exit |
| advisory | `nisa_allocator.py`, `nisa_migration_planner.py` | owner別NISA枠・migration |
| live/advisory | `rebalance_engine.py`, `rebalance_planner.py` | driftと人が行うrebalance plan |
| live | `cash_wallet_projection.py` | wallet別の読み取り専用cash投影監査。broker確認済み買付余力を置換しない |

## 4. Execution lifecycle と安全

| Lifecycle | Modules | 境界 |
|---|---|---|
| live | `action_stage_log.py`, `action_state_tracker.py`, `execution_invalidation.py` | stage監査、action lifecycle、invalidation |
| live | `execution_readiness.py`, `execution_safety.py`, `execution_preflight.py`, `policy_engine.py` | 決定論的採用、freshness、署名付き発注前安全gate |
| live | `feature_controls.py` | runtime機能switch、実効状態の理由、fail-closed UI contract |
| live | `order_intent_resolver.py`, `exit_sizing.py`, `execution_explanation.py`, `action_amounts.py` | 冪等intent、決定論的数量、構造化金額表示 |
| observe | `execution_plan_engine.py`, `execution_plan_observer.py`, `execution_quality.py` | plan budget、enforce readiness、shortfall |
| live | `behavioral_guard.py`, `margin_manager.py` | 実損益P&L shock guardと信用管理 |

## 5. Risk・allocation・quant研究

| Lifecycle | Modules | 境界 |
|---|---|---|
| live | `risk_policy.py`, `risk_engine.py`, `portfolio_risk_returns.py`, `risk_model_validation.py` | 版管理された固定上限、VaR/CVaR、保有based return、Kupiec |
| optional | `portfolio_optimizer.py`, `optimize.py`, `bl_alpha_sources.py` | allocation objectiveと独立BL view |
| live | `capital_allocator.py` | 既存安全gate通過後の通常risk-increasing actionを決定論的に最終選抜 |
| live/advisory | `market_regime_v2.py`, `regime_params.py`, `vix_classification.py`, `vix_tracker.py` | regime/rate/shock policy、volatility state |
| shadow/manual-promote | `drawdown_state_machine.py`, `drawdown_enforcement.py` | flow調整DDのhysteresisと明示的enforcement昇格 |
| live | `drawdown_dca_engine.py`, `leveraged_decay_monitor.py` | DCA ladderとleveraged decay |
| research/default-deny | `ginn_model.py` | candidate training、promotion manifest、GARCH fallback |
| shadow | `kelly_sizing.py`, `kelly_shadow.py` | 推薦統計とhalf-Kelly反実仮想 |
| shadow | `fx_exposure.py`, `fx_hedge_manager.py`, `fx_hedge_policy.py`, `fx_actual_hedge_state.py` | look-through通貨、target、broker actual、shadow |
| measurement | `factor_attribution.py` | factor regression/attribution |

## 6. Market data・metadata・state producer

| Lifecycle | Modules | 境界 |
|---|---|---|
| live | `data_fetcher.py`, `technical_signals.py`, `chart_analyzer.py`, `options_fetcher.py` | price history、technical、chart、options |
| live | `macro_fetcher.py`, `macro_event_calendar.py`, `event_calendar.py`, `follow_rate_analyzer.py` | macro/rates/event calendar |
| live | `news_fetcher.py`, `geopolitical_monitor.py`, `earnings_proximity_manager.py`, `earnings_season.py` | news/geopolitics/earnings blackout |
| live | `sector_rotation.py`, `sector_strength_updater.py` | sector state |
| shared | `instrument_metadata.py`, `pseudo_tickers.py`, `download_tickers.py`, `expand_tickers.py` | instrument identity、synthetic ID、universe |
| maintenance | `parquet_rebuilder.py`, `sync_jp_universe_prices.py` | local price store修復、JP同期 |
| live | `analysis_snapshot.py`, `freshness_policy.py`, `claim_provenance.py`, `holdings_freshness.py` | decision input凍結、refresh/stale不変条件、claim lineage |

## 7. Screener とcandidate計測

| Lifecycle | Modules | 境界 |
|---|---|---|
| live | `screener.py`, `short_screener.py`, `margin_long_screener.py`, `long_term_screener.py` | 主long/short/margin/long-term lane |
| live | `news_screener.py`, `social_screener.py`, `pair_screener.py`, `squeeze_detector.py`, `overnight_gap_scanner.py` | news/social/pair/squeeze/gap |
| shared | `screening_helpers.py`, `short_universe.py`, `jp_loanability.py`, `kabu_mini_eligibility.py` | 共通screen、short在庫、JP売買可否 |
| observe | `screener_shadow_book.py`, `swing_lane_kpi.py`, `signal_tracker.py` | candidate outcome、swing KPI、signal history |
| maintenance/legacy | `screener_backup.py`, `screen_fix.py` | 互換・修復 |

## 8. Disclosure・filing・feature promotion

| Lifecycle | Modules | 境界 |
|---|---|---|
| live | `edinet_fetcher.py`, `tdnet_fetcher.py`, `edgar_fetcher.py`, `ingest_disclosures.py` | 公式source取込 |
| live/observe | `disclosure_enrich.py`, `deterministic_disclosure_features.py`, `disclosure_feature_extractor.py` | filing本文とdeterministic/LLM feature |
| observe | `disclosure_feature_promotion.py`, `disclosure_shadow_book.py`, `disclosure_push.py`, `brief_disclosures.py` | promotion証拠、shadow P&L、表示 |
| live | `jp_buyback_parser.py`, `jp_dilution_parser.py`, `jp_guidance_parser.py`, `jp_monthly_sales_parser.py` | JP filingの決定論parser |
| audit | `extraction_audit_sampler.py`, `feature_validation.py` | 抽出sampleとfeature検証 |

## 9. Scenario・catalyst・topic・alternative

| Lifecycle | Modules | 境界 |
|---|---|---|
| live/advisory | `scenario_engine.py`, `scenario_strategy.py`, `scenario_invariants.py` | scenario state、playbook、invariant |
| observe | `catalyst_outcome_catchup.py`, `news_topic_analyzer.py`, `social_topic_analyzer.py` | catalyst/topic outcome・分類 |
| observe | `ipo_watch.py`, `insider_cluster.py`, `insider_tracker.py`, `insider_restrictions.py` | IPO/insider signal・制限 |

## 10. 税務

| Lifecycle | Modules | 境界 |
|---|---|---|
| compare/advisory | `tax_lot.py`, `tax_harvest_scanner.py`, `tax_optimizer.py` | inventory監査、総平均比較、人用税務idea |

tax表示/action consumerはAPI、analyst、NISA、ESPP、rebalanceにもあります。この3fileだけの変更は完全なtax migrationではありません。

## 11. Ledger・NAV・benchmark・reconciliation

| Lifecycle | Modules | 境界 |
|---|---|---|
| live | `event_ledger.py`, `event_ledger_backfill.py`, `cash_transactions_backfill.py` | append-only eventと制御backfill |
| live | `nav_recorder.py`, `benchmark_tracker.py` | measured NAVとbenchmark |
| maintenance | `nav_backfill.py`, `ledger_fx_reprice.py`, `ledger_trade_corrections.py`, `opening_balance_backfill_9432.py` | explicitなestimated/correction |
| live | `broker_balance_import.py`, `broker_position_import.py`, `broker_reconcile.py`, `broker_reconcile_cron.py`, `broker_cost_basis.py`, `execution_reconciliation.py` | broker証拠、position/cash取込、route修正overlay、取得原価の採用判定、ledger照合 |
| maintenance | `sync_broker_short_us.py`, `sync_broker_short_us_index.py`, `sync_jp_loanable.py`, `sync_jp_short_taisyaku.py`, `sync_jsf_lending.py` | borrowability/broker universe同期 |
| live | `config_clean_baseline.py`, `backup_manager.py` | clean計測境界と復元可能backup |

## 12. Tuning・backtest・governance

| Lifecycle | Modules | 境界 |
|---|---|---|
| optional | `auto_tune.py`, `tunable_params.py`, `tuning_advisor.py`, `threshold_calibrator.py` | guarded parameter評価/適用 |
| research | `backtest.py`, `backtest_full.py`, `backtest_wfo.py`, `backtest_comparison.py` | historical/walk-forward研究 |
| measurement | `recommendation_verifier.py`, `recommendation_verifier_walk_forward.py`, `behavior_coverage_report.py`, `monthly_governance_report.py` | outcome採点、coverage、governance |

## 13. One-off migration と互換utility

| Lifecycle | Modules | 境界 |
|---|---|---|
| one-off | `migrate_avgo_keys.py` | historical key migration |
| development | `test.py`, `test_analyzer.py` | legacy手動test。supported suiteは`tests/` |

## 14. Root外package

| Path | 責務 |
|---|---|
| `analyst/` | 日次orchestration、data gathering、model call、cache、order strategy |
| `almanac/` | runtime naming、privacy safety、observability schema、persistence helper、migration |
| `api/` | FastAPI read/write、Today、action、tax/performance、health |
| `frontend/` | API contractを消費するNext.js dashboard |
| `launchagents/` | installされないmacOS schedule example |
| `examples/` | sanitized state/crontab example |
| `scripts/` | init、public safety、documentation check |
| `tests/` | unit、contract、integration、state isolation |

<!-- ROOT_MODULES_END -->

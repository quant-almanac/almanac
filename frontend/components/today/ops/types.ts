/** GET /api/today のレスポンス型 (api/routes/today.py と対応) */
import type { DashboardDataHealth } from '@/lib/api'

export interface Lifecycle {
  id?: string | null
  status: string
  recommended_at?: string | null
  placed_at?: string | null
  filled_at?: string | null
  expiry_at?: string | null
  expiry_starts_at?: string | null
  expiry_ends_at?: string | null
  expiry_deferred_until_reprice?: boolean
  market_reprice_after?: string | null
  note?: string | null
}

export interface BoardRow {
  rank?: number
  source_rank?: number
  display_rank?: number
  tier?: string
  ticker?: string
  type?: string
  urgency?: string
  action?: string
  reason?: string
  amount_hint?: string
  confidence_pct?: number
  order_type?: string
  limit_price?: number
  decision_price?: number
  execution_reason?: string
  execution_note?: string
  expiry_minutes?: number
  target_5d_pct?: number
  target_20d_pct?: number
  cooldown_warning?: string
  return_20d_rank?: string
  plan_item_id?: string
  monthly_objective_id?: string
  execution_plan_decision?: string
  execution_plan_override?: string
  plan_remaining_before_jpy?: number
  plan_remaining_after_jpy?: number
  override_reason?: string
  budget_impact_jpy?: number
  ai_bounded_gate?: string
  analysis_id?: string
  action_state_id?: string | null
  execution_readiness?: 'ready' | 'review' | 'blocked' | string
  execution_block_reasons?: { code?: string; message?: string }[]
  execution_advisories?: { code?: string; message?: string }[]
  execution_plan_observed_decision?: string
  execution_plan_would_filter?: boolean
  execution_owner?: 'husband' | 'wife' | string
  execution_broker?: 'rakuten' | 'sbi' | string
  execution_account?: string
  execution_investment_type?: string
  execution_position_keys?: string[]
  market_quote_confirmation_required?: boolean
  market_order_window?: string
  expiry_starts_at?: string | null
  expiry_ends_at?: string | null
  market_reprice_required?: boolean
  market_reprice_after?: string | null
  order_intent_decision?: string
  filter_rule?: string
  minimum_executable_quantity?: number | null
  estimated_notional_jpy?: number
  impact_nav_pct?: number | null
  days_pending?: number
  historical_backlog?: boolean
  lifecycle: Lifecycle
}

export interface FunnelStage {
  key: string
  label: string
  count: number
  note?: string
  hot?: boolean
}

export interface RedTeamVerdict {
  verdict: string
  // 旧形式 (tier analysis): hypothesis + reason
  hypothesis?: string
  reason?: string
  // 新形式 (synthesis): ticker + action + verdict_reason + adopted_as
  ticker?: string
  action?: string
  verdict_reason?: string
  adopted_as?: string
}

export interface LaneVerdict {
  lane: string
  ticker?: string
  verdict: string
  verdict_reason?: string
  adopted_as?: string
}

export interface RedTeamAttack {
  ticker?: string
  action?: string
  expected_return_pct?: number
  rationale?: string
  risk_note?: string
  model?: string
}

export interface Engine {
  funnel: FunnelStage[]
  stance_reason?: string
  operational_stance?: OperationalStance
  red_team: RedTeamVerdict[]
  lanes: LaneVerdict[]
  attacks: RedTeamAttack[]
  underutilized: RedTeamAttack[]
}

export interface OperationalStance {
  code?: string
  label?: string
  reason?: string
}

export type TierReport = Record<string, unknown>

export interface ScoreRow {
  agent: string
  role: string
  n?: number
  measured_n?: number
  win_rate?: number
  excess_bps?: number
  payoff?: number
  measured?: boolean
}

export interface RecentFill {
  ticker?: string
  action_type?: string
  detail?: string
  filled_at?: string
  limit_price?: number
}

export interface Scorecard {
  as_of?: string
  horizon_days?: number
  rows: ScoreRow[]
  status_counts: Record<string, number>
  recent_fills: RecentFill[]
}

export interface Allocation {
  currency: {
    current_usd_pct?: number | null
    usd_target_pct?: number
    jpy_target_pct?: number
    confidence_pct?: number
    valid_until?: string
    reason?: string
    review_triggers: string[]
    risk_notes?: string
  }
  nisa: {
    husband: NisaCapacityView
    wife: NisaCapacityView
  }
  risk_warnings: string[]
  stop_loss_alerts: string[]
  ginn_vol: Record<string, number>
  margin_health?: string
  margin_summary?: string
}

export interface NisaCapacityView {
  broker?: string
  growth_remaining: number
  tsumitate_remaining: number
  baseline?: string
  age_days?: number | null
  unattributed_count?: number
  unattributed_execution_ids?: string[]
  growth_readiness?: string
  tsumitate_readiness?: string
}

export interface Command {
  scenario?: string
  stance?: string
  health?: string
  operational_stance?: OperationalStance
  vix?: number
  vix_status?: string
  yield_10y?: number
  fear_greed?: number
  guard: {
    new_entry_allowed?: boolean
    trading_allowed?: boolean
    alerts: string[]
    daily_pnl_pct?: number
    monthly_pnl_pct?: number
  }
  usd_ratio_pct?: number | null
  usd_target_pct?: number
  data_age_hours?: number | null
}

export interface ChartPoint {
  d: string
  c?: number
  v?: number
}

export interface ChartsData {
  pnl: { d: string; v: number }[]
  tickers: Record<string, { d: string; c: number }[]>
  holdings?: Record<string, { d: string; c: number }[]>
}

export interface AlmanacEvent {
  t?: string
  date?: string
  label: string
  kind: string
  ticker?: string | null
}

export interface PastTrade {
  date: string
  kind: string
  ticker?: string
  side: string
  detail?: string
}

export interface AlmanacData {
  today: AlmanacEvent[]
  sessions: {
    id?: string
    label: string
    market?: 'JP' | 'US' | string
    phase?: 'pre' | 'regular' | 'after' | string
    start: string
    end: string
    timezone?: string
    is_open_day?: boolean
  }[]
  upcoming: AlmanacEvent[]
  past: PastTrade[]
  pnl_by_date: Record<string, number>
  notes: string[]
  is_weekday: boolean
  today_str?: string
}

export interface DeltaData {
  prev_as_of?: string
  stance_prev?: string
  stance_now?: string
  added: { ticker: string; type: string }[]
  removed: { ticker: string; type: string }[]
  kept: { ticker: string; type: string }[]
}

export interface ExecutionPlanItem {
  plan_item_id?: string
  label: string
  objective?: string
  status?: string
  priority?: number
  normal_budget_jpy?: number
  requested_jpy?: number
  shared_pool_jpy?: number | null
  consumed_jpy?: number
  remaining_jpy?: number
  preferred_tickers: string[]
  consumed_by_count: number
  source_reasons: string[]
  today_decision?: { decision?: string; reason?: string }
}

export interface ExecutionPlanRationale {
  reason_code?: string
  message?: string
}

export interface ScenarioSummary {
  active: number
  partial: number
  watching: number
  alert_level: string | null
  evaluated_at: string | null
}

export interface ExecutionPlan {
  status: string
  as_of?: string | null
  age_hours?: number | null
  horizon: { month?: string; week_start?: string; week_end?: string }
  budgets: {
    monthly_total_jpy?: number
    monthly_remaining_jpy?: number
    monthly_discretionary_budget_jpy?: number
    monthly_base_consumed_jpy?: number
    monthly_base_remaining_jpy?: number
    approved_contribution_released_this_month_jpy?: number
    normal_pool_available_jpy?: number
    opportunity_pool_available_jpy?: number
    weekly_normal_jpy?: number
    weekly_opportunity_reserve_jpy?: number
    weekly_defensive_reserve_jpy?: number
    max_single_normal_action_jpy?: number
    max_single_opportunity_action_jpy?: number
    h2_hard_cap_jpy?: number
    budget_source?: string
    scheduled_contributions_remaining_jpy?: number
  }
  consumption: {
    normal_consumed_jpy?: number
    open_order_consumed_jpy?: number
    filled_consumed_jpy?: number
    monthly_open_order_consumed_jpy?: number
    monthly_filled_consumed_jpy?: number
    monthly_consumed_jpy?: number
    monthly_remaining_jpy?: number
    unattributed_monthly_open_order_count?: number
    unattributed_monthly_open_order_notional_jpy?: number
    unattributed_monthly_filled_count?: number
    unattributed_monthly_filled_notional_jpy?: number
    unattributed_monthly_total_count?: number
    unattributed_monthly_total_notional_jpy?: number
    unattributed_monthly_buy_total_count?: number
    unattributed_monthly_buy_total_notional_jpy?: number
    unattributed_monthly_sell_total_count?: number
    unattributed_monthly_sell_total_notional_jpy?: number
    unattributed_monthly_unpriced_count?: number
    remaining_normal_jpy?: number
    remaining_opportunity_jpy?: number
    normal_plan_budget_consumed_jpy?: number | null
    normal_plan_budget_consumed_pct?: number | null
    normal_matched_notional_jpy?: number | null
    normal_open_order_matched_notional_jpy?: number | null
    normal_filled_matched_notional_jpy?: number | null
    opportunity_matched_notional_jpy?: number | null
    monthly_attribution_incomplete?: boolean
  }
  summary: {
    items_total: number
    active_items: number
    covered_items: number
    board_count: number
    plan_filtered_count: number
  }
  items: ExecutionPlanItem[]
  contributions?: {
    approved_contribution_count?: number
    released_this_month_jpy?: number
    available_jpy?: number
    available_normal_jpy?: number
    available_opportunity_jpy?: number
    sources?: Array<{
      id: string
      source?: string
      bucket?: 'normal' | 'opportunity' | string
      owner?: string
      broker?: string
      amount_jpy?: number
      available_jpy?: number
      start_month?: string
      release_months?: number
      note?: string
    }>
  }
  today_decision: { code: string; label: string; reason: string }
  filtered_summary: Record<string, number>
  filtered_examples: {
    ticker?: string
    type?: string
    code: string
    reason?: string
    plan_item_id?: string
    confidence_pct?: number
    estimated_notional_jpy?: number
  }[]
  order_intent_review?: {
    count: number
    summary: Record<string, number>
    items: {
      ticker?: string
      type?: string
      action?: string
      decision: string
      label: string
      reason?: string
      existing_order_id?: string
      existing_order_status?: string
      existing_order_notional_jpy?: number
      recommended_notional_jpy?: number
      incremental_notional_jpy?: number
      material_change: boolean
      non_executable: true
    }[]
  }
  gate_observation?: {
    mode?: string
    warning?: string
    observed_decisions?: Record<string, number>
    would_filter_count?: number
    batch_allocation?: {
      applied?: boolean
      accepted_count?: number
      over_budget_count?: number
      error?: string
    }
    readiness?: {
      ready_for_enforce?: boolean
      observe_run_count?: number
      trading_day_count?: number
      classification_count?: number
      classification_error_count?: number
      metadata_mismatch_count?: number
      blockers?: string[]
    }
  }
  warnings: string[]
  no_action_rationale: Array<string | ExecutionPlanRationale>
}

export interface TodayOps {
  as_of?: string
  generated_at: string
  portfolio_total: number
  portfolio_snapshot: TodayPortfolioSnapshot
  snapshot_meta: {
    snapshot_id: string
    analysis_as_of?: string
    portfolio_as_of?: string
    generated_at: string
    status: 'healthy' | 'data_stale' | 'analysis_old' | 'degraded'
    data_health?: DashboardDataHealth
  }
  command: Command
  focus: BoardRow | null
  board: BoardRow[]
  review_board?: BoardRow[]
  decision_summary?: {
    candidate_count: number
    executable_count: number
    review_count: number
    filtered_count: number
    deferred_count: number
    no_action_classification?: string | null
    reason_counts: Record<string, number>
    count_conservation_ok?: boolean | null
  }
  board_notes: { label: string; text: string }[]
  backlog: BoardRow[]
  pending_portfolio_applications?: Array<{
    id?: string
    ticker?: string
    direction?: string
    quantity?: number
    price?: number
    account?: string
    investment_type?: string
    execution_owner?: string
    execution_broker?: string
    saved_at?: string
    reasons: Array<{ code?: string; message?: string }>
    candidate_position_keys: string[]
  }>
  cash_status?: Array<{
    key: string
    owner: string
    broker: string
    currency: string
    effective_balance?: number
    reported_balance?: number
    reported_as_of?: string
    ledger_delta_since_report?: number
    balance_status: string
    reconciliation_required: boolean
    available_for_new_buy?: number
  }>
  engine: Engine
  report: Record<string, TierReport>
  scorecard: Scorecard
  allocation: Allocation
  charts: ChartsData
  almanac: AlmanacData
  delta: DeltaData | null
  benchmark: BenchmarkData | null
  execution_plan?: ExecutionPlan
  scenario_summary?: ScenarioSummary
  holdings_intel: Record<string, HoldingIntel>
  pulse: { vix?: number }
}

export interface TodayPortfolioSnapshot {
  positions?: Array<{
    ticker?: string
    name?: string
    currency?: string
    shares?: number
    current_price?: number
    value_jpy?: number
    unrealized_jpy?: number
    unrealized_pct?: number
    investment_type?: string
    account?: string
  }>
  total_jpy?: number
  cash_jpy?: number
  cash_total_jpy?: number
  cash_jpy_native?: number
  cash_usd_native?: number
  cash_usd_jpy?: number
  as_of?: string
  error?: string
}

export interface HoldingIntel {
  note?: string
  tier?: string
  stop_loss?: string
  ginn_vol?: number
}

export interface BenchmarkData {
  dates: string[]
  portfolio: number[]
  sp500?: (number | null)[]
  nikkei?: (number | null)[]
  outperf: { sp500?: number; nikkei?: number }
  method: 'modified_dietz'
  confirmed: boolean
  clean_ok: boolean
  clean_since?: string | null
  start_date?: string | null
  end_date?: string | null
  period_days_actual?: number | null
  net_cash_flow?: number | null
  basis: {
    portfolio: 'jpy_modified_dietz_twr'
    sp500: 'jpy_unhedged_price_return'
    nikkei: 'jpy_price_return'
  }
}

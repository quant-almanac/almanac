export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || 'http://localhost:8000'

// P0-3: 書き込み系は X-API-Key ヘッダを注入。
// 開発時は ALLOW_UNAUTH=1 が FastAPI 側で有効なので省略可。
// 本番は frontend/.env.local に NEXT_PUBLIC_ALMANAC_API_KEY を設定。
const API_KEY = process.env.NEXT_PUBLIC_ALMANAC_API_KEY || ''

export const fetcher = (url: string) =>
  fetch(API_BASE + url).then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    return r.json()
  })

/**
 * 書き込み系 API を叩くためのヘルパー。X-API-Key を自動注入する。
 * 既存の fetch(API_BASE + url, { method: 'POST', ... }) を apiFetch(url, { method: 'POST', ... }) に置換可。
 */
export const apiFetch = (url: string, init: RequestInit = {}) => {
  const headers = new Headers(init.headers || {})
  if (API_KEY) headers.set('X-API-Key', API_KEY)
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  return fetch(API_BASE + url, { ...init, headers })
}

export function apiErrorMessage(json: unknown, fallback: string): string {
  const obj = json as { detail?: unknown; error?: unknown; message?: unknown } | null
  const detail = obj?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail.map(item => {
      const rec = item as { msg?: unknown; loc?: unknown }
      const loc = Array.isArray(rec.loc) ? rec.loc.join('.') : ''
      const msg = typeof rec.msg === 'string' ? rec.msg : JSON.stringify(item)
      return loc ? `${loc}: ${msg}` : msg
    })
    return parts.join(' / ')
  }
  if (typeof obj?.error === 'string') return obj.error
  if (typeof obj?.message === 'string') return obj.message
  return fallback
}

// --- 型定義 ---

export interface Briefing {
  generated_at?: string
  date?: string
  portfolio_value?: number
  daily_pnl_pct?: number
  monthly_pnl_pct?: number
  regime?: string
  guard_status?: string[]
  summary?: string
  market_comment?: string
  actions?: string[]
  risk_alert?: string
  opportunity?: string
}

export interface Guard {
  date?: string
  month?: string
  daily_pnl_jpy?: number
  daily_pnl_pct?: number
  monthly_pnl_jpy?: number
  monthly_pnl_pct?: number
  portfolio_value?: number
  active_trades?: number
  short_positions?: number
  new_entry_allowed?: boolean
  trading_allowed?: boolean
  nisa_exception_allowed?: boolean  // レジームフリップ例外: BULL転換中はNISA積立・買増OK
  alerts?: string[]
  last_updated?: string
}

export interface Regime {
  spy_above?: boolean
  nk_above?: boolean
  regime?: string
  updated?: string
}

export interface NewsSentiment {
  positive: number
  negative: number
  neutral: number
  total: number
  as_of?: string
}

export interface DashboardSourceHealth {
  source_file?: string
  exists?: boolean
  timestamp?: string | null
  timestamp_source?: string | null
  age_hours?: number | null
  stale_after_hours?: number
  stale?: boolean
}

export interface DashboardDataHealth {
  sources?: Record<string, DashboardSourceHealth>
  stale_sources?: string[]
  missing_sources?: string[]
  stale_count?: number
  missing_count?: number
  ok?: boolean
  checked_at?: string
}

export interface DashboardData {
  guard: Guard
  regime: Regime
  portfolio_total: number
  news_sentiment?: NewsSentiment | null
  data_health?: DashboardDataHealth
}

export interface MacroData {
  fed_rate?: number | null
  yield_10y?: number | null
  yield_2y?: number | null
  yield_spread?: number | null
  yield_inverted?: boolean
  cpi_yoy?: number | null
  unemp_rate?: number | null
  macro_adj?: number
  source?: string
  cached_at?: string
  error?: string
  // VIX恐怖指数（yfinance ^VIX）
  vix?: number | null
  vix_capitulation?: boolean   // VIX > 40: capitulation zone
  vix_fear?: boolean           // VIX > 30: high fear
  vix_status?: 'capitulation' | 'fear' | 'elevated' | 'normal' | 'unknown'
}

export interface Position {
  key?: string
  ticker: string
  name?: string
  currency?: string
  shares?: number          // 数量（quantityではなくshares）
  current_price?: number
  value_jpy?: number
  cost_jpy?: number
  unrealized_jpy?: number
  unrealized_pct?: number  // 損益%（gain_pctではなくunrealized_pct）
  sector?: string
  investment_type?: string
  account?: string
}

export interface BreakdownItem {
  value_jpy: number
  ratio: number            // 0〜1 の小数（例: 0.65 = 65%）
}

export interface PortfolioData {
  positions: Position[]
  currency_breakdown: Record<string, BreakdownItem>
  sector_breakdown: Record<string, BreakdownItem>
  total_jpy: number
  /** @deprecated cash_jpy is total JPY-equivalent cash; use cash_total_jpy. */
  cash_jpy?: number
  cash_total_jpy?: number
  cash_jpy_native?: number
  cash_usd_native?: number
  cash_usd_jpy?: number
  fx_rate?: number
  as_of?: string
  error?: string
}

export interface RiskData {
  var_95: number
  cvar_95: number
  current_dd: number
  max_dd: number
  drawdown_series: number[]
  sample_size: number
  error?: string
}

export interface Signal {
  entry_price?: number
  target_price?: number
  stop_loss?: number
  reason?: string
  holding_period?: string
  score?: number
  signal_date?: string
}

export interface SignalsData {
  signals: Record<string, Signal>
  candidates: Record<string, unknown>[]
}

export interface RebalanceItem {
  ratio: number         // 現在比率 (0-1)
  value_jpy: number
  target?: number       // 目標比率
  target_min?: number
  target_max?: number
  deviation: number     // 現在 - 目標
  level: 'ok' | 'warning' | 'action_needed'
}

export interface RebalanceAction {
  priority: number
  level: 'ok' | 'warning' | 'critical'
  type: 'buy' | 'reduce'
  currency?: string
  sector?: string
  message: string
  amount_jpy: number
}

export interface RebalanceData {
  currency?: {
    status: string
    data: Record<string, RebalanceItem>
    actions: RebalanceAction[]
  }
  sector?: {
    status: string
    data: Record<string, RebalanceItem>
    actions: RebalanceAction[]
  }
  action_plan: RebalanceAction[]
  core_total_jpy?: number
  core_position_count?: number
  error?: string
}

// ── 信用建玉 ──
export interface MarginPosition {
  id?: number
  ticker: string
  side: 'long' | 'short'
  shares: number
  entry_price: number
  current_price?: number
  currency: string
  entry_date?: string
  expiry?: string
  closed?: boolean
  unrealized_pnl_jpy?: number
  pnl_pct?: number
  memo?: string
}

export interface MarginData {
  open_positions: MarginPosition[]
  closed_positions: MarginPosition[]
  collateral: number
  maintenance_ratio: number
  margin_status: 'safe' | 'caution' | 'warning' | 'emergency'
  total_unrealized: number
  total_realized: number
  expiry_alerts: { ticker: string; side: string; days_left: number; expiry: string }[]
  fx_usdjpy: number
  as_of: string
  error?: string
}

export interface MarketItem {
  price: number
  change: number
  level?: string       // VIX
  ma50?: number        // SPY, NK225
  ma50_diff?: number   // SPY, NK225
  inverted?: boolean   // YIELD_SPREAD
}

export interface MarketData {
  VIX?: MarketItem
  SPY?: MarketItem
  NK225?: MarketItem
  USDJPY?: MarketItem
  US10Y?: MarketItem
  US2Y?: MarketItem
  GOLD?: MarketItem
  OIL?: MarketItem
  DXY?: MarketItem
  YIELD_SPREAD?: MarketItem
  as_of?: string
  error?: string
}

export interface ShortCandidate {
  ticker: string
  name?: string
  rsi?: number
  ma50_pct?: number
  reason?: string
  sector?: string
}

// ── NISA ──
export interface NisaHolding {
  name: string
  account: string
  units?: number
  shares?: number
  avg_nav?: number
  avg_cost?: number
  current_nav?: number
  current_price?: number
  value: number
  cost_basis_estimate?: number
  cost_basis?: number
  auto_invest?: boolean
  daily_amount?: number
}

export interface NisaPerson {
  broker: string
  tsumitate_limit_annual: number
  growth_limit_annual: number
  lifetime_limit: number
  tsumitate_used_this_year: number
  growth_used_this_year: number
  lifetime_used_estimate: number
  tsumitate_schedule: {
    type: string
    amount_per_day?: number
    fund: string
    annual_estimate: number
    note?: string
  } | null
  holdings: Record<string, NisaHolding>
  notes: string
  tsumitate_remaining?: number
  growth_remaining?: number
  lifetime_remaining?: number
  tsumitate_used_pct?: number
  growth_used_pct?: number
  lifetime_used_pct?: number
  holdings_total_value?: number
  holdings_total_cost?: number
  holdings_total_unrealized?: number
}

export interface NisaData {
  husband: NisaPerson
  wife: NisaPerson
  last_updated: string
  placement_status?: 'display_only'
  placement_proposals?: {
    ticker: string
    name: string
    score: number
    recommended_account: string
    current_account?: string
    expected_return_pct: number
    dividend_yield: number
    capital_gain_share: number
    foreign_dividend_tax_credit_relevant: boolean
    loss_harvest_flexibility_relevant: boolean
    display_only: boolean
  }[]
  error?: string
}

// ── 長期スクリーニング ──
export interface LongTermCandidate {
  ticker: string
  name: string
  sector: string
  currency: string
  price: number
  score: number
  pe_ratio?: number
  forward_pe?: number
  eps_growth?: number
  rev_growth?: number
  gross_margin?: number
  fcf_yield?: number
  roe?: number
  buy_pct?: number
  reco_mean?: number
  priority_sector?: boolean
  ai_thesis?: string
}

export interface BatchStatus {
  status: 'none' | 'submitted' | 'completed' | 'error'
  batch_id?: string | null
  submitted_at?: string | null
  count?: number
  error?: string
}

export interface OptimizationResult {
  weights: Record<string, number>
  expected_return?: number
  volatility?: number
  sharpe?: number
  method?: string
  regime_weights?: Record<string, number>
  error?: string
}

export interface ScreeningData {
  long_term?: {
    passed: LongTermCandidate[]
    rejected_count?: number
    total_screened?: number
    as_of?: string
    error?: string
  }
  optimization?: {
    tickers?: string[]
    regime?: string
    results?: Record<string, OptimizationResult>
    recommended?: string
    as_of?: string
    error?: string
  }
}

// ── Admin ──
export interface EsppData {
  ticker: string
  monthly_amount: number
  current_shares: number
  avg_cost: number
  adjusted_cost: number
  total_invested: number
  total_incentive: number
  incentive_rate: number
  hold_limit_pct: number
  current_price?: number
  market_value?: number
  unrealized_jpy?: number
  unrealized_pct?: number
  adjusted_unrealized_pct?: number
  last_purchase_date?: string
  purchase_history?: { date: string; shares: number; price: number; cost: number; incentive: number; type: string }[]
  error?: string
}

export interface CreditCardPlan {
  monthly_amount: number
  fund: string
  account_type: string
  broker: string
  card: string
  point_rate: number
  total_invested: number
  total_points: number
  notes: string
}

export interface AdminData {
  espp?: EsppData
  credit_card?: Record<string, CreditCardPlan | { _summary: { total_monthly_amount: number; annual_points_estimate: number } }>
}

// ── AI ポートフォリオ分析 ──
export interface AiAction {
  rank: number
  urgency: 'high' | 'medium' | 'low'
  type: string
  ticker?: string | null
  action: string
  reason: string
  amount_hint?: string
  account?: string | null
  price_target?: string
  stop_loss?: string | null
  tier?: string
  // v5.1: 執行方式 AI 決定
  order_type?: 'market' | 'limit' | 'stop_limit' | null
  limit_price?: number | null
  limit_price_band?: { low: number; high: number } | null
  expiry_minutes?: number | null
  execution_reason?: string | null
  decision_price?: number | null
  // No-Transaction Band（Nakagawa流）
  no_trade_zone?: boolean | null
  skip_reason?: string | null
  // Multi-Horizon target hint
  target_5d_pct?: number | null
  target_20d_pct?: number | null
  // optional confidence
  confidence_pct?: number | null
  return_20d_rank?: 'top' | 'middle' | 'bottom' | null
  filtered_reason?: string | null
  analysis_id?: string | null
}

export interface AiTierAnalysis {
  health: 'good' | 'caution' | 'critical'
  health_reason?: string
  summary?: string
  priority_actions?: AiAction[]
  new_candidates?: { ticker: string; reason: string; score?: number }[]
  profit_taking?: { ticker: string; reason: string; target_pct?: number }[]
  new_entries?: { ticker: string; reason: string; risk_level?: string; entry_condition?: string }[]
  short_opportunities?: { ticker: string; reason: string; entry_zone?: string; rsi?: number; risk_reward?: string; catalyst?: string }[]
  optimization_insight?: string
  rebalance_summary?: string
  nisa_strategy?: string
  high_return_opportunity?: string
  medium_high_return_strategy?: string
  high_risk_high_return?: string
  watchlist_alert?: string
  stop_loss_alerts?: string[]
  crisis_opportunity?: string
  news_impact?: string
  signals_quality?: string
  error?: string
}

export interface AiSynthesis {
  analysis_id?: string
  overall_stance: 'defensive' | 'neutral' | 'moderately_aggressive' | 'aggressive'
  stance_reason?: string
  priority_actions: AiAction[]
  _filtered_actions?: AiAction[]
  _filtered_action_summary?: Record<string, number>
  no_action_rationale?: string
  jp_no_buy_rationale?: string[]
  margin_no_buy_rationale?: string[]
  short_no_action_rationale?: string[]
  kabu_mini_verification_needed?: Array<{
    ticker?: string
    requested_channel?: string | null
    action_type?: string | null
    reason?: string
    estimated_notional_jpy?: number
    threshold_jpy?: number
  }>
  post_filter?: {
    input_count?: number
    kept_count?: number
    filtered_count?: number
    summary?: Record<string, number>
    all_actions_filtered?: boolean
    cooldown_scope?: string
    policy_accepted_count?: number | null
    warning?: string
  }
  telegram_message: string
  risk_warnings: string[]
  opportunity_highlights: string[]
  weekly_theme: string
  geopolitical_note?: string
  error?: string
}

export interface AiAnalysisData {
  as_of?: string
  scenario_key?: string
  portfolio_total?: number
  long_analysis?: AiTierAnalysis
  medium_analysis?: AiTierAnalysis
  short_analysis?: AiTierAnalysis
  synthesis?: AiSynthesis
  cache_valid?: boolean
  refresh_running?: boolean
  error?: string
}

// ── シナリオ戦略 ──
export interface HighReturnOpportunity {
  type: 'short' | 'long_screen'
  ticker?: string
  reason?: string
  rsi?: number
  sector?: string
  icon?: string
}

export interface StrategyData {
  scenario: 'BULL' | 'NEUTRAL' | 'BEAR' | 'CRASH'
  scenario_name: string
  scenario_icon: string
  scenario_color: string
  scenario_description: string
  cash_ratio_target: number
  long_bias: boolean
  short_allowed: boolean
  leverage_allowed: boolean
  actions: string[]
  opportunity: { medium_risk: string[]; high_risk: string[] }
  crisis_protocol: string[]
  high_return_opportunities: HighReturnOpportunity[]
  regime: { spy_above?: boolean; nk_above?: boolean; updated?: string }
  briefing_summary: string
  risk_alert: string
  opportunity_note: string
  as_of: string
  error?: string
}

// ── Market Digest (X投稿用) ──
export interface MarketDigest {
  generated_at?: string
  date?: string
  regime?: string
  digest_type?: string   // "weekly_review" | "weekly_outlook" | undefined(平日)
  headline?: string
  body?: string
  regime_comment?: string
  learning?: string      // 週次振り返りの「今週の学び」
  hashtags?: string[]
  error?: string
}

// ── シナリオシステム ──
export interface ScenarioSignalDetail {
  type: 'news' | 'indicator' | 'technical'
  key: string
  matched: boolean
  detail: string
  value?: number
  threshold?: number
}

export interface ScenarioItem {
  status: 'dormant' | 'watching' | 'active'
  readiness: number
  signals_met: number
  signals_total: number
  signal_details: ScenarioSignalDetail[]
  recommended_actions: Record<string, unknown>
  first_detected?: string | null
  last_evaluated?: string | null
  // プレイブックから
  name: string
  icon: string
  color: string
  description: string
  actions: Record<string, unknown>
  priority: string
}

export interface ScenarioSourceHealth {
  timestamp?: string
  age_hours?: number | null
  stale_after_hours?: number
  stale?: boolean
  news_count?: number
  active_alert_count?: number
  keyword_match_count?: number
  assessment_error_count?: number
}

export interface ScenarioDataHealth {
  scenario?: ScenarioSourceHealth
  geopolitical?: ScenarioSourceHealth
  technical?: ScenarioSourceHealth
  vix?: ScenarioSourceHealth
  macro?: ScenarioSourceHealth
  has_stale_sources?: boolean
  has_collection_warnings?: boolean
}

export interface ScenarioRefreshStatus {
  state?: 'queued' | 'running' | 'succeeded' | 'warning' | 'failed'
  queued_at?: string | null
  started_at?: string | null
  finished_at?: string | null
  returncode?: number | null
  scenario_evaluated_at_before?: string | null
  scenario_evaluated_at_after?: string | null
  state_updated?: boolean
  stdout_tail?: string
  stderr_tail?: string
  updated_at?: string
}

export interface ScenarioData {
  scenarios: Record<string, ScenarioItem>
  active_count: number
  watching_count: number
  overall_alert_level: 'calm' | 'elevated' | 'high' | 'critical'
  evaluated_at?: string
  data_health?: ScenarioDataHealth
  refresh_status?: ScenarioRefreshStatus
}

export interface SectorFlow {
  perf_5d: number
  relative_to_spy: number
}

export interface IndicatorData {
  data_health?: ScenarioDataHealth
  vix: {
    level?: number
    classification?: string
    change_1d?: number
    change_5d?: number
    term_structure?: string
    oil_price?: number
    oil_change_5d?: number
    yield_spread?: number
    fear_greed_score?: number
    fear_greed_label?: string
    sector_flows?: Record<string, SectorFlow>
    cached_at?: string
  }
  technical: {
    market_breadth?: {
      pct_above_ma50?: number
      avg_rsi?: number
      bearish_divergences?: string[]
    }
    tickers?: Record<string, {
      price?: number
      rsi?: number
      rsi_signal?: string
      macd_histogram?: number
      macd_crossover?: string
      bb_pct_b?: number
      bb_signal?: string
      volume_ratio?: number
      composite_score?: number
      composite_signal?: string
    }>
    cached_at?: string
  }
  geopolitical: {
    active_alerts?: Array<{
      scenario_key: string
      headline: string
      severity: string
      detail?: string
    }>
    keyword_matches?: Array<{
      scenario_key: string
      scenario_name?: string
      score?: number
      matched_keywords?: string[]
      assessment_status?: string
    }>
    assessment_errors?: Array<{
      scenario_key: string
      reason?: string
    }>
    last_scan?: string
    news_summary?: string
  }
  macro: {
    fed_rate?: number
    yield_10y?: number
    cpi_yoy?: number
    unemp_rate?: number
  }
}

// ── 意思決定支援 ──
export interface DecisionLog {
  case_type: string
  ticker?: string
  memo?: string
  sonnet_analysis?: string
  opus_judgment?: string
  created_at?: string
  error?: string
}

// ── News Sentiment Screener ──
export interface NewsCandidate {
  ticker: string
  name: string
  sentiment_score: number
  bullish_count: number
  bearish_count: number
  neutral_count: number
  top_headlines: string[]
  signal: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
  sources: string[]
  last_article_at: string
  total_articles: number
}
export interface NewsSignalData {
  generated_at?: string
  total_tickers_scanned?: number
  candidates: NewsCandidate[]
  trending?: string[]
  market_mood?: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
  market_mood_score?: number
  error?: string
}

// ── Social Sentiment Screener ──
export interface StockTwitsEntry {
  bullish_pct: number
  bearish_pct: number
  message_count: number
  is_trending: boolean
  watchlist_count?: number
  sentiment: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
}
export interface OptionsUnusual {
  ticker: string
  call_volume: number
  put_volume: number
  call_put_ratio: number
  call_oi: number
  put_oi: number
  unusual: boolean
  bias: 'CALL_HEAVY' | 'PUT_HEAVY' | 'BALANCED'
}
export interface SocialSentimentData {
  generated_at?: string
  stocktwits: Record<string, StockTwitsEntry>
  options_unusual: OptionsUnusual[]
  top_bullish: string[]
  top_bearish: string[]
  trending_tickers: string[]
  error?: string
}

// ── AI アップグレード Phase 2 ──

export interface RegimeConsensus {
  hmm_regime: string
  macro_score: number
  vix: number
  vix_scale: string
  spy_above: boolean
  signals: { hmm: string; macro: string; vix: string; spy: string }
  bull_count: number
  bear_count: number
  confidence: number      // 0.25 / 0.5 / 0.75 / 1.0
  direction: string       // "強気" | "弱気" | "中立"
  conflicted: boolean
  error?: string
}

export interface RedTeamVerdict {
  ticker: string
  action: string
  verdict: 'adopt' | 'partial' | 'reject'
  verdict_reason: string
  adopted_as?: string
}

export interface RedTeamAttack {
  ticker: string
  action: string
  expected_return_pct?: number
  rationale?: string
  risk_note?: string
  model?: string
}

export interface RedTeamData {
  attacks?: RedTeamAttack[]
  underutilized?: string[]
  red_team_verdict?: RedTeamVerdict[]
  error?: string
}

export interface AiUpgradesData {
  bl_views: BLViews
  beliefs: AgentBeliefs
  regime_consensus: RegimeConsensus
  redteam?: RedTeamData
  error?: string
}

// ── AI アップグレード ──

export interface BLViewEntry {
  bull_view: number
  bear_view: number
  macro_view: number
  mean_view: number
  variance: number
  n_signals?: number
  avg_confidence?: number | null
}

export interface BLViews {
  views: Record<string, BLViewEntry>
  as_of?: string
  error?: string
}

export interface UpgradeComparisonMethod {
  annual_return: number
  annual_vol: number
  sharpe: number
  cvar_95: number
  max_dd: number
  calmar: number
  n_days?: number
  method?: string
}

export interface UpgradeComparison {
  comparison: Record<string, UpgradeComparisonMethod>
  period?: { start: string; end: string }
  rebalance?: string
  generated?: string
  summary?: { best_sharpe?: string; best_calmar?: string; bl_vs_max_sharpe?: number }
  error?: string
}

export interface AgentBelief {
  id: string
  ticker: string
  theme: string
  conviction_score: number
  rationale: string
  source_agent: string
  evidence?: string[]
  created_at: string
  last_updated: string
  expires_at: string
}

export interface AgentBeliefs {
  beliefs: AgentBelief[]
  last_updated?: string | null
  version?: string
  error?: string
}

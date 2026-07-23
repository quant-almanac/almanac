import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import PlanRail from '../PlanRail'
import type { AlmanacData, ExecutionPlan } from '../types'

const almanac: AlmanacData = {
  today: [],
  sessions: [],
  upcoming: [
    { date: '2026-07-14', label: '決算予定', kind: 'earnings' },
    { date: '2026-07-15', label: '積立日', kind: 'nisa' },
  ],
  past: [{ date: '2026-07-10', kind: 'order', ticker: '7203.T', side: 'buy', detail: '買付記録' }],
  pnl_by_date: {},
  notes: ['相場メモ'],
  is_weekday: true,
}

const plan: ExecutionPlan = {
  status: 'active',
  age_hours: 12,
  horizon: { month: '2026-07', week_start: '2026-07-13', week_end: '2026-07-19' },
  budgets: { monthly_total_jpy: 300_000, scheduled_contributions_remaining_jpy: 50_000 },
  consumption: {
    normal_plan_budget_consumed_pct: 19.7,
    normal_plan_budget_consumed_jpy: 10_347,
    normal_matched_notional_jpy: 387_055,
    normal_open_order_matched_notional_jpy: 92_455,
    normal_filled_matched_notional_jpy: 294_600,
    remaining_normal_jpy: 42_153,
    remaining_opportunity_jpy: 20_000,
    opportunity_matched_notional_jpy: 50_000,
    monthly_remaining_jpy: 130_000,
    unattributed_monthly_total_count: 17,
    unattributed_monthly_total_notional_jpy: 85_000,
    monthly_attribution_incomplete: true,
  },
  summary: { items_total: 1, active_items: 1, covered_items: 0, board_count: 0, plan_filtered_count: 0 },
  items: [{ label: '通常枠', preferred_tickers: ['7203.T'], consumed_by_count: 0, source_reasons: [], status: 'active', remaining_jpy: 42_153 }],
  today_decision: { code: 'wait_candidate', label: '候補待ち', reason: 'より良い候補を待ちます。' },
  filtered_summary: {},
  filtered_examples: [],
  warnings: [],
  no_action_rationale: [],
}

describe('PlanRail', () => {
  it('keeps the budget-consumption percentage separate from matched notional', () => {
    render(<PlanRail plan={plan} almanac={almanac} />)

    expect(screen.getByText('通常の共通プール 参考消化 19.7%')).toBeInTheDocument()
    expect(screen.getByText(/対応した実額/)).toHaveTextContent('¥39万')
    expect(screen.getByText('帰属確認中')).toBeInTheDocument()
  })

  it('switches between plan, schedule, and record without refetching', () => {
    render(<PlanRail plan={plan} almanac={almanac} />)

    fireEvent.click(screen.getByRole('tab', { name: '予定' }))
    expect(screen.getByText('決算予定')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: '記録' }))
    expect(screen.getByText('買付記録')).toBeInTheDocument()
  })

  it('keeps schedule and record available when plan is missing', () => {
    render(<PlanRail almanac={almanac} />)

    expect(screen.getByText('実行計画データはまだありません。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: '予定' }))
    expect(screen.getByText('積立日')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: '記録' }))
    expect(screen.getByText('執行台帳 →')).toBeInTheDocument()
  })
})

import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import CommandDeck from '../CommandDeck'
import type { TodayOps } from '../types'

const today = {
  board: [],
  focus: null,
  command: {
    stance: 'neutral',
    data_age_hours: 3,
    guard: { new_entry_allowed: true, trading_allowed: true, alerts: [] },
  },
  engine: { stance_reason: '条件が揃うまで観察します。' },
  delta: { added: [], removed: [], kept: [] },
  scenario_summary: { active: 1, partial: 2, watching: 3, alert_level: 'normal', evaluated_at: null },
  execution_plan: {
    status: 'active',
    horizon: {},
    budgets: {},
    consumption: { normal_plan_budget_consumed_pct: 19.7, remaining_normal_jpy: 42_153 },
    summary: { items_total: 0, active_items: 0, covered_items: 0, board_count: 0, plan_filtered_count: 0 },
    items: [],
    today_decision: { code: 'wait_candidate', label: '候補待ち', reason: 'より良い候補を待ちます。' },
    filtered_summary: {}, filtered_examples: [], warnings: [], no_action_rationale: [],
  },
} as unknown as TodayOps

describe('CommandDeck', () => {
  it('puts today decision, plan usage, guard, and order route in the first view', () => {
    render(<CommandDeck data={today} />)

    expect(screen.getByText('候補待ち')).toBeInTheDocument()
    expect(screen.getByText('19.7%')).toBeInTheDocument()
    expect(screen.getByText('GUARD OK')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '発注状況を見る →' })).toHaveAttribute('href', '#orders-section')
  })

  it('keeps the full execution plan reachable', () => {
    render(<CommandDeck data={today} />)
    fireEvent.click(screen.getByRole('button', { name: '計画詳細' }))
    const dialog = screen.getByRole('dialog')
    expect(dialog).toBeInTheDocument()
    expect(within(dialog).getByText('より良い候補を待ちます。')).toBeInTheDocument()
  })
})

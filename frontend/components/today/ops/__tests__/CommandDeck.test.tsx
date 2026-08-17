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

  it('surfaces allocator explanation and every wallet rather than hiding non-SBI cash', () => {
    const allocatorToday = {
      ...today,
      capital_allocator: { mode: 'enforce', selected_count: 1, selected_ticker: 'V', normal_action_cap_jpy: 250000 },
      capital_allocator_comparison: { run_id: 'allocator-run-1', explanation_status: 'explainable' },
      cash_status: [
        { key: 'usd', wallet_key: 'husband|rakuten|broker_cash|USD', owner: 'husband', broker: 'rakuten', currency: 'USD', balance_status: 'confirmed', reconciliation_required: false, available_for_new_buy: 54256, projected_balance: 54256 },
        { key: 'wife', wallet_key: 'wife|sbi|broker_cash|JPY', owner: 'wife', broker: 'sbi', currency: 'JPY', balance_status: 'confirmed', reconciliation_required: false, available_for_new_buy: 192886, projected_balance: 192886 },
      ],
      funding_alternatives: [{
        ticker: '1489.T',
        funding_workflows: [{
          kind: 'cross_owner_transfer_then_reprice_buy', can_fund: true,
          source_owner: 'husband', source_broker: 'sbi', source_wallet_key: 'husband|sbi|broker_cash|JPY', source_available_native: 195324,
          minimum_transfer_native: 3723,
          requirement: { kind: 'minimum_executable', target_quantity: 43 },
        }],
      }],
    } as unknown as TodayOps
    render(<CommandDeck data={allocatorToday} />)

    expect(screen.getByText('資本配分・現金監査')).toBeInTheDocument()
    expect(screen.getByText('husband/rakuten · USD')).toBeInTheDocument()
    expect(screen.getByText('wife/sbi · JPY')).toBeInTheDocument()
    expect(screen.getByText('差分説明可能')).toBeInTheDocument()
    expect(screen.getByText('1489.T · 最小実行数量 43')).toBeInTheDocument()
    expect(screen.getByText(/最低 ¥3,723/)).toBeInTheDocument()
    expect(screen.getByText('再評価後に買付')).toBeInTheDocument()
  })
})

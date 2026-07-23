import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AlmanacStrip from '../AlmanacStrip'
import type { AlmanacData, ExecutionPlan } from '../types'

const plan: ExecutionPlan = {
  status: 'active',
  age_hours: 3,
  horizon: { month: '2026-07', week_start: '2026-07-13', week_end: '2026-07-19' },
  budgets: { monthly_total_jpy: 300_000 },
  consumption: { normal_plan_budget_consumed_pct: 20, remaining_normal_jpy: 40_000 },
  summary: { items_total: 0, active_items: 0, covered_items: 0, board_count: 0, plan_filtered_count: 0 },
  items: [],
  today_decision: { code: 'wait_candidate', label: '候補待ち', reason: '条件待ちです。' },
  filtered_summary: {},
  filtered_examples: [],
  warnings: [],
  no_action_rationale: [],
}

const disabledPlan: ExecutionPlan = {
  ...plan,
  status: 'disabled',
  today_decision: { code: 'disabled', label: '計画レイヤー無効', reason: '最新の計画を生成できません。' },
}

const almanac: AlmanacData = {
  today: [],
  sessions: [
    { id: 'jpx-am', label: '東証 前場', market: 'JP', phase: 'regular', start: '09:00', end: '11:30', is_open_day: true },
    { id: 'jpx-pm', label: '東証 後場', market: 'JP', phase: 'regular', start: '12:30', end: '15:30', is_open_day: true },
    { id: 'us-pre', label: '米国 プレ', market: 'US', phase: 'pre', start: '17:00', end: '22:30', is_open_day: true },
    { id: 'us-regular', label: '米国 通常', market: 'US', phase: 'regular', start: '22:30', end: '05:00', is_open_day: true },
    { id: 'us-after', label: '米国 アフター', market: 'US', phase: 'after', start: '05:00', end: '09:00', is_open_day: true },
  ],
  upcoming: [
    { date: '2026-07-01', label: '古い予定', kind: 'earnings', ticker: 'OLD.T' },
    { date: '2026-07-08', label: '先週の予定', kind: 'earnings', ticker: 'LAST.T' },
  ],
  past: [{ date: '2026-07-08', kind: 'trade', ticker: '1489.T', side: 'buy', detail: '100株買付' }],
  pnl_by_date: { '2026-07-08': 20_000 },
  notes: [],
  is_weekday: true,
  today_str: '2026-07-15',
}

describe('AlmanacStrip', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-15T12:00:00+09:00'))
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('links the monthly lane and every weekly plan/result to calendar rows', () => {
    render(<AlmanacStrip almanac={almanac} plan={plan} />)

    expect(screen.getByRole('button', { name: '7/13–7/19の計画詳細' })).toBeInTheDocument()
    const monthlyLane = screen.getByTestId('monthly-plan-lane')
    const monday = screen.getByText('月', { selector: 'div' })
    expect(monthlyLane).toHaveTextContent('MONTHLY PLAN · 2026.07')
    expect(monthlyLane.compareDocumentPosition(monday) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByLabelText('各週の計画と結果')).toBeInTheDocument()
    expect(screen.getAllByLabelText(/の週次計画と結果$/)).toHaveLength(7)
    expect(screen.queryByLabelText('週内の日次損益')).not.toBeInTheDocument()
    expect(screen.getByText('●OLD.T')).toBeInTheDocument()
    expect(screen.getByText('●LAST.T')).toBeInTheDocument()
    expect(screen.getByText('表示範囲 先々週〜4週先')).toBeInTheDocument()
    expect(screen.getByText('1489.T')).toBeInTheDocument()
    expect(screen.getAllByText('+¥2万').length).toBeGreaterThan(0)
  })

  it('shows the active cross-midnight US session instead of only Tokyo hours', () => {
    vi.setSystemTime(new Date('2026-07-15T23:15:00+09:00'))
    render(<AlmanacStrip almanac={almanac} plan={plan} />)

    expect(screen.getByText('米国 通常 取引中')).toBeInTheDocument()
    expect(screen.getByText('東証 前場')).toBeInTheDocument()
    expect(screen.getAllByText('米国 通常').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('本日の市場タイムライン')).toHaveTextContent('米国 プレ')
    expect(screen.getByLabelText('本日の市場タイムライン')).toHaveTextContent('米国 アフター')
  })

  it('does not present a disabled plan as active or actionable', () => {
    render(<AlmanacStrip almanac={almanac} plan={disabledPlan} />)

    const monthlyLane = screen.getByTestId('monthly-plan-lane')
    expect(monthlyLane).toHaveTextContent('MONTHLY PLAN · 2026.07 · DISABLED')
    expect(monthlyLane).toHaveTextContent('計画レイヤー無効')
    expect(screen.queryByRole('button', { name: '7/13–7/19の計画詳細' })).not.toBeInTheDocument()
    expect(screen.getAllByText('計画レイヤー無効').length).toBeGreaterThan(1)
  })

  it('labels trade-only weeks as P&L pending', () => {
    render(<AlmanacStrip almanac={{ ...almanac, pnl_by_date: {} }} plan={plan} />)

    expect(screen.getByText('損益未集計')).toBeInTheDocument()
    expect(screen.queryByLabelText('週次損益 ¥0')).not.toBeInTheDocument()
  })
})

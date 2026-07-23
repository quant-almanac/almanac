import { render, screen } from '@testing-library/react'
import { SWRConfig } from 'swr'
import { describe, expect, it, vi } from 'vitest'

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), prefetch: vi.fn() }),
}))

import TraceStrip from '../TraceStrip'
import type { TodayOps } from '../types'

function renderTrace(today: TodayOps) {
  return render(<SWRConfig value={{ provider: () => new Map(), revalidateOnFocus: false }}><TraceStrip today={today} /></SWRConfig>)
}

const today = {
  board: [],
  command: {
    scenario: 'RISK_OFF',
    vix: 24.5,
    stance: 'defensive',
    guard: { new_entry_allowed: true, trading_allowed: true, alerts: [] },
  },
  scenario_summary: { active: 1, partial: 2, watching: 3, alert_level: 'elevated', evaluated_at: '2026-07-11T09:00:00+09:00' },
  engine: {
    funnel: [{ key: 'tiers', label: 'ティア候補', count: 7 }],
    red_team: [{ verdict: 'adopt' }, { verdict: 'reject' }],
  },
  execution_plan: {
    summary: { plan_filtered_count: 4 },
    today_decision: { reason: '候補の条件が揃うまで待機します。' },
  },
} as unknown as TodayOps

describe('TraceStrip', () => {
  it('renders counts from the Today payload and gives a zero-board conclusion reason', () => {
    renderTrace(today)

    expect(screen.getByRole('button', { name: /発動1 · 部分2 · 監視3/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /ティア7/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /採用1 \/ 棄却1/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /候補の条件が揃うまで待機します/ })).toBeInTheDocument()
  })

  it('uses dashes rather than failing while dashboard freshness is unavailable', () => {
    renderTrace(today)

    expect(screen.getByText('ok —')).toBeInTheDocument()
    expect(screen.getByText('停滞 —')).toBeInTheDocument()
  })
})

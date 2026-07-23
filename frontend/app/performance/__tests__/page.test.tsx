import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PerformancePage from '../page'
import { OPS } from '@/components/today/ops/tokens'
import type { ScoreRow } from '@/components/today/ops/types'

const useSWRMock = vi.hoisted(() => vi.fn())

vi.mock('swr', () => ({ default: useSWRMock }))

const baseObjective = {
  as_of: '2026-07-11',
  clean_since: '2026-05-25',
  clean_days: 47,
  required_days: 365,
  thresholds: { excess_pct_min: 2, max_dd_pct_limit: -15 },
  twr: { twr_pct: 1.2, benchmark_twr_pct: 0.8, excess_return_pct: null, excess_suppressed_reason: 'no_nav_data', confirmed: false },
  max_dd_12m: { dd_pct: -3.5, confirmed: false },
}

function swrData(data: unknown) {
  return { data, error: undefined, isLoading: false, isValidating: false, mutate: vi.fn() }
}

function setup(objective: Record<string, unknown>, rows: ScoreRow[] = [{ agent: 'analyst', role: 'synthesis', n: 12, win_rate: 0.6, excess_bps: 24, payoff: 1.2, measured: true }]) {
  useSWRMock.mockImplementation((key: string | null) => {
    if (key === '/api/objective-status') return swrData(objective)
    if (key?.startsWith('/api/twr')) return swrData({ twr_pct: 1, benchmark_twr_pct: 0.5, excess_return_pct: 0.5, period_days_actual: 30 })
    if (key === '/api/today') return swrData({ scorecard: { rows } })
    if (key === '/api/policy-decisions') return swrData({ accepted_count: 2, rejected_count: 1, modified_count: 0 })
    if (key === '/api/upgrade-comparison') return swrData({ comparison: {} })
    return swrData(undefined)
  })
}

describe('PerformancePage', () => {
  beforeEach(() => useSWRMock.mockReset())

  it('renders pending progress and maps a suppressed reason', () => {
    setup({ ...baseObjective, judgment: 'pending' })
    render(<PerformancePage />)

    expect(screen.getByText('判定待ち — クリーン期間 47/365日（起点 2026-05-25）')).toBeInTheDocument()
    expect(screen.getByText('NAV未記録')).toBeInTheDocument()
    expect(screen.getByText('クリーン期間 30日')).toBeInTheDocument()
  })

  it('renders the met state only after confirmation', () => {
    setup({ ...baseObjective, judgment: 'met', clean_days: 365, twr: { ...baseObjective.twr, confirmed: true, excess_return_pct: 2.2 }, max_dd_12m: { dd_pct: -12, confirmed: true } })
    render(<PerformancePage />)

    expect(screen.getByText('目標達成')).toBeInTheDocument()
    expect(screen.getByText('MET')).toBeInTheDocument()
  })

  it('renders the not-met state for a confirmed threshold failure', () => {
    setup({ ...baseObjective, judgment: 'not_met', clean_days: 365, twr: { ...baseObjective.twr, confirmed: true, excess_return_pct: 1.2 }, max_dd_12m: { dd_pct: -16, confirmed: true } })
    render(<PerformancePage />)

    expect(screen.getByText('目標未達')).toBeInTheDocument()
    expect(screen.getByText('NOT MET')).toBeInTheDocument()
  })

  it('renders every scorecard row and dims unmeasured rows', () => {
    setup({ ...baseObjective, judgment: 'pending' }, [
      { agent: 'analyst', role: 'synthesis', n: 12, win_rate: 0.6, excess_bps: 24, payoff: 1.2, measured: true },
      { agent: 'redteam', role: 'review', n: 0, measured: false },
    ])
    render(<PerformancePage />)

    expect(screen.getByText('analyst')).toBeInTheDocument()
    expect(screen.getByText('redteam').closest('tr')).toHaveStyle({ color: OPS.dim })
  })
})

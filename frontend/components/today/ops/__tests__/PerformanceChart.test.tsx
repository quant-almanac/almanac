import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import PerformanceChart from '../PerformanceChart'
import type { BenchmarkData } from '../types'

const benchmark: BenchmarkData = {
  dates: ['07-01', '07-08', '07-14'],
  portfolio: [0, 2, 4], sp500: [0, 5, 10], nikkei: [0, -1, -5],
  outperf: { sp500: -6, nikkei: 9 },
  method: 'modified_dietz', confirmed: true, clean_ok: true,
  clean_since: '2026-05-25', start_date: '2026-07-01', end_date: '2026-07-14',
  period_days_actual: 13, net_cash_flow: 100_000,
  basis: {
    portfolio: 'jpy_modified_dietz_twr',
    sp500: 'jpy_unhedged_price_return',
    nikkei: 'jpy_price_return',
  },
}

const pnl = [
  { d: '08-01', v: 10_000 },
  { d: '08-02', v: 30_000 },
  { d: '08-03', v: 20_000 },
  { d: '08-04', v: 50_000 },
]

describe('PerformanceChart', () => {
  it('puts 腕前 and 金額 in one frame instead of two lookalike charts', () => {
    render(<PerformanceChart benchmark={benchmark} pnl={pnl} />)
    expect(screen.getByText('腕前 %')).toBeInTheDocument()
    expect(screen.getByText('金額 円')).toBeInTheDocument()
  })

  it('explains what the active tab actually measures', () => {
    render(<PerformanceChart benchmark={benchmark} pnl={pnl} />)
    // 既定は腕前。TWRとP&Lの違いが読めることが要件
    expect(screen.getByText(/入出金の影響を除いた率/)).toBeInTheDocument()
  })

  it('shows benchmark comparison numbers, not just a line', () => {
    render(<PerformanceChart benchmark={benchmark} pnl={pnl} />)
    expect(screen.getByText('対S&P500')).toBeInTheDocument()
    expect(screen.getByText('-6.00pt')).toBeInTheDocument()
    expect(screen.getByText('確定')).toBeInTheDocument()
  })

  it('switches to the amount view with real statistics', async () => {
    render(<PerformanceChart benchmark={benchmark} pnl={pnl} />)
    fireEvent.click(screen.getByText('金額 円'))

    expect(await screen.findByText('累積損益')).toBeInTheDocument()
    expect(screen.getByText('勝率')).toBeInTheDocument()
    expect(screen.getByText('最大の落ち込み')).toBeInTheDocument()
    // 凡例と注記の両方に出るので複数ヒットする
    expect(screen.getAllByText(/差し引き済み/).length).toBeGreaterThanOrEqual(1)
  })

  it('falls back to the amount view when no benchmark exists', () => {
    render(<PerformanceChart benchmark={null} pnl={pnl} />)
    expect(screen.queryByText('腕前 %')).not.toBeInTheDocument()
    expect(screen.getByText('累積損益')).toBeInTheDocument()
  })

  it('renders nothing when there is no data at all', () => {
    const { container } = render(<PerformanceChart benchmark={null} pnl={[]} />)
    expect(container.querySelector('.perf')).toBeNull()
  })
})

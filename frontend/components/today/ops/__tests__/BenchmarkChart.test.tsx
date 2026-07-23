import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import BenchmarkChart from '../BenchmarkChart'

describe('BenchmarkChart', () => {
  it('labels the portfolio line as cash-flow-adjusted TWR', () => {
    render(
      <BenchmarkChart
        data={{
          dates: ['07-01', '07-08', '07-14'],
          portfolio: [0, 2, 4],
          sp500: [0, 5, 10],
          nikkei: [0, -1, -5],
          outperf: { sp500: -6, nikkei: 9 },
          method: 'modified_dietz',
          confirmed: true,
          clean_ok: true,
          clean_since: '2026-05-25',
          start_date: '2026-07-01',
          end_date: '2026-07-14',
          period_days_actual: 13,
          net_cash_flow: 100_000,
          basis: {
            portfolio: 'jpy_modified_dietz_twr',
            sp500: 'jpy_unhedged_price_return',
            nikkei: 'jpy_price_return',
          },
        }}
      />,
    )

    expect(screen.getByText('TWR VS BENCHMARK')).toBeInTheDocument()
    expect(screen.getByText(/Portfolio TWR/)).toBeInTheDocument()
    expect(screen.getByText('確定')).toBeInTheDocument()
    expect(screen.getByText(/Modified Dietz · 入出金調整済み/)).toHaveTextContent('純入出金 ¥10万')
    expect(screen.getByText(/vs S&P500円/)).toHaveTextContent('-6.00pt 負け')
    expect(screen.getByText(/S&P500は為替込みの円換算/)).toBeInTheDocument()
    expect(screen.getByLabelText('入出金調整済みTWRとベンチマークの比較チャート')).toBeInTheDocument()
  })
})

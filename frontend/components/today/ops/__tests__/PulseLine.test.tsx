import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import PulseLine from '../PulseLine'
import type { BenchmarkData } from '../types'

const benchmark: BenchmarkData = {
  dates: ['08-05', '08-06', '08-07'], portfolio: [0, 0.2, 0.3],
  nikkei: [-7, -5, -4.3], sp500: [-2.6, -1.2, -0.5],
  outperf: {}, method: 'modified_dietz', confirmed: true, clean_ok: true,
  basis: { portfolio: 'jpy_modified_dietz_twr', sp500: 'jpy_unhedged_price_return', nikkei: 'jpy_price_return' },
}

const calmGuard = { new_entry_allowed: true, trading_allowed: true, alerts: [], monthly_pnl_pct: 0 }

function cmd(overrides: Record<string, unknown> = {}) {
  return { scenario: 'BULL', stance: 'moderately_aggressive', vix: 14.9, guard: calmGuard, ...overrides }
}

/** レーンの BPM を順に読む（自分 / 市場）。 */
function laneBpms(): number[] {
  return [...document.querySelectorAll('.pulse-lane-bpm')].map(e => Number(e.textContent))
}

describe('PulseLine (市場の鼓動)', () => {
  it('shows exactly two pulses: 自分 と 市場（総合は置かない）', () => {
    render(<PulseLine command={cmd()} pulse={{ vix: 14.9 }} benchmark={benchmark} />)

    expect(screen.getByText('自分')).toBeInTheDocument()
    expect(screen.getByText('市場')).toBeInTheDocument()
    // 平均は「市場だけ荒れている」と「自分だけ痛んでいる」を潰すので出さない
    expect(screen.queryByText('総合')).not.toBeInTheDocument()
    expect(document.querySelectorAll('.pulse-lane')).toHaveLength(2)
  })

  it('names the combination instead of averaging it away', () => {
    // 市場だけ張り詰めている
    const { unmount } = render(<PulseLine command={cmd({ vix: 40 })} pulse={{ vix: 40 }} benchmark={benchmark} />)
    expect(screen.getByText('市場先行')).toBeInTheDocument()
    unmount()

    // 自分だけ痛んでいる
    render(<PulseLine
      command={cmd({ vix: 13, guard: { ...calmGuard, monthly_pnl_pct: -0.09, trading_allowed: false } })}
      pulse={{ vix: 13 }} benchmark={benchmark} />)
    expect(screen.getByText('自分先行')).toBeInTheDocument()
  })

  it('calls out the dangerous case where both are tense', () => {
    render(<PulseLine
      command={cmd({ vix: 34, guard: { ...calmGuard, monthly_pnl_pct: -0.07, new_entry_allowed: false } })}
      pulse={{ vix: 34 }} benchmark={benchmark} />)
    expect(screen.getByText('同時警戒')).toBeInTheDocument()
  })

  it('drives each lane independently', () => {
    render(<PulseLine command={cmd({ vix: 40 })} pulse={{ vix: 40 }} benchmark={benchmark} />)
    const [ownBpm, marketBpm] = laneBpms()
    expect(marketBpm).toBeGreaterThan(ownBpm)
  })

  it('beats faster when risk rises', () => {
    const { unmount } = render(<PulseLine command={cmd({ vix: 13 })} pulse={{ vix: 13 }} benchmark={benchmark} />)
    const calmMarket = laneBpms()[1]
    unmount()

    render(<PulseLine command={cmd({ vix: 38 })} pulse={{ vix: 38 }} benchmark={benchmark} />)
    expect(laneBpms()[1]).toBeGreaterThan(calmMarket)
  })

  it('flatlines both lanes instead of inventing a beat when nothing is known', () => {
    render(<PulseLine command={undefined} pulse={undefined} benchmark={null} />)

    expect(screen.getAllByText('データなし')).toHaveLength(2)
    expect(document.querySelectorAll('.pulse-lane-track.is-beating')).toHaveLength(0)
    expect(screen.getByText('判定不能')).toBeInTheDocument()
  })

  it('still reports the underlying indicators as numbers', () => {
    render(<PulseLine command={cmd()} pulse={{
      vix: 14.9, vix_change_1d: -1.65,
      oil_price: 79.5, oil_change_1d_pct: 0.14,
      us_10y: 4.66, us_10y_change_1d_pt: -0.01,
      dxy_level: 99.66, dxy_change_1d_pct: 0.06,
    }} benchmark={benchmark} />)

    expect(screen.getByText('79.5')).toBeInTheDocument()
    expect(screen.getByText('4.66')).toBeInTheDocument()
    // 利回りは pt、それ以外は %
    expect(screen.getByText(/米10年/).textContent).toContain('-0.01pt')
    expect(screen.getByText(/原油/).textContent).toContain('+0.14%')
  })
})

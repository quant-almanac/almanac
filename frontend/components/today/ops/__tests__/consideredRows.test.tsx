import { describe, expect, it } from 'vitest'
import { buildConsideredRows, buildRebuttals } from '../consideredRows'
import type { DecisionFlowUnselected, LaneVerdict, RedTeamAttack, RedTeamVerdict } from '../types'

function unsel(n: number): DecisionFlowUnselected[] {
  return Array.from({ length: n }, (_, i) => ({
    ticker: `U${i}`, type: 'buy', tier: 'Long', confidence_pct: 30 + i,
  }))
}

describe('buildConsideredRows: dropped candidates', () => {
  it('gives every unselected candidate its own row instead of bundling them', () => {
    const rows = buildConsideredRows(unsel(13), [], [])
    expect(rows.filter(r => r.kind === 'dropped')).toHaveLength(13)
  })

  it('draws no dropped rows when nothing was dropped', () => {
    expect(buildConsideredRows([], [], []).some(r => r.kind === 'dropped')).toBe(false)
  })

  it('survives missing lists (older API responses, no throw)', () => {
    expect(() => buildConsideredRows(undefined, null, undefined)).not.toThrow()
    expect(buildConsideredRows(null, null, null)).toEqual([])
  })

  it('adds the lane to a dropped row whose ticker collides with another dropped tier', () => {
    const dropped: DecisionFlowUnselected[] = [
      { ticker: 'XLF', type: 'trim', tier: 'Long' },
      { ticker: 'XLF', type: 'trim', tier: 'Medium' },
      { ticker: 'NEM', type: 'trim', tier: 'Long' },
    ]
    const rows = buildConsideredRows(dropped, [], [])
    const tickers = rows.map(r => r.ticker)
    expect(tickers).toContain('XLF Long')
    expect(tickers).toContain('XLF Medium')
    expect(tickers).not.toContain('XLF')
    expect(tickers).toContain('NEM')
  })

  it('carries confidence and notional into the detail', () => {
    const rows = buildConsideredRows(
      [{ ticker: 'NEM', type: 'trim', tier: 'Long', confidence_pct: 35, estimated_notional_jpy: 18208 }],
      [], [])
    expect(rows[0].detail).toContain('確信度 35%')
    expect(rows[0].detail).toContain('¥18,208')
  })
})

describe('buildRebuttals', () => {
  const attacks: RedTeamAttack[] = [
    { ticker: 'CRL', action: '空売り', expected_return_pct: 18, rationale: 'RSI81で過熱' },
  ]

  it('takes the verdict as the source of truth and borrows the rationale', () => {
    const v: RedTeamVerdict[] = [
      { ticker: 'CRL', action: '空売り', verdict: 'reject', verdict_reason: '踏み上げリスク' },
    ]
    const [r] = buildRebuttals(attacks, v)
    expect(r.adopted).toBe(false)
    expect(r.rationale).toBe('RSI81で過熱')
    expect(r.verdictReason).toBe('踏み上げリスク')
  })

  it('marks adopted verdicts as adopted', () => {
    const v: RedTeamVerdict[] = [{ ticker: 'CRL', action: '空売り', verdict: 'adopt', adopted_as: 'ヘッジ縮小' }]
    expect(buildRebuttals(attacks, v)[0]).toMatchObject({ adopted: true, adoptedAs: 'ヘッジ縮小' })
  })

  it('drops proposals that were never judged instead of calling them rejected', () => {
    expect(buildRebuttals(attacks, [])).toHaveLength(0)
  })

  it('distinguishes two verdicts on the same ticker by their action', () => {
    const out = buildRebuttals([], [
      { ticker: 'CRNX', action: '空売り（借株確認後）', verdict: 'reject' },
      { ticker: 'CRNX', action: 'short', verdict: 'reject' },
    ])
    expect(new Set(out.map(r => r.label)).size).toBe(2)
  })

  it('handles missing inputs without throwing', () => {
    expect(buildRebuttals(null, null)).toEqual([])
  })
})

describe('buildConsideredRows: rebuttals', () => {
  const rebuttals = buildRebuttals(
    [{ ticker: 'CRL', action: 'short', rationale: '過熱' }],
    [
      { ticker: 'CRL', action: 'short', verdict: 'reject', verdict_reason: '踏み上げ' },
      { ticker: 'HALO', action: 'short', verdict: 'adopt' },
    ],
  )

  it('gives every rebuttal its own row', () => {
    expect(buildConsideredRows([], rebuttals, []).filter(r => r.kind === 'rebuttal')).toHaveLength(2)
  })

  it('marks an adopted rebuttal pass, a rejected one reject', () => {
    const rows = buildConsideredRows([], rebuttals, [])
    expect(rows.find(r => r.ticker === 'HALO')!.verdict).toBe('pass')
    expect(rows.find(r => r.ticker === 'CRL')!.verdict).toBe('reject')
  })

  it('orders adopted rebuttals before rejected ones', () => {
    const rows = buildConsideredRows([], rebuttals, [])
    expect(rows.filter(r => r.kind === 'rebuttal').map(r => r.ticker)).toEqual(['HALO', 'CRL'])
  })

  it('carries both the proposal and the verdict into the detail text', () => {
    const detail = buildConsideredRows([], rebuttals, []).find(r => r.ticker === 'CRL')!.detail
    expect(detail).toContain('過熱')
    expect(detail).toContain('踏み上げ')
  })
})

describe('buildConsideredRows: information lanes', () => {
  const lanes: LaneVerdict[] = [
    { lane: 'catalyst', ticker: '1489.T', verdict: 'adopt', verdict_reason: '独立根拠で裏づけ', adopted_as: 'buy 100口' },
    { lane: 'catalyst', ticker: 'NVDA', verdict: 'reject', verdict_reason: '決算ブラックアウト中' },
    { lane: 'ipo_watch', ticker: 'SKHY', verdict: 'ignore', verdict_reason: 'ユニバース未登録で評価不能' },
  ]

  it('gives every lane verdict its own row', () => {
    expect(buildConsideredRows([], [], lanes).filter(r => r.kind === 'lane')).toHaveLength(3)
  })

  it('marks adopt pass and reject reject, matching the rebuttal color language', () => {
    const rows = buildConsideredRows([], [], lanes)
    expect(rows.find(r => r.ticker === '1489.T')!).toMatchObject({ verdict: 'pass', outcomeLabel: '採用' })
    expect(rows.find(r => r.ticker === 'NVDA')!).toMatchObject({ verdict: 'reject', outcomeLabel: '棄却' })
  })

  it('does not call "ignore" a rejection — it is a distinct claim', () => {
    const row = buildConsideredRows([], [], lanes).find(r => r.ticker === 'SKHY')!
    expect(row.verdict).not.toBe('reject')
    expect(row.outcomeLabel).toBe('監視のみ')
  })

  it('names the lane in the subtitle', () => {
    expect(buildConsideredRows([], [], lanes).find(r => r.ticker === '1489.T')!.subtitle).toContain('catalyst')
  })

  it('carries the adopted-as text into the detail', () => {
    expect(buildConsideredRows([], [], lanes).find(r => r.ticker === '1489.T')!.detail).toContain('buy 100口')
  })

  it('falls back safely on an unrecognised verdict string instead of throwing', () => {
    const rows = buildConsideredRows([], [], [{ lane: 'x', ticker: 'Z', verdict: 'unknown_future_value' }])
    expect(rows).toHaveLength(1)
    expect(rows[0].verdict).toBe('defer')
  })
})

describe('buildConsideredRows: overall order', () => {
  it('orders dropped candidates, then rebuttals, then lanes', () => {
    const rows = buildConsideredRows(
      unsel(2),
      buildRebuttals([], [{ ticker: 'R', verdict: 'reject' }]),
      [{ lane: 'catalyst', ticker: 'L', verdict: 'adopt' }],
    )
    expect(rows.map(r => r.kind)).toEqual(['dropped', 'dropped', 'rebuttal', 'lane'])
  })
})

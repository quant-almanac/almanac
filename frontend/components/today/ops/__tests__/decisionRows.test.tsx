import { describe, expect, it } from 'vitest'
import {
  buildDecisionRows, buildRebuttals, statusLabel, stopIndexFor,
} from '../decisionRows'
import type { DecisionFlowAction, DecisionFlowUnselected, RedTeamAttack, RedTeamVerdict } from '../types'

function action(over: Partial<DecisionFlowAction> = {}): DecisionFlowAction {
  return {
    key: 'k1', ticker: 'RDY-T', identity_quality: 'exact',
    decision_status: 'ready', execution_status: 'not_started',
    stage_states: {}, reason_codes: [], reasons: [],
    ...over,
  } as DecisionFlowAction
}

function unsel(n: number): DecisionFlowUnselected[] {
  return Array.from({ length: n }, (_, i) => ({
    ticker: `U${i}`, type: 'buy', tier: 'Long', confidence_pct: 30 + i,
  }))
}

describe('stopIndexFor', () => {
  it('places a ready candidate past every gate', () => {
    expect(stopIndexFor(action())).toEqual({ gate: 3, outcome: 'ready' })
  })
  it('stops at the first gate when policy rejected', () => {
    expect(stopIndexFor(action({ stage_states: { policy_rejected: 'x' } })))
      .toEqual({ gate: 0, outcome: 'filtered' })
  })
  it('stops at the third gate for review', () => {
    expect(stopIndexFor(action({ decision_status: 'review' })))
      .toEqual({ gate: 2, outcome: 'review' })
  })
  it('never reports a closed candidate as ready', () => {
    const stop = stopIndexFor(action({ decision_status: 'closed', execution_status: 'expired' }))
    expect(stop.outcome).not.toBe('ready')
    expect(stop.outcome).toBe('closed')
  })
})

describe('statusLabel', () => {
  it('prefers the execution-time status over the decision status', () => {
    // 発注可能(ready)でも指値が通っていれば「指値中」と言うべきで、
    // stopIndexFor の outcome だけではこの区別がつかない。
    expect(statusLabel(action({ decision_status: 'ready', execution_status: 'ordered' }))).toBe('指値中')
  })
  it('falls back to the decision status when nothing has been ordered', () => {
    expect(statusLabel(action({ decision_status: 'ready', execution_status: 'not_started' }))).toBe('発注可能')
  })
})

describe('buildDecisionRows: candidates', () => {
  it('gives every tracked candidate its own row carrying the selection key', () => {
    const { today } = buildDecisionRows(
      [action({ key: 'a', ticker: 'AAA' }), action({ key: 'b', ticker: 'BBB' })], [], [])
    expect(today.map(r => r.ticker)).toEqual(['AAA', 'BBB'])
    expect(today.map(r => r.actionKey)).toEqual(['a', 'b'])
    expect(today.every(r => r.kind === 'candidate')).toBe(true)
  })

  it('marks every candidate as full-depth (all three gates apply)', () => {
    const { today } = buildDecisionRows([action()], [], [])
    expect(today[0].stop.depth).toBe(3)
  })

  it('does not report a stopped candidate as having passed further gates than it did', () => {
    const { today } = buildDecisionRows(
      [action({ stage_states: { policy_rejected: 'x' } })], [], [])
    expect(today[0].stop.gate).toBe(0)
    expect(today[0].stop.kind).toBe('reject')
  })

  it('orders live orders before merely-ready candidates, and ready before review', () => {
    const { today } = buildDecisionRows([
      action({ key: 'r', ticker: 'REV', decision_status: 'review' }),
      action({ key: 'o', ticker: 'ORD', decision_status: 'ready', execution_status: 'ordered' }),
      action({ key: 'y', ticker: 'RDY', decision_status: 'ready' }),
    ], [], [])
    expect(today.map(r => r.ticker)).toEqual(['ORD', 'RDY', 'REV'])
  })

  it('surfaces the first reason message as the headline and keeps all of them in detail', () => {
    const { today } = buildDecisionRows([action({
      decision_status: 'review',
      reasons: [{ code: 'a', message: '理由1', provenance: 'today_overlay' },
        { code: 'b', message: '理由2', provenance: 'today_overlay' }],
    })], [], [])
    expect(today[0].headline).toBe('理由1')
    expect(today[0].detail).toBe('理由1\n理由2')
  })

  it('handles null actions without throwing', () => {
    expect(() => buildDecisionRows(null, null, null)).not.toThrow()
    expect(buildDecisionRows(null, null, null)).toEqual({ today: [], considered: [] })
  })
})

describe('buildDecisionRows: dropped candidates', () => {
  it('gives every unselected candidate its own row instead of bundling them', () => {
    // tier_generated に13件分の個票があるので、束ねる理由が無い。
    const { considered } = buildDecisionRows([action()], unsel(13), [])
    const dropped = considered.filter(r => r.kind === 'dropped')
    expect(dropped).toHaveLength(13)
    expect(new Set(dropped.map(r => r.id)).size).toBe(13)
  })

  it('marks dropped rows as depth 0 — they never entered any gate', () => {
    const { considered } = buildDecisionRows([], unsel(1), [])
    expect(considered[0].stop).toEqual({ depth: 0, gate: 0, kind: 'reject' })
  })

  it('draws no dropped rows when nothing was dropped', () => {
    const { considered } = buildDecisionRows([action()], [], [])
    expect(considered.some(r => r.kind === 'dropped')).toBe(false)
  })

  it('survives a missing unselected list (older API responses)', () => {
    expect(() => buildDecisionRows([action()], undefined, [])).not.toThrow()
    expect(buildDecisionRows([action()], null, []).considered).toEqual([])
  })

  it('adds the lane to a dropped row whose ticker also survived as a candidate', () => {
    // XLF は Long と Medium の両方から上がり、Medium が採られた。同名行が
    // 採用側と不採用側に並ぶと同じ話が二重に見えるので、レーン名で区別する。
    const picked = action({ key: 'x', ticker: 'XLF' })
    const dropped: DecisionFlowUnselected[] = [
      { ticker: 'XLF', type: 'trim', tier: 'Long' },
      { ticker: 'NEM', type: 'trim', tier: 'Long' },
    ]
    const { considered } = buildDecisionRows([picked], dropped, [])
    const tickers = considered.map(r => r.ticker)
    expect(tickers).toContain('XLF Long')
    expect(tickers).not.toContain('XLF')
    expect(tickers).toContain('NEM')
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

describe('buildDecisionRows: rebuttals', () => {
  const rebuttals = buildRebuttals(
    [{ ticker: 'CRL', action: 'short', rationale: '過熱' }],
    [
      { ticker: 'CRL', action: 'short', verdict: 'reject', verdict_reason: '踏み上げ' },
      { ticker: 'HALO', action: 'short', verdict: 'adopt' },
    ],
  )

  it('gives every rebuttal its own row', () => {
    const { considered } = buildDecisionRows([], [], rebuttals)
    const rows = considered.filter(r => r.kind === 'rebuttal')
    expect(rows).toHaveLength(2)
  })

  it('limits the gate ladder to depth 1 — a rebuttal is resolved at 対案検証, nothing further applies', () => {
    const { considered } = buildDecisionRows([], [], rebuttals)
    expect(considered.every(r => r.kind !== 'rebuttal' || r.stop.depth === 1)).toBe(true)
  })

  it('marks an adopted rebuttal as having passed the gate, a rejected one as stopped there', () => {
    const { considered } = buildDecisionRows([], [], rebuttals)
    const adopted = considered.find(r => r.ticker === 'HALO')!
    const rejected = considered.find(r => r.ticker === 'CRL')!
    expect(adopted.stop).toEqual({ depth: 1, gate: 1, kind: 'pass' })
    expect(rejected.stop).toEqual({ depth: 1, gate: 0, kind: 'reject' })
  })

  it('orders adopted rebuttals before rejected ones', () => {
    const { considered } = buildDecisionRows([], [], rebuttals)
    const rebuttalTickers = considered.filter(r => r.kind === 'rebuttal').map(r => r.ticker)
    expect(rebuttalTickers).toEqual(['HALO', 'CRL'])
  })

  it('carries both the proposal and the verdict into the detail text', () => {
    const { considered } = buildDecisionRows([], [], rebuttals)
    const detail = considered.find(r => r.ticker === 'CRL')!.detail
    expect(detail).toContain('過熱')
    expect(detail).toContain('踏み上げ')
  })

  it('changes nothing when Red Team produced no verdicts', () => {
    const { considered } = buildDecisionRows([action()], [], [])
    expect(considered.some(r => r.kind === 'rebuttal')).toBe(false)
  })
})

describe('buildDecisionRows: full shape from the live payload', () => {
  it('matches the funnel drop count exactly (regression: map once showed 8 while the funnel said 7)', () => {
    const actions = [action({ key: 'a', ticker: 'V', decision_status: 'ready', execution_status: 'ordered' })]
    const dropped = unsel(7)
    const { today, considered } = buildDecisionRows(actions, dropped, [])
    expect(today).toHaveLength(1)
    expect(considered.filter(r => r.kind === 'dropped')).toHaveLength(7)
  })
})

import { describe, expect, it } from 'vitest'
import {
  GATE_NODES, OUTCOME_NODES, buildDecisionGraph, buildRebuttals,
  pathNodeIdsFor, stopIndexFor,
} from '../decisionGraph'
import type {
  DecisionFlowAction, DecisionFlowUnselected, RedTeamAttack, RedTeamVerdict,
} from '../types'

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

const idsOf = (g: ReturnType<typeof buildDecisionGraph>) => g.nodes.map(n => n.id)
const linkBetween = (g: ReturnType<typeof buildDecisionGraph>, s: string, t: string) =>
  g.links.find(l => l.source === s && l.target === t)

describe('stopIndexFor', () => {
  it('places a ready candidate past every gate', () => {
    expect(stopIndexFor(action())).toEqual({ gate: 3, outcome: 'ready' })
  })
  it('stops at the policy gate when policy rejected', () => {
    expect(stopIndexFor(action({ stage_states: { policy_rejected: 'x' } })))
      .toEqual({ gate: 0, outcome: 'filtered' })
  })
  it('stops at the budget gate for review', () => {
    expect(stopIndexFor(action({ decision_status: 'review' })))
      .toEqual({ gate: 2, outcome: 'review' })
  })
  it('never reports a closed candidate as ready', () => {
    const stop = stopIndexFor(action({ decision_status: 'closed', execution_status: 'expired' }))
    expect(stop.outcome).not.toBe('ready')
    expect(stop.outcome).toBe('closed')
  })
})

describe('buildDecisionGraph', () => {
  it('connects sources into the synthesis node', () => {
    const g = buildDecisionGraph([action()])
    expect(linkBetween(g, 'src:market', 'stage:synthesis')).toBeTruthy()
    expect(linkBetween(g, 'src:portfolio', 'stage:synthesis')).toBeTruthy()
    expect(linkBetween(g, 'src:events', 'stage:synthesis')).toBeTruthy()
  })

  it('gives every tracked candidate its own node carrying the selection key', () => {
    const g = buildDecisionGraph([action({ key: 'a', ticker: 'AAA' }), action({ key: 'b', ticker: 'BBB' })])
    const cands = g.nodes.filter(n => n.kind === 'candidate')
    expect(cands.map(c => c.label)).toEqual(['AAA', 'BBB'])
    expect(cands.map(c => c.actionKey)).toEqual(['a', 'b'])
  })

  it('gives every unselected candidate its own node instead of bundling them', () => {
    // tier_generated に13件分の個票があるので、束ねる理由が無い。
    const g = buildDecisionGraph([action()], unsel(13))
    const dropped = g.nodes.filter(n => n.kind === 'dropped')
    expect(dropped).toHaveLength(13)
    expect(new Set(dropped.map(n => n.id)).size).toBe(13)
    for (const n of dropped) {
      expect(linkBetween(g, 'stage:synthesis', n.id)).toBeTruthy()
    }
  })

  it('draws no drop nodes when nothing was dropped', () => {
    const g = buildDecisionGraph([action()], [])
    expect(g.nodes.some(n => n.kind === 'dropped')).toBe(false)
  })

  it('survives a missing unselected list (older API responses)', () => {
    expect(() => buildDecisionGraph([action()], undefined)).not.toThrow()
    expect(buildDecisionGraph([action()], null).nodes.some(n => n.kind === 'dropped')).toBe(false)
  })

  it('wires a ready candidate through all gates to 発注可能', () => {
    const g = buildDecisionGraph([action({ key: 'a' })])
    expect(linkBetween(g, 'cand:a', GATE_NODES[0].id)).toBeTruthy()
    expect(linkBetween(g, GATE_NODES[0].id, GATE_NODES[1].id)).toBeTruthy()
    expect(linkBetween(g, GATE_NODES[1].id, GATE_NODES[2].id)).toBeTruthy()
    expect(linkBetween(g, GATE_NODES[2].id, 'out:ready')).toBeTruthy()
  })

  it('does not wire a stopped candidate past the gate it died at', () => {
    // policy で落ちた候補が安全ゲート以降につながっていたら経路が嘘になる
    const g = buildDecisionGraph([action({ key: 'a', stage_states: { policy_rejected: 'x' } })])
    expect(linkBetween(g, GATE_NODES[0].id, GATE_NODES[1].id)).toBeUndefined()
    expect(linkBetween(g, GATE_NODES[0].id, 'out:filtered')).toBeTruthy()
  })

  it('creates each outcome node only once even with several candidates sharing it', () => {
    const g = buildDecisionGraph([
      action({ key: 'a', decision_status: 'review' }),
      action({ key: 'b', decision_status: 'review' }),
    ])
    expect(g.nodes.filter(n => n.id === 'out:review')).toHaveLength(1)
    expect(g.nodes.find(n => n.id === 'out:review')?.label).toBe(OUTCOME_NODES.review)
  })

  it('still renders the frame when there are no candidates at all', () => {
    const g = buildDecisionGraph([], [])
    expect(idsOf(g)).toContain('stage:synthesis')
    expect(g.nodes.filter(n => n.kind === 'candidate')).toHaveLength(0)
  })

  it('handles null actions without throwing', () => {
    expect(() => buildDecisionGraph(null)).not.toThrow()
  })
})

describe('reading direction', () => {
  // force レイアウトに任せると「市場データが AI合成 の右」になり読む向きが逆転した。
  // 判断は向きが意味なので、背骨の段は必ず単調増加していること。
  it('orders the spine left to right: 入力 < 合成 < 候補 < ゲート < 結末', () => {
    const g = buildDecisionGraph([action({ key: 'a' })], unsel(5))
    const layer = (id: string) => g.nodes.find(n => n.id === id)?.layer

    expect(layer('src:market')).toBe(0)
    expect(layer('stage:synthesis')).toBe(1)
    expect(layer('cand:a')).toBe(2)
    expect(g.nodes.filter(n => n.kind === 'dropped').every(n => n.layer === 2)).toBe(true)
    expect(layer(GATE_NODES[0].id)).toBe(3)
    expect(layer(GATE_NODES[1].id)).toBe(4)
    expect(layer(GATE_NODES[2].id)).toBe(5)
    expect(layer('out:ready')).toBe(6)
  })

  it('assigns every node a layer so none escapes the placement', () => {
    const g = buildDecisionGraph([action({ key: 'a' }), action({ key: 'b' })], unsel(3))
    expect(g.nodes.filter(n => n.layer == null)).toEqual([])
  })

  it('separates candidates vertically so they do not stack', () => {
    const g = buildDecisionGraph([action({ key: 'a' }), action({ key: 'b' })])
    const ys = g.nodes.filter(n => n.kind === 'candidate').map(n => n.ny)
    expect(new Set(ys).size).toBe(ys.length)
  })
})

describe('placement', () => {
  // 力学レイアウトに任せたら13件の不採用が AI合成 の周りに放射状に散り、
  // 入力(市場データ等)と混ざって左→右が読めなくなった。位置は全部決め打つ。
  const many = buildDecisionGraph(
    [action({ key: 'a' })],
    unsel(13),
    buildRebuttals([], [{ ticker: 'Z', verdict: 'reject' }]),
  )
  const at = (id: string) => many.nodes.find(n => n.id === id)!

  it('gives every node an explicit position', () => {
    expect(many.nodes.filter(n => n.nx == null || n.ny == null)).toEqual([])
  })

  it('runs the spine strictly left to right', () => {
    const xs = [
      at('src:market').nx!, at('stage:synthesis').nx!, at('cand:a').nx!,
      at(GATE_NODES[0].id).nx!, at(GATE_NODES[1].id).nx!, at(GATE_NODES[2].id).nx!,
      at('out:ready').nx!,
    ]
    expect(xs).toEqual([...xs].sort((p, q) => p - q))
    expect(new Set(xs).size).toBe(xs.length)
  })

  it('keeps the spine on one horizontal band', () => {
    const band = [at('stage:synthesis'), at(GATE_NODES[0].id), at(GATE_NODES[2].id)]
    expect(new Set(band.map(n => n.ny)).size).toBe(1)
  })

  it('puts every dropped candidate below the spine, never mixed into it', () => {
    const spineY = at('stage:synthesis').ny!
    const dropped = many.nodes.filter(n => n.kind === 'dropped')
    expect(dropped).toHaveLength(13)
    expect(dropped.every(n => n.ny! > spineY + 0.15)).toBe(true)
  })

  it('wraps the dropped block into a grid instead of one unreadable column', () => {
    const dropped = many.nodes.filter(n => n.kind === 'dropped')
    expect(new Set(dropped.map(n => n.nx)).size).toBeGreaterThan(1)
    expect(new Set(dropped.map(n => n.ny)).size).toBeGreaterThan(1)
  })

  it('never stacks two nodes on the exact same point', () => {
    const seen = many.nodes.map(n => `${n.nx!.toFixed(4)},${n.ny!.toFixed(4)}`)
    expect(new Set(seen).size).toBe(seen.length)
  })

  it('keeps every node inside the drawable box', () => {
    expect(many.nodes.every(n => n.nx! >= 0 && n.nx! <= 1 && n.ny! >= 0 && n.ny! <= 1)).toBe(true)
  })

  it('keeps outcomes centred on the spine however many there are', () => {
    const one = buildDecisionGraph([action({ key: 'a' })])
    expect(one.nodes.find(n => n.kind === 'outcome')!.ny)
      .toBe(one.nodes.find(n => n.id === 'stage:synthesis')!.ny)
  })

  it('separates the opened rebuttals from the dropped block', () => {
    const g = buildDecisionGraph([action()], unsel(13), buildRebuttals([], [
      { ticker: 'A', verdict: 'reject' }, { ticker: 'B', verdict: 'reject' },
    ]), new Set(['rejected']))
    const dropX = Math.max(...g.nodes.filter(n => n.kind === 'dropped').map(n => n.nx!))
    const rebX = Math.min(...g.nodes.filter(n => n.kind === 'rebuttal').map(n => n.nx!))
    expect(rebX).toBeGreaterThan(dropX)
  })
})

describe('outcome identity', () => {
  // 結末をひとつの色で塗ると「要確認」が緑になり、成功したように見える。
  it('tags each outcome node with its kind so the UI can color it honestly', () => {
    const g = buildDecisionGraph([
      action({ key: 'a', decision_status: 'review' }),
      action({ key: 'b', decision_status: 'ready' }),
    ])
    expect(g.nodes.find(n => n.id === 'out:review')?.outcome).toBe('review')
    expect(g.nodes.find(n => n.id === 'out:ready')?.outcome).toBe('ready')
  })

  it('never leaves an outcome node without its kind', () => {
    const g = buildDecisionGraph([
      action({ key: 'a', stage_states: { policy_rejected: 'x' } }),
      action({ key: 'b', decision_status: 'deferred' }),
    ])
    const outcomes = g.nodes.filter(n => n.kind === 'outcome')
    expect(outcomes.length).toBeGreaterThan(0)
    expect(outcomes.every(n => !!n.outcome)).toBe(true)
  })
})

describe('buildRebuttals', () => {
  const attacks: RedTeamAttack[] = [
    { ticker: 'CRL', action: '空売り', expected_return_pct: 18, rationale: 'RSI81で過熱' },
    { ticker: 'HALO', action: '空売り', expected_return_pct: 20, rationale: 'RSI83で過熱' },
  ]

  it('takes the verdict as the source of truth and borrows the rationale', () => {
    const v: RedTeamVerdict[] = [
      { ticker: 'CRL', action: '空売り', verdict: 'reject', verdict_reason: '踏み上げリスク' },
    ]
    const [r] = buildRebuttals(attacks, v)
    expect(r.adopted).toBe(false)
    expect(r.label).toBe('CRL')
    expect(r.rationale).toBe('RSI81で過熱')       // attacks 側から
    expect(r.verdictReason).toBe('踏み上げリスク')  // 判定側から
    expect(r.expectedReturnPct).toBe(18)
  })

  it('marks adopted verdicts as adopted', () => {
    const v: RedTeamVerdict[] = [
      { ticker: 'HALO', action: '空売り', verdict: 'adopt', adopted_as: 'ヘッジ縮小' },
    ]
    expect(buildRebuttals(attacks, v)[0]).toMatchObject({ adopted: true, adoptedAs: 'ヘッジ縮小' })
  })

  it('drops proposals that were never judged instead of calling them rejected', () => {
    // 判定が無いものを棄却として描くと、通っていない主張を通っていないことにしてしまう
    expect(buildRebuttals(attacks, [])).toHaveLength(0)
  })

  it('falls back to the ticker when the action wording differs between the two lists', () => {
    const v: RedTeamVerdict[] = [{ ticker: 'CRL', action: 'short', verdict: 'reject' }]
    expect(buildRebuttals(attacks, v)[0].rationale).toBe('RSI81で過熱')
  })

  it('still labels the old hypothesis-only format', () => {
    const v: RedTeamVerdict[] = [{ verdict: 'reject', hypothesis: '金利上昇継続', reason: '根拠薄' }]
    expect(buildRebuttals([], v)[0]).toMatchObject({
      label: '金利上昇継続', verdictReason: '根拠薄', adopted: false,
    })
  })

  it('handles missing inputs without throwing', () => {
    expect(buildRebuttals(null, null)).toEqual([])
    expect(buildRebuttals(undefined, undefined)).toEqual([])
  })
})

describe('rebuttals on the map', () => {
  const rebuttals = buildRebuttals(
    [{ ticker: 'CRL', action: 'short', rationale: '過熱' }],
    [
      { ticker: 'CRL', action: 'short', verdict: 'reject', verdict_reason: '踏み上げ' },
      { ticker: 'HALO', action: 'short', verdict: 'adopt' },
    ],
  )

  it('collapses into one node per verdict group so the spine stays readable', () => {
    const g = buildDecisionGraph([action()], [], rebuttals)
    expect(g.nodes.filter(n => n.kind === 'rebuttal')).toHaveLength(0)
    const groups = g.nodes.filter(n => n.kind === 'rebuttal_group')
    expect(groups.map(n => n.label)).toEqual(['採用した対案', '棄却した対案'])
    expect(groups.every(n => n.expandable === 'closed')).toBe(true)
  })

  it('hangs the groups off the 対案検証 gate, not off the candidates', () => {
    const g = buildDecisionGraph([action()], [], rebuttals)
    expect(linkBetween(g, GATE_NODES[0].id, 'rebutgrp:rejected')).toBeTruthy()
  })

  it('opens only the group that was clicked', () => {
    const g = buildDecisionGraph([action()], [], rebuttals, new Set(['rejected']))
    const items = g.nodes.filter(n => n.kind === 'rebuttal')
    expect(items.map(n => n.label)).toEqual(['CRL'])
    expect(g.nodes.find(n => n.id === 'rebutgrp:rejected')?.expandable).toBe('open')
    expect(g.nodes.find(n => n.id === 'rebutgrp:adopted')?.expandable).toBe('closed')
  })

  it('carries both the proposal and the verdict into the hover text', () => {
    const g = buildDecisionGraph([action()], [], rebuttals, new Set(['rejected']))
    const detail = g.nodes.find(n => n.kind === 'rebuttal')?.detail ?? ''
    expect(detail).toContain('過熱')
    expect(detail).toContain('踏み上げ')
  })

  it('omits a verdict group that has no members', () => {
    const only = buildRebuttals([], [{ ticker: 'X', verdict: 'reject' }])
    const g = buildDecisionGraph([action()], [], only)
    expect(g.nodes.some(n => n.id === 'rebutgrp:adopted')).toBe(false)
    expect(g.nodes.some(n => n.id === 'rebutgrp:rejected')).toBe(true)
  })

  it('changes nothing about the map when Red Team produced no verdicts', () => {
    const withRebuttals = buildDecisionGraph([action()], [], [])
    expect(withRebuttals.nodes.some(n => n.kind.startsWith('rebuttal'))).toBe(false)
  })

  it('colors adopted and rejected differently', () => {
    const g = buildDecisionGraph([action()], [], rebuttals)
    expect(g.nodes.find(n => n.id === 'rebutgrp:adopted')?.outcome).toBe('ready')
    expect(g.nodes.find(n => n.id === 'rebutgrp:rejected')?.outcome).toBe('filtered')
  })
})

describe('pathNodeIdsFor', () => {
  it('lists exactly the nodes a candidate touched', () => {
    expect(pathNodeIdsFor(action({ key: 'a' }))).toEqual([
      'stage:synthesis', 'cand:a', GATE_NODES[0].id, GATE_NODES[1].id, GATE_NODES[2].id, 'out:ready',
    ])
  })

  it('stops the path at the gate where the candidate died', () => {
    const ids = pathNodeIdsFor(action({ key: 'a', stage_states: { policy_rejected: 'x' } }))
    expect(ids).toContain(GATE_NODES[0].id)
    expect(ids).not.toContain(GATE_NODES[1].id)
    expect(ids).toContain('out:filtered')
  })
})

describe('duplicate rebuttal labels', () => {
  // 実データで CRNX が「空売り（借株確認後）」と「short」の2件出た。
  // 同名ノードが2つ並ぶと描画バグに見えるので、手口で区別する。
  it('distinguishes two verdicts on the same ticker by their action', () => {
    const out = buildRebuttals([], [
      { ticker: 'CRNX', action: '空売り（借株確認後）', verdict: 'reject' },
      { ticker: 'CRNX', action: 'short', verdict: 'reject' },
    ])
    expect(new Set(out.map(r => r.label)).size).toBe(2)
    expect(out.every(r => r.label.startsWith('CRNX'))).toBe(true)
  })

  it('leaves a lone ticker label alone', () => {
    const out = buildRebuttals([], [{ ticker: 'CRL', action: '空売り', verdict: 'reject' }])
    expect(out[0].label).toBe('CRL')
  })
})

describe('duplicate tickers across lanes', () => {
  // 実データ: XLF は Long と Medium の両レーンから上がり、Medium が採られた。
  // 「XLF」ノードが採用側と不採用側に並ぶと、同じ話が二重に見える。
  const picked = action({ key: 'x', ticker: 'XLF' })
  const dropped: DecisionFlowUnselected[] = [
    { ticker: 'XLF', type: 'trim', tier: 'Long' },
    { ticker: 'NEM', type: 'trim', tier: 'Long' },
  ]

  it('adds the lane to a dropped node whose ticker also survived', () => {
    const g = buildDecisionGraph([picked], dropped)
    const labels = g.nodes.filter(n => n.kind === 'dropped').map(n => n.label)
    expect(labels).toContain('XLF Long')
    expect(labels).not.toContain('XLF')
  })

  it('leaves a ticker alone when it appears only once', () => {
    const g = buildDecisionGraph([picked], dropped)
    expect(g.nodes.filter(n => n.kind === 'dropped').map(n => n.label)).toContain('NEM')
  })

  it('disambiguates two dropped rows sharing a ticker', () => {
    const g = buildDecisionGraph([], [
      { ticker: 'AVGO', type: 'trim', tier: 'Long' },
      { ticker: 'AVGO', type: 'trim', tier: 'Medium' },
    ])
    const labels = g.nodes.filter(n => n.kind === 'dropped').map(n => n.label)
    expect(new Set(labels).size).toBe(2)
    expect(labels.sort()).toEqual(['AVGO Long', 'AVGO Medium'])
  })
})

describe('label width', () => {
  // 文字数で切ると全角が倍幅になり、狭い画面で隣のラベルに食い込んだ。
  it('clips a wide Japanese action but keeps a short ASCII one whole', () => {
    const out = buildRebuttals([], [
      { ticker: 'CRL', action: '空売り（借株確認後）', verdict: 'reject' },
      { ticker: 'CRL', action: 'short', verdict: 'reject' },
    ])
    expect(out.map(r => r.label)).toEqual(['CRL 空売り…', 'CRL short'])
  })

  it('keeps every node label within a drawable width', () => {
    const g = buildDecisionGraph([action()], unsel(13), buildRebuttals([], [
      { ticker: 'CRNX', action: '空売り（借株確認後）', verdict: 'reject' },
      { ticker: 'CRNX', action: 'short', verdict: 'reject' },
    ]), new Set(['rejected']))
    const units = (s: string) => [...s].reduce((a, c) => a + (/[\x00-\x7F]/.test(c) ? 1 : 2), 0)
    for (const n of g.nodes) expect(units(n.label)).toBeLessThanOrEqual(14)
  })
})

describe('grid shape', () => {
  // ECharts は layout:'none' でも外接矩形を描画領域にフィットさせるので、
  // こちらが測ったピクセル幅は当てにならない。実測620pxから列数を決めたら
  // 13件が2列7行に伸び、行間が潰れてラベルが次の行のノードに重なった。
  it('shapes the dropped block into a roughly square grid, not a long column', () => {
    const g = buildDecisionGraph([action()], unsel(13))
    const dropped = g.nodes.filter(n => n.kind === 'dropped')
    const cols = new Set(dropped.map(n => n.nx)).size
    const rows = new Set(dropped.map(n => n.ny)).size
    expect(cols).toBe(4)
    expect(rows).toBe(4)
  })

  it('does not depend on any measured width', () => {
    // 同じ件数なら常に同じ配置。幅は引数にも入っていない。
    const a = buildDecisionGraph([action()], unsel(13))
    const b = buildDecisionGraph([action()], unsel(13))
    expect(a.nodes.map(n => [n.nx, n.ny])).toEqual(b.nodes.map(n => [n.nx, n.ny]))
  })

  it('keeps the dropped block inside the box however many there are', () => {
    for (const n of [1, 5, 13, 20, 40]) {
      const g = buildDecisionGraph([action()], unsel(n), [], new Set())
      const ys = g.nodes.filter(d => d.kind === 'dropped').map(d => d.ny!)
      expect(Math.max(...ys)).toBeLessThanOrEqual(0.95)
      expect(g.nodes.every(d => d.nx! >= 0 && d.nx! <= 1)).toBe(true)
    }
  })

  it('shapes the opened rebuttals the same way', () => {
    const reb = buildRebuttals([], Array.from({ length: 9 }, (_, i) => ({
      ticker: `R${i}`, verdict: 'reject',
    })))
    const g = buildDecisionGraph([action()], [], reb, new Set(['rejected']))
    // 対案のラベルは銘柄+手口で長いので、列は増やさず行で伸ばす
    const items = g.nodes.filter(n => n.kind === 'rebuttal')
    expect(new Set(items.map(n => n.nx)).size).toBe(2)
    expect(new Set(items.map(n => n.ny)).size).toBe(5)
  })

  it('handles a single dropped candidate without dividing by zero', () => {
    const g = buildDecisionGraph([action()], unsel(1))
    const only = g.nodes.find(n => n.kind === 'dropped')!
    expect(Number.isFinite(only.nx!) && Number.isFinite(only.ny!)).toBe(true)
  })
})

describe('rebuttal group placement', () => {
  // 採用0件のとき配列の添字(gi=1)を使うと、棄却の束が不採用の1行目と
  // 同じ高さに落ちてラベルが重なった。描いた束だけ数える。
  it('places the only visible group at the first slot, not the second', () => {
    const onlyRejected = buildRebuttals([], [{ ticker: 'A', verdict: 'reject' }])
    const both = buildRebuttals([], [
      { ticker: 'A', verdict: 'adopt' }, { ticker: 'B', verdict: 'reject' },
    ])
    const solo = buildDecisionGraph([action()], [], onlyRejected)
    const pair = buildDecisionGraph([action()], [], both)
    expect(solo.nodes.find(n => n.id === 'rebutgrp:rejected')!.ny)
      .toBe(pair.nodes.find(n => n.id === 'rebutgrp:adopted')!.ny)
  })

  it('keeps the group clear of the dropped block', () => {
    const g = buildDecisionGraph([action()], unsel(13),
      buildRebuttals([], [{ ticker: 'A', verdict: 'reject' }]))
    const group = g.nodes.find(n => n.kind === 'rebuttal_group')!
    const firstDropRow = Math.min(...g.nodes.filter(n => n.kind === 'dropped').map(n => n.ny!))
    expect(group.ny!).toBeLessThan(firstDropRow - 0.05)
  })
})

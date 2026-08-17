import { describe, expect, it } from 'vitest'
import {
  DROP_LABEL, MAX_UNITS_FOR_CELLS, biggestDrop, buildFunnelModel, buildUnitLanes,
  canRenderAsCells, type StageRow,
} from '../decisionFunnel'

/** 2026-08-16 の実データ形状（15件入って2件だけ通り、最後は要確認）。 */
const LIVE_STAGES: StageRow[] = [
  { key: 'candidate_generation', entered: 15, passed: 15, review: 0, rejected: 0, deferred: 0 },
  { key: 'synthesis', entered: 15, passed: 2, review: 0, rejected: 0, deferred: 0 },
  { key: 'policy', entered: 2, passed: 2, review: 0, rejected: 0, deferred: 0 },
  { key: 'post_filter', entered: 2, passed: 2, review: 0, rejected: 0, deferred: 0 },
  { key: 'execution_readiness', entered: 2, passed: 0, review: 2, rejected: 0, deferred: 0 },
  { key: 'execution', entered: 0, passed: 2, review: 0, rejected: 0, deferred: 0 },
]

const stepFor = (model: ReturnType<typeof buildFunnelModel>, label: string) =>
  model.steps.find(s => s.label === label)

describe('buildFunnelModel', () => {
  it('surfaces the silent drop — 15件入って2件しか通らない差を必ず出す', () => {
    const synthesis = stepFor(buildFunnelModel(LIVE_STAGES), 'AI合成')
    const unselected = synthesis?.drops.find(d => d.kind === 'unselected')
    expect(unselected?.value).toBe(13)
    expect(unselected?.label).toBe(DROP_LABEL.unselected)
  })

  it('keeps every drop attached to the stage where it happened', () => {
    const model = buildFunnelModel(LIVE_STAGES)
    // 合成での脱落は合成の行だけに現れ、後段の行には出ない
    expect(stepFor(model, 'AI合成')?.lost).toBe(13)
    expect(stepFor(model, '安全ゲート')?.lost).toBe(0)
    expect(stepFor(model, '執行可否')?.lost).toBe(0)
  })

  it('records entered/passed per stage for the bar geometry', () => {
    const model = buildFunnelModel(LIVE_STAGES)
    expect(stepFor(model, '候補生成')).toMatchObject({ entered: 15, passed: 15 })
    expect(stepFor(model, 'AI合成')).toMatchObject({ entered: 15, passed: 2 })
    expect(stepFor(model, '発注判定')).toMatchObject({ entered: 2, passed: 0 })
  })

  it('separates review/rejected/deferred instead of lumping them together', () => {
    const model = buildFunnelModel([
      { key: 'candidate_generation', entered: 10, passed: 10 },
      { key: 'policy', entered: 10, passed: 4, rejected: 3, review: 2, deferred: 1 },
    ])
    const policy = stepFor(model, '安全ゲート')!
    expect(policy.drops.map(d => [d.kind, d.value])).toEqual([
      ['rejected', 3], ['review', 2], ['deferred', 1],
    ])
    // 全部説明できているので「AIが採用せず」は作らない
    expect(policy.drops.some(d => d.kind === 'unselected')).toBe(false)
    expect(policy.lost).toBe(6)
  })

  it('ignores the inconsistent execution row instead of drawing a phantom stage', () => {
    // 実データの execution 行は entered=0 なのに passed=2 で、そのまま描くと嘘になる
    expect(stepFor(buildFunnelModel(LIVE_STAGES), '執行')).toBeUndefined()
  })

  it('scales bars against the widest stage', () => {
    expect(buildFunnelModel(LIVE_STAGES).scale).toBe(15)
  })

  it('never emits negative drops from a broken row', () => {
    const model = buildFunnelModel([
      { key: 'candidate_generation', entered: 5, passed: 5 },
      { key: 'policy', entered: 5, passed: 9 },  // passed > entered
    ])
    expect(model.steps.every(s => s.drops.every(d => d.value > 0))).toBe(true)
    expect(stepFor(model, '安全ゲート')?.lost).toBe(0)
  })

  it('marks an all-passing stage as having lost nothing', () => {
    const model = buildFunnelModel([{ key: 'candidate_generation', entered: 4, passed: 4 }])
    expect(stepFor(model, '候補生成')?.lost).toBe(0)
  })

  it('returns an empty model for missing or unknown stages', () => {
    expect(buildFunnelModel(null)).toEqual({ steps: [], scale: 0 })
    expect(buildFunnelModel([])).toEqual({ steps: [], scale: 0 })
    expect(buildFunnelModel([{ key: 'nonsense', entered: 5, passed: 5 }])).toEqual({ steps: [], scale: 0 })
  })
})

describe('biggestDrop', () => {
  it('names the single largest loss so the headline can state it', () => {
    expect(biggestDrop(buildFunnelModel(LIVE_STAGES)))
      .toEqual({ stage: 'AI合成', reason: DROP_LABEL.unselected, value: 13 })
  })

  it('ignores survivors when picking the largest drop', () => {
    const model = buildFunnelModel([
      { key: 'candidate_generation', entered: 100, passed: 100 },
      { key: 'policy', entered: 100, passed: 98, rejected: 2 },
    ])
    // 通過(98)ではなく脱落(2)を返す
    expect(biggestDrop(model)?.value).toBe(2)
  })

  it('returns null when nothing dropped', () => {
    const model = buildFunnelModel([
      { key: 'candidate_generation', entered: 4, passed: 4 },
      { key: 'execution_readiness', entered: 4, passed: 4 },
    ])
    expect(biggestDrop(model)).toBeNull()
  })
})

describe('buildUnitLanes（1マス=候補1件）', () => {
  const model = buildFunnelModel(LIVE_STAGES)

  it('gives every candidate its own cell so 15→2 stays legible', () => {
    // 帯の太さを比例させると 2/15 = 13% で線になり後段の情報が消える。
    // マスなら生き残り2件がそのまま2マスとして見える。
    const lanes = buildUnitLanes(model)
    const synthesis = lanes.find(l => l.label === 'AI合成')!
    expect(synthesis.units).toHaveLength(15)
    expect(synthesis.units.filter(u => u === 'survive')).toHaveLength(2)
    expect(synthesis.units.filter(u => u === 'unselected')).toHaveLength(13)
  })

  it('puts survivors first so the same column descends through the stages', () => {
    const lanes = buildUnitLanes(model)
    for (const lane of lanes) {
      const firstDrop = lane.units.findIndex(u => u !== 'survive')
      if (firstDrop === -1) continue
      // 生き残りより後ろに脱落が来る = 左詰めが崩れていない
      expect(lane.units.slice(0, firstDrop).every(u => u === 'survive')).toBe(true)
      expect(lane.units.slice(firstDrop).every(u => u !== 'survive')).toBe(true)
    }
  })

  it('carries the survivor count down to the next stage', () => {
    const lanes = buildUnitLanes(model)
    for (let i = 0; i < lanes.length - 1; i += 1) {
      expect(lanes[i + 1].entered).toBe(lanes[i].passed)
    }
  })

  it('shows nothing surviving the final gate when everything went to review', () => {
    const last = buildUnitLanes(model).at(-1)!
    expect(last.units.filter(u => u === 'survive')).toHaveLength(0)
    expect(last.units.filter(u => u === 'review')).toHaveLength(2)
  })

  it('keeps each drop kind as its own cells', () => {
    const lanes = buildUnitLanes(buildFunnelModel([
      { key: 'candidate_generation', entered: 10, passed: 10 },
      { key: 'policy', entered: 10, passed: 4, rejected: 3, review: 2, deferred: 1 },
    ]))
    const policy = lanes.find(l => l.label === '安全ゲート')!
    expect(policy.units.filter(u => u === 'rejected')).toHaveLength(3)
    expect(policy.units.filter(u => u === 'review')).toHaveLength(2)
    expect(policy.units.filter(u => u === 'deferred')).toHaveLength(1)
    expect(policy.units).toHaveLength(10)
  })

  it('returns no lanes for an empty model', () => {
    expect(buildUnitLanes({ steps: [], scale: 0 })).toEqual([])
  })
})

describe('canRenderAsCells', () => {
  it('uses cells for a normal day', () => {
    expect(canRenderAsCells(buildFunnelModel(LIVE_STAGES))).toBe(true)
  })

  it('falls back to bars when there are too many candidates to draw individually', () => {
    const many = buildFunnelModel([
      { key: 'candidate_generation', entered: MAX_UNITS_FOR_CELLS + 1, passed: 10 },
    ])
    expect(canRenderAsCells(many)).toBe(false)
  })

  it('does not try to draw cells for an empty model', () => {
    expect(canRenderAsCells({ steps: [], scale: 0 })).toBe(false)
  })
})

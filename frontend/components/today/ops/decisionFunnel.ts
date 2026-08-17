/**
 * DECISION FLOW の「何件がどこで落ちたか」をファネル行に変換する純粋関数。
 *
 * 当初 Sankey で描いたが読めなかった。Sankey は終端ノードを必ず最終列に置くため、
 * AI合成で落ちた13件の帯が安全ゲート・執行可否・発注判定を横切って右端まで伸び、
 * 「13件がゲートを通過してから落ちた」ように見えてしまう。実際には合成の時点で
 * 消えている。一本道＋各段での脱落という形に Sankey は向いていない。
 *
 * 代わりに段ごとの横棒にする。棒の長さ = その段に入った件数、脱落はその段の行に
 * そのまま書く。落ちた場所が図の上で動かないので誤読しようがない。
 */

export type StageRow = {
  key: string
  entered?: number | null
  passed?: number | null
  review?: number | null
  rejected?: number | null
  deferred?: number | null
  executed?: number | null
}

export type DropKind = 'rejected' | 'review' | 'deferred' | 'unselected'

export type FunnelDrop = { kind: DropKind; label: string; value: number }

export type FunnelStep = {
  key: string
  label: string
  /** この段に入った件数。棒の長さの基準。 */
  entered: number
  /** 次の段へ進んだ件数。 */
  passed: number
  /** この段で失われた内訳。 */
  drops: FunnelDrop[]
  /** この段で失われた合計。 */
  lost: number
}

export type FunnelModel = {
  steps: FunnelStep[]
  /** 最初の段に入った件数。棒の相対長の基準。 */
  scale: number
}

export const STAGE_LABEL: Record<string, string> = {
  candidate_generation: '候補生成',
  synthesis: 'AI合成',
  policy: '安全ゲート',
  post_filter: '執行可否',
  execution_readiness: '発注判定',
  execution: '執行',
}

export const DROP_LABEL: Record<DropKind, string> = {
  unselected: 'AIが採用せず',
  rejected: '除外',
  review: '要確認',
  deferred: '保留',
}

const num = (v: number | null | undefined): number =>
  typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0

/**
 * ステージ列をファネル行に変換する。
 *
 * entered を passed / rejected / review / deferred に振り分け、どれにも計上されて
 * いない残りは「AIが採用せず」として必ず可視化する。ここを黙って落とすと
 * 「15件入って2件出た、残り13件は闇」になり、図が嘘をつく。
 */
export function buildFunnelModel(stages: StageRow[] | null | undefined): FunnelModel {
  const rows = (stages ?? []).filter(s => s && STAGE_LABEL[s.key])
  // 最終段の execution 行は entered/passed が食い違うことがある(集計元が別)ため使わない。
  const chain = rows.filter(s => s.key !== 'execution')
  if (!chain.length) return { steps: [], scale: 0 }

  const steps: FunnelStep[] = chain.map(stage => {
    const entered = num(stage.entered)
    const passed = num(stage.passed)
    const drops: FunnelDrop[] = []

    const push = (kind: DropKind, value: number) => {
      if (value > 0) drops.push({ kind, label: DROP_LABEL[kind], value })
    }
    push('rejected', num(stage.rejected))
    push('review', num(stage.review))
    push('deferred', num(stage.deferred))
    // 説明のつかない目減り分
    push('unselected', entered - (passed + num(stage.rejected) + num(stage.review) + num(stage.deferred)))

    return {
      key: stage.key,
      label: STAGE_LABEL[stage.key],
      entered,
      passed,
      drops,
      lost: drops.reduce((sum, d) => sum + d.value, 0),
    }
  })

  return { steps, scale: Math.max(1, ...steps.map(s => s.entered)) }
}

// ---------------------------------------------------------------------------
// 単位マス表示: 候補1件を1マスとして描く。
//
// 帯の太さを件数に比例させると 15→2 で幅比 13% になり、AI合成より後ろが
// ただの細い線になって情報が消える。件数が数十件なら1件=1マスで描いたほうが
// 比率をごまかさずに済み、「15件のうち13件がここで消えた」が一目で分かる。
//
// 生き残りは常に左詰めなので、同じ列が下の段へ降りていく = 流れとして追える。
// 落ちたマスはその段で止まり、次の段には現れない。
// ---------------------------------------------------------------------------

/** これを超える件数はマスで描かず、比率バーに落とす。 */
export const MAX_UNITS_FOR_CELLS = 48

export type UnitState = 'survive' | DropKind

export type UnitLane = {
  key: string
  label: string
  entered: number
  passed: number
  drops: FunnelDrop[]
  lost: number
  /** 左から: 生き残り → 脱落。次の段でも生き残りが同じ位置に来る。 */
  units: UnitState[]
}

export function buildUnitLanes(model: FunnelModel): UnitLane[] {
  return model.steps.map(step => {
    const units: UnitState[] = Array(step.passed).fill('survive')
    for (const drop of step.drops) {
      for (let i = 0; i < drop.value; i += 1) units.push(drop.kind)
    }
    return {
      key: step.key,
      label: step.label,
      entered: step.entered,
      passed: step.passed,
      drops: step.drops,
      lost: step.lost,
      units,
    }
  })
}

/** マスで描けるか（多すぎるとマスが潰れて逆に読めない）。 */
export function canRenderAsCells(model: FunnelModel): boolean {
  return model.scale > 0 && model.scale <= MAX_UNITS_FOR_CELLS
}

/** 一番大きな脱落。「今日はどこで落ちたのか」を1行で言うために使う。 */
export function biggestDrop(model: FunnelModel): { stage: string; reason: string; value: number } | null {
  let worst: { stage: string; reason: string; value: number } | null = null
  for (const step of model.steps) {
    for (const drop of step.drops) {
      if (!worst || drop.value > worst.value) {
        worst = { stage: step.label, reason: drop.label, value: drop.value }
      }
    }
  }
  return worst
}

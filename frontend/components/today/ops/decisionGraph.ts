/**
 * DECISION FLOW をノード・リンク図（Obsidianのグラフビュー相当）へ変換する純粋関数。
 *
 * これまでの試行と、なぜここに来たか:
 *   - Sankey    … 終端が最終列に固定され、早い段の脱落が後段を横切って誤読された
 *   - 横棒       … 正しいが「流れ」が消えた
 *   - リボン     … 15→2 の比率で後半が線に潰れた
 *   - マス       … 読めるが、候補と判断の「つながり」は表現できない
 *
 * ノードグラフは「何と何がつながっているか」を主役にする形なので、
 * 候補・ゲート・結末の関係をそのまま置ける。件数比を太さで表さないため、
 * 15→2 のような極端な比でも後段が潰れない。
 */

import type {
  DecisionFlowAction, DecisionFlowUnselected, RedTeamAttack, RedTeamVerdict,
} from './types'

export type GraphNodeKind =
  | 'source' | 'stage' | 'candidate' | 'gate' | 'outcome' | 'dropped'
  | 'rebuttal' | 'rebuttal_group'

export type GraphNode = {
  id: string
  label: string
  kind: GraphNodeKind
  /** 候補ノードだけが持つ。選択連動のキー。 */
  actionKey?: string
  /** 束ねたノードの件数（不採用のまとめなど）。 */
  count?: number
  /** 左→右の段。読む順の意味づけ（描画位置は nx/ny）。 */
  layer?: number
  /** 描画位置。0〜1の正規化座標（左上原点）。実ピクセルは描画側で掛ける。 */
  nx?: number
  ny?: number
  /** 結末ノードの種別。色分けに使う（要確認を緑にしない）。 */
  outcome?: string
  /** 反論ノードの本文（提案理由・判定理由）。ホバーで出す。 */
  detail?: string
  /** クリックで開閉できるノードか。開いていれば true。 */
  expandable?: 'open' | 'closed'
}

export type GraphLink = {
  source: string
  target: string
  /** その候補の実際に通った経路か（通電）。false は通っていない参考線。 */
  live: boolean
  /** 束ねたリンクの件数。 */
  count?: number
}

export type DecisionGraph = { nodes: GraphNode[]; links: GraphLink[] }

const SOURCES = [
  { id: 'src:market', label: '市場データ' },
  { id: 'src:portfolio', label: 'ポートフォリオ' },
  { id: 'src:events', label: 'イベント' },
]

export const GATE_NODES = [
  { id: 'gate:red', label: '対案検証' },
  { id: 'gate:safety', label: '安全ゲート' },
  { id: 'gate:budget', label: '執行可否' },
]

const SYNTH = 'stage:synthesis'

/**
 * 配置。0〜1の正規化座標で、描画側が実ピクセルに直す。
 *
 * force レイアウトは使わない。13件の不採用を力学に任せると AI合成 の周りに
 * 放射状に散り、入力(市場データ等)と混ざって左→右の流れが消えた。
 * 判断は向きが意味そのものなので、位置は全部こちらで決める。
 *
 *   上段  … 背骨（入力 → 合成 → 候補 → ゲート → 結末）を1本の横線に置く
 *   左下  … AI合成が採用しなかった候補を格子で並べる
 *   右下  … Red Team の対案（束ノードと、開いたときの個別ノード）
 */
const SPINE_Y = 0.17
const COL = { source: 0.02, synth: 0.20, candidate: 0.33, gate: [0.47, 0.61, 0.75], outcome: 0.93 }
type Grid = { x0: number; dx: number; y0: number; dy: number; cols: number }

/**
 * 格子の形は件数だけで決める。実寸は使わない。
 *
 * ECharts は layout:'none' でもノードの外接矩形を描画領域にフィットさせて
 * 拡大縮小するので、こちらが測ったピクセル幅は実際の描画幅と一致しない。
 * 実測幅から列数を決めたところ、分割カラム内の実測620pxで2列になり、
 * 13件が7行に伸びて行間が潰れた。
 *
 * 縦横が同じくらいの塊になるよう列数を取り、行が増えたぶん縦を詰める。
 */
function gridsFor(dropCount: number, rebutCount: number): { drop: Grid; rebut: Grid } {
  const shape = (n: number, maxCols: number, band: number, maxDy: number, x0: number, span: number) => {
    const cols = Math.max(1, Math.min(maxCols, Math.ceil(Math.sqrt(Math.max(1, n)))))
    const rows = Math.ceil(Math.max(1, n) / cols)
    return { x0, dx: span / cols, y0: 0, dy: Math.min(maxDy, band / Math.max(1, rows - 1)), cols }
  }
  const drop = shape(dropCount, 5, 0.50, 0.135, 0.045, 0.60)
  // 対案のラベルは銘柄+手口で長い。3列だと狭い画面で隣に食い込むので2列まで。
  const rebut = shape(rebutCount, 2, 0.42, 0.115, 0.63, 0.36)
  return { drop: { ...drop, y0: 0.44 }, rebut: { ...rebut, y0: 0.50 } }
}

/** 背骨の縦並び。件数が変わっても中心が SPINE_Y から動かないように配る。 */
function spineY(index: number, total: number, gap = 0.10): number {
  return SPINE_Y + (index - (total - 1) / 2) * gap
}

/** 格子の座標。件数が多い塊は縦1列にすると読めないので折り返す。 */
function gridAt(i: number, g: Grid): { nx: number; ny: number } {
  return { nx: g.x0 + (i % g.cols) * g.dx, ny: g.y0 + Math.floor(i / g.cols) * g.dy }
}

export const OUTCOME_NODES: Record<string, string> = {
  ready: '発注可能',
  review: '要確認',
  filtered: '除外',
  deferred: '保留',
  closed: '終了',
}

/** Red Team の対案 1件。attacks(提案) と red_team(判定) を突き合わせた結果。 */
export type Rebuttal = {
  id: string
  label: string
  /** 手口（"空売り" / "short" など）。同名ノードの区別に使う。 */
  action?: string
  adopted: boolean
  /** 提案理由。attacks 側にしか無いので、突き合わせできた時だけ入る。 */
  rationale?: string
  /** 採否の理由。判定側の言い分。 */
  verdictReason?: string
  expectedReturnPct?: number
  adoptedAs?: string
}

const ADOPTED_VERDICTS = new Set([
  'adopt', 'adopted', 'accept', 'accepted', 'partial', 'partial_adopt', 'partially_adopted',
])

/**
 * 対案を組み立てる。
 *
 * 正本は red_team（判定）。attacks は提案文しか持たず、採否を知らない。
 * 判定の無い提案は「採用か棄却か言えない」ので出さない
 * ——不明を棄却として描くと、通っていない主張を通っていないことにしてしまう。
 */
export function buildRebuttals(
  attacks: RedTeamAttack[] | null | undefined,
  verdicts: RedTeamVerdict[] | null | undefined,
): Rebuttal[] {
  const key = (t?: string, a?: string) => `${(t ?? '').trim()}|${(a ?? '').trim()}`
  const byPair = new Map<string, RedTeamAttack>()
  const byTicker = new Map<string, RedTeamAttack>()
  for (const at of attacks ?? []) {
    byPair.set(key(at.ticker, at.action), at)
    if (at.ticker && !byTicker.has(at.ticker)) byTicker.set(at.ticker, at)
  }

  const list = (verdicts ?? []).map((v, i) => {
    // 銘柄+アクションで拾えなければ銘柄だけで拾う（同一銘柄で表記揺れがある）
    const src = byPair.get(key(v.ticker, v.action))
      ?? (v.ticker ? byTicker.get(v.ticker) : undefined)
    const verdict = (v.verdict ?? '').toLowerCase().trim()
    return {
      id: `rebut:${i}`,
      // 新形式は ticker、旧形式(tier analysis)は hypothesis しか無い
      label: v.ticker || v.hypothesis || `対案 ${i + 1}`,
      action: v.action,
      adopted: ADOPTED_VERDICTS.has(verdict),
      rationale: src?.rationale,
      verdictReason: v.verdict_reason || v.reason,
      expectedReturnPct: src?.expected_return_pct,
      adoptedAs: v.adopted_as,
    }
  })

  // 同じ銘柄に別々の判定が出ることがある（「空売り」と「short」など）。
  // 同名ノードが2つ並ぶと描画バグに見えるので、重なった時だけ手口を足して区別する。
  const seen = new Map<string, number>()
  for (const r of list) seen.set(r.label, (seen.get(r.label) ?? 0) + 1)
  for (const r of list) {
    if ((seen.get(r.label) ?? 0) < 2 || !r.action) continue
    r.label = `${r.label} ${clipToWidth(r.action, 6)}`
  }
  return list
}

/**
 * 表示幅で切る。文字数で切ると「空売り（借株確…」のように全角で倍幅になり、
 * 狭い画面で隣のラベルへ食い込む（実測で700px幅では衝突した）。
 * 全角を2、半角を1として数える。
 */
function clipToWidth(text: string, maxUnits: number): string {
  let used = 0
  let out = ''
  for (const ch of text) {
    const w = /[\x00-\x7F]/.test(ch) ? 1 : 2
    if (used + w > maxUnits) return `${out}…`
    used += w
    out += ch
  }
  return out
}

/** 候補がどのゲートまで進んだか。DecisionFlow の laneStop と同じ判定規則。 */
export function stopIndexFor(a: DecisionFlowAction): { gate: number; outcome: string } {
  const s = a.stage_states ?? {}
  if (s.policy_rejected) return { gate: 0, outcome: 'filtered' }
  if (s.post_filter_rejected) return { gate: 1, outcome: 'filtered' }
  if (s.post_filter_deferred) return { gate: 1, outcome: 'deferred' }
  if (a.decision_status === 'review') return { gate: 2, outcome: 'review' }
  if (a.decision_status === 'filtered') return { gate: 2, outcome: 'filtered' }
  if (a.decision_status === 'deferred') return { gate: 2, outcome: 'deferred' }
  if (a.decision_status === 'ready') return { gate: 3, outcome: 'ready' }
  return { gate: 3, outcome: 'closed' }
}

/**
 * グラフを組む。
 *
 * 不採用候補は action_stage_log の tier_generated に1件ずつ残っているので、
 * 束ねずにそのまま1ノードずつ描く。対案(Red Team)だけは件数が多く
 * 背骨を埋めるので、既定は畳んでおきクリックで開く。
 */
export function buildDecisionGraph(
  actions: DecisionFlowAction[] | null | undefined,
  /** AIが採用しなかった候補。束ねずに1件1ノードで描く。 */
  unselected: DecisionFlowUnselected[] | null | undefined = [],
  rebuttals: Rebuttal[] = [],
  /** 展開済みの対案グループ（'adopted' / 'rejected'）。既定は畳んだまま。 */
  openGroups: ReadonlySet<string> = new Set(),
): DecisionGraph {
  const list = actions ?? []
  const unselectedList = unselected ?? []
  const openCount = rebuttals.filter(
    r => openGroups.has(r.adopted ? 'adopted' : 'rejected')).length
  const grids = gridsFor(unselectedList.length, openCount)
  const nodes: GraphNode[] = []
  const links: GraphLink[] = []

  SOURCES.forEach((s, i) => {
    nodes.push({
      id: s.id, label: s.label, kind: 'source',
      layer: 0, nx: COL.source, ny: spineY(i, SOURCES.length),
    })
    links.push({ source: s.id, target: SYNTH, live: true })
  })
  nodes.push({ id: SYNTH, label: 'AI合成', kind: 'stage', layer: 1, nx: COL.synth, ny: SPINE_Y })

  // 同じ銘柄が複数レーンから上がる（XLF は Long と Medium の両方）。
  // Medium が採られて Long が落ちると同名ノードが採用側と不採用側に並ぶので、
  // ぶつかった時だけレーン名を足して、どちらの話か分かるようにする。
  const tickerSeen = new Map<string, number>()
  for (const t of [
    ...list.map(a => a.ticker), ...unselectedList.map(u => u.ticker),
  ]) {
    const k = String(t ?? '')
    if (k) tickerSeen.set(k, (tickerSeen.get(k) ?? 0) + 1)
  }

  // 不採用も1件ずつノードにする。合成の直後で落ちるので候補と同じ列に、
  // レーンごとにまとめて（Long / Medium / MarginLong / ShortSell の順に）置く。
  sortUnselected(unselectedList).forEach((u, i) => {
    const id = `drop:${u.ticker ?? ''}|${u.type ?? ''}|${u.tier ?? ''}|${i}`
    const ticker = u.ticker || '候補'
    const collides = (tickerSeen.get(String(u.ticker ?? '')) ?? 0) > 1
    nodes.push({
      id,
      label: collides && u.tier ? `${ticker} ${u.tier}` : ticker,
      kind: 'dropped',
      detail: unselectedDetail(u),
      layer: 2,
      ...gridAt(i, grids.drop), // 背骨の下に格子で並べる
    })
    links.push({ source: SYNTH, target: id, live: true })
  })

  GATE_NODES.forEach((gate, i) => {
    nodes.push({
      id: gate.id, label: gate.label, kind: 'gate',
      layer: 3 + i, nx: COL.gate[i], ny: SPINE_Y,
    })
  })

  const usedOutcomes = new Set<string>()

  list.forEach((action, index) => {
    const id = `cand:${action.key || index}`
    const stop = stopIndexFor(action)
    nodes.push({
      id,
      label: action.ticker || '候補',
      kind: 'candidate',
      actionKey: action.key,
      layer: 2,
      nx: COL.candidate,
      ny: spineY(index, list.length),
    })
    links.push({ source: SYNTH, target: id, live: true })

    // 通ったゲートまでを通電線でつなぐ。止まった先はつながない。
    let prev = id
    for (let g = 0; g < GATE_NODES.length; g += 1) {
      if (g > stop.gate) break
      links.push({ source: prev, target: GATE_NODES[g].id, live: g <= stop.gate })
      prev = GATE_NODES[g].id
    }

    const outcomeId = `out:${stop.outcome}`
    if (!usedOutcomes.has(stop.outcome)) {
      usedOutcomes.add(stop.outcome)
      nodes.push({
        id: outcomeId,
        label: OUTCOME_NODES[stop.outcome] ?? stop.outcome,
        kind: 'outcome',
        outcome: stop.outcome,
        layer: 6,
        nx: COL.outcome,
        ny: SPINE_Y, // 件数が確定してから下で配り直す
      })
    }
    links.push({ source: prev, target: outcomeId, live: true })
  })

  const outcomeNodes = nodes.filter(n => n.kind === 'outcome')
  outcomeNodes.forEach((n, i) => { n.ny = spineY(i, outcomeNodes.length) })

  addRebuttals(nodes, links, rebuttals, openGroups, grids.rebut)
  return { nodes, links }
}

/**
 * 対案を 対案検証ゲート の枝として足す。
 *
 * 常時展開すると 9件の対案がゲート周りを埋めて背骨が読めなくなるので、
 * 既定は「棄却した対案 9」のような束ノード1つ。クリックで個別に開く。
 */
function addRebuttals(
  nodes: GraphNode[],
  links: GraphLink[],
  rebuttals: Rebuttal[],
  openGroups: ReadonlySet<string>,
  grid: Grid,
): void {
  if (!rebuttals.length) return
  const groups: { key: string; label: string; items: Rebuttal[] }[] = [
    { key: 'adopted', label: '採用した対案', items: rebuttals.filter(r => r.adopted) },
    { key: 'rejected', label: '棄却した対案', items: rebuttals.filter(r => !r.adopted) },
  ]

  // 束ノードは 対案検証ゲート の真下。開いた個別ノードは、束が何個開いていても
  // 重ならないよう右下のひとつの格子にまとめて配る。
  let placed = 0
  let shown = 0
  groups.forEach(g => {
    if (!g.items.length) return // 0件の束を描くと無いものがあるように見える
    const open = openGroups.has(g.key)
    const groupId = `rebutgrp:${g.key}`
    nodes.push({
      id: groupId,
      label: g.label,
      kind: 'rebuttal_group',
      count: g.items.length,
      outcome: g.key === 'adopted' ? 'ready' : 'filtered',
      expandable: open ? 'open' : 'closed',
      detail: open ? 'クリックで畳む' : 'クリックで1件ずつ開く',
      layer: 3.3,
      nx: 0.55,
      // 描いた束だけ数える。採用0件のときに配列の添字を使うと、
      // 棄却の束が不採用の1行目と同じ高さに落ちてラベルが重なった。
      ny: 0.30 + shown * 0.10,
    })
    shown += 1
    links.push({ source: GATE_NODES[0].id, target: groupId, live: true, count: g.items.length })

    if (!open) return
    for (const r of g.items) {
      nodes.push({
        id: r.id,
        label: r.label,
        kind: 'rebuttal',
        outcome: r.adopted ? 'ready' : 'filtered',
        detail: rebuttalDetail(r),
        layer: 3.62,
        ...gridAt(placed, grid),
      })
      links.push({ source: groupId, target: r.id, live: true })
      placed += 1
    }
  })
}

/** ホバーで出す本文。提案と判定は別の主張なので分けて出す。 */
export function rebuttalDetail(r: Rebuttal): string {
  const parts: string[] = []
  if (r.expectedReturnPct != null) parts.push(`想定リターン ${r.expectedReturnPct}%`)
  if (r.rationale) parts.push(`提案: ${r.rationale}`)
  if (r.verdictReason) parts.push(`${r.adopted ? '採用' : '棄却'}: ${r.verdictReason}`)
  if (r.adoptedAs) parts.push(`反映先: ${r.adoptedAs}`)
  return parts.join('\n')
}

const TIER_ORDER = ['Long', 'Medium', 'Swing', 'MarginLong', 'MarginShort', 'ShortSell']

const TYPE_LABEL: Record<string, string> = {
  buy: '買い', add: '買い増し', trim: '部分利確', sell: '売り', hold: '保持',
  hedge: 'ヘッジ', margin_buy: '信用買い', margin_sell: '信用売り', short: '空売り',
}

/** 同じレーンの候補が隣り合うように並べる。散らすとレーンの塊が読めない。 */
function sortUnselected(list: DecisionFlowUnselected[]): DecisionFlowUnselected[] {
  const rank = (t?: string) => {
    const i = TIER_ORDER.indexOf(String(t ?? ''))
    return i < 0 ? TIER_ORDER.length : i
  }
  return [...list].sort((a, b) =>
    rank(a.tier) - rank(b.tier)
    || (b.confidence_pct ?? 0) - (a.confidence_pct ?? 0)
    || String(a.ticker ?? '').localeCompare(String(b.ticker ?? '')))
}

/** ホバーで出す本文。なぜ弱かったのかを確信度と規模で示す。 */
export function unselectedDetail(u: DecisionFlowUnselected): string {
  const parts: string[] = []
  const head = [u.tier, TYPE_LABEL[String(u.type ?? '')] ?? u.type].filter(Boolean).join(' ・ ')
  if (head) parts.push(head)
  if (u.confidence_pct != null) parts.push(`確信度 ${u.confidence_pct}%`)
  if (u.estimated_notional_jpy != null) {
    parts.push(`想定 ¥${Math.round(u.estimated_notional_jpy).toLocaleString('ja-JP')}`)
  }
  parts.push('AI合成が採用せず')
  return parts.join('\n')
}

/** ある候補の経路上にあるノードID。hoverで経路だけ光らせるために使う。 */
export function pathNodeIdsFor(
  action: DecisionFlowAction,
  index = 0,
): string[] {
  const stop = stopIndexFor(action)
  const ids = [SYNTH, `cand:${action.key || index}`]
  for (let g = 0; g <= stop.gate && g < GATE_NODES.length; g += 1) {
    ids.push(GATE_NODES[g].id)
  }
  ids.push(`out:${stop.outcome}`)
  return ids
}

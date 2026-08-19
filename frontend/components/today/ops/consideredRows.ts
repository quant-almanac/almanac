/**
 * 「今日は動かなかったもの」を1件1行にする純粋関数。
 *
 * 前身の DECISION FLOW は候補・不採用候補・対案・情報レーンを、意味の薄い
 * 3列のゲート表(対案検証/安全ゲート/執行可否)に載せていた。実データで測ると
 * ゲート枠72個のうち意味を持つのは15個(21%)、24行中17行は3列とも空
 * だった (2026-08-19)。「フロー」と呼べる工程はそもそも無かった。
 *
 * さらに、今日動く候補(board/review_board)は 02 発注 が理由つきで既に
 * 表示している ——重複だった。ここに残す理由があるのは「動かなかったもの」
 * だけで、それは工程ではなく単なる一覧で足りる。
 */

import type { DecisionFlowUnselected, LaneVerdict, RedTeamAttack, RedTeamVerdict } from './types'

export type VerdictKind = 'pass' | 'reject' | 'defer'

export const OUTCOME_LABEL: Record<VerdictKind, string> = {
  pass: '採用', reject: '棄却', defer: '監視のみ',
}

export type RowKind = 'dropped' | 'rebuttal' | 'lane'

export type ConsideredRow = {
  id: string
  kind: RowKind
  ticker: string
  subtitle: string
  verdict: VerdictKind
  outcomeLabel: string
  headline: string
  detail: string
}

const TIER_ORDER = ['Long', 'Medium', 'Swing', 'MarginLong', 'MarginShort', 'ShortSell']
const TYPE_LABEL: Record<string, string> = {
  buy: '買い', add: '買い増し', trim: '部分利確', sell: '売り', hold: '保持',
  hedge: 'ヘッジ', margin_buy: '信用買い', margin_sell: '信用売り', short: '空売り',
}

function fmtJpy(v?: number): string {
  return v == null ? '' : `¥${Math.round(v).toLocaleString('ja-JP')}`
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

function unselectedDetail(u: DecisionFlowUnselected): string {
  const parts: string[] = []
  const head = [u.tier, TYPE_LABEL[String(u.type ?? '')] ?? u.type].filter(Boolean).join(' ・ ')
  if (head) parts.push(head)
  if (u.confidence_pct != null) parts.push(`確信度 ${u.confidence_pct}%`)
  if (u.estimated_notional_jpy != null) parts.push(`想定 ${fmtJpy(u.estimated_notional_jpy)}`)
  parts.push('AI合成が採用せず')
  return parts.join('\n')
}

function droppedRows(unselected: DecisionFlowUnselected[]): ConsideredRow[] {
  // 同じ銘柄が複数レーンから上がることがある(XLF は Long/Medium 両方)。
  // ぶつかった時だけレーン名を足して、どちらの話か分かるようにする。
  const seen = new Map<string, number>()
  for (const u of unselected) {
    const k = String(u.ticker ?? '')
    if (k) seen.set(k, (seen.get(k) ?? 0) + 1)
  }
  return sortUnselected(unselected).map((u, i) => {
    const ticker = u.ticker || '候補'
    const collides = (seen.get(String(u.ticker ?? '')) ?? 0) > 1
    const subtitle = [u.tier, TYPE_LABEL[String(u.type ?? '')] ?? u.type,
      u.confidence_pct != null ? `確信度${u.confidence_pct}%` : null]
      .filter(Boolean).join(' ・ ')
    return {
      id: `drop:${u.ticker ?? ''}|${u.type ?? ''}|${u.tier ?? ''}|${i}`,
      kind: 'dropped',
      ticker: collides && u.tier ? `${ticker} ${u.tier}` : ticker,
      subtitle,
      verdict: 'reject',
      outcomeLabel: '不採用',
      headline: 'AI合成が採用せず',
      detail: unselectedDetail(u),
    }
  })
}

/** Red Team の対案 1件。attacks(提案) と red_team(判定) を突き合わせた結果。 */
export type Rebuttal = {
  id: string
  label: string
  /** 手口（"空売り" / "short" など）。同名行の区別に使う。 */
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
    const src = byPair.get(key(v.ticker, v.action))
      ?? (v.ticker ? byTicker.get(v.ticker) : undefined)
    const verdict = (v.verdict ?? '').toLowerCase().trim()
    return {
      id: `rebut:${i}`,
      label: v.ticker || v.hypothesis || `対案 ${i + 1}`,
      action: v.action,
      adopted: ADOPTED_VERDICTS.has(verdict),
      rationale: src?.rationale,
      verdictReason: v.verdict_reason || v.reason,
      expectedReturnPct: src?.expected_return_pct,
      adoptedAs: v.adopted_as,
    }
  })

  const seen = new Map<string, number>()
  for (const r of list) seen.set(r.label, (seen.get(r.label) ?? 0) + 1)
  for (const r of list) {
    if ((seen.get(r.label) ?? 0) < 2 || !r.action) continue
    r.label = `${r.label} ${r.action}`
  }
  return list
}

function rebuttalDetail(r: Rebuttal): string {
  const parts: string[] = []
  if (r.expectedReturnPct != null) parts.push(`想定リターン ${r.expectedReturnPct}%`)
  if (r.rationale) parts.push(`提案: ${r.rationale}`)
  if (r.verdictReason) parts.push(`${r.adopted ? '採用' : '棄却'}: ${r.verdictReason}`)
  if (r.adoptedAs) parts.push(`反映先: ${r.adoptedAs}`)
  return parts.join('\n')
}

function rebuttalRows(rebuttals: Rebuttal[]): ConsideredRow[] {
  const ordered = [...rebuttals].sort((a, b) =>
    Number(b.adopted) - Number(a.adopted) || a.label.localeCompare(b.label))
  return ordered.map(r => ({
    id: r.id,
    kind: 'rebuttal',
    ticker: r.label,
    subtitle: r.action ? `対案 ・ ${r.action}` : '対案',
    verdict: r.adopted ? 'pass' : 'reject',
    outcomeLabel: r.adopted ? '採用' : '棄却',
    headline: r.verdictReason ?? (r.adopted ? '採用' : '棄却'),
    detail: rebuttalDetail(r),
  }))
}

const LANE_VERDICT_KIND: Record<string, VerdictKind> = {
  adopt: 'pass', partial: 'pass', adopt_partial: 'pass',
  reject: 'reject',
  // ignore は「評価したが積極的に却下したわけではない」(未登録銘柄・様子見)。
  // reject と同じ扱いにすると、能動的な却下と「まだ判断材料が無い」の
  // 区別が消える。
  ignore: 'defer',
}

function laneRows(lanes: LaneVerdict[]): ConsideredRow[] {
  return lanes.map((l, i) => {
    const kind = LANE_VERDICT_KIND[l.verdict] ?? 'defer'
    const parts = [l.verdict_reason, l.adopted_as ? `→ ${l.adopted_as}` : null].filter(Boolean)
    return {
      id: `lane:${l.lane}|${l.ticker ?? ''}|${i}`,
      kind: 'lane',
      ticker: l.ticker || '候補',
      subtitle: `情報レーン ・ ${l.lane}`,
      verdict: kind,
      outcomeLabel: OUTCOME_LABEL[kind],
      headline: l.verdict_reason ?? '',
      detail: parts.join('\n') || l.verdict_reason || '',
    }
  })
}

/**
 * 見送った提案の一覧を組む。
 *
 * 不採用候補は action_stage_log の tier_generated に1件ずつ残っているので、
 * 束ねずにそのまま1行ずつ出す。並び順は 不採用候補 → 対案(採用→棄却) →
 * 情報レーン。
 */
export function buildConsideredRows(
  unselected: DecisionFlowUnselected[] | null | undefined,
  rebuttals: Rebuttal[] | null | undefined,
  lanes: LaneVerdict[] | null | undefined,
): ConsideredRow[] {
  return [
    ...droppedRows(unselected ?? []),
    ...rebuttalRows(rebuttals ?? []),
    ...laneRows(lanes ?? []),
  ]
}

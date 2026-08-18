/**
 * DECISION FLOW を「提案1件 = 1行」の表へ変換する純粋関数。
 *
 * これまでの5案がすべて分かりにくかったのは、画面の大半を「工程」（毎日
 * 変わらない入力3・合成・ゲート3）に使い、一番知りたい「今日の候補・止まった
 * 理由・他に何を検討したか」が MAP / FUNNEL / TRACKS / 対案パネルの4箇所に
 * 分散していたため (2026-08-19)。
 *
 * 実データで確認すると、不採用候補も対案もすべて「評価された提案」で、
 * 違うのは止まった場所だけだった。工程は列見出しとして1回だけ描き、
 * 各提案は1行・どこまで進んだかを塗りで示す形に統合する。
 */

import type {
  DecisionFlowAction, DecisionFlowUnselected, RedTeamAttack, RedTeamVerdict,
} from './types'

/** 判断フローの3ゲート。列見出しとして1回だけ描く。 */
export const STAGE_LABELS = ['対案検証', '安全ゲート', '執行可否'] as const

export type StopKind = 'pass' | 'reject' | 'review' | 'defer'

export const OUTCOME_LABEL: Record<string, string> = {
  ready: '発注可能',
  review: '要確認',
  filtered: '除外',
  deferred: '保留',
  closed: '終了',
}

const OUTCOME_KIND: Record<string, StopKind> = {
  ready: 'pass', review: 'review', filtered: 'reject', deferred: 'defer', closed: 'defer',
}

/**
 * 提案がどのゲートまで進んだか。
 *
 * gate は「通過したゲート数」(0〜3)。3 なら対案検証・安全ゲート・執行可否を
 * すべて通過して結末に到達。0〜2 は、その番号のゲートで止まったことを表す
 * （0=対案検証で止まった、1=安全ゲートで止まった、2=執行可否で止まった）。
 */
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

export type StopState = {
  /** 対案検証・安全ゲート・執行可否のうち、この提案の種類にとって意味を持つ段数。
   *  candidate=3(全ゲート対象) / rebuttal=1(対案検証のみ、他は対象外) /
   *  dropped=0(どのゲートにも入らず合成の時点で終わっている)。 */
  depth: 0 | 1 | 3
  /** 通過したゲート数 (0..depth)。depth と同じなら意味のある範囲を全通過。 */
  gate: number
  kind: StopKind
}

export type RowKind = 'candidate' | 'dropped' | 'rebuttal'

export type DecisionRow = {
  id: string
  kind: RowKind
  ticker: string
  subtitle: string
  /** candidate 行だけが持つ。他パネルとの選択連動に使う。 */
  actionKey?: string
  stop: StopState
  outcomeLabel: string
  /** クリック前から見える一言。 */
  headline: string
  /** クリックで開く全文。 */
  detail: string
}

function actionTypeLabel(type?: string): string {
  return ({
    buy: '買い', add: '買い増し', trim: '部分利確', sell: '売り', hold: '保持',
    hedge: 'ヘッジ', margin_buy: '信用買い', margin_sell: '信用売り', short: '空売り',
  } as Record<string, string>)[type ?? ''] ?? type ?? ''
}

/** 執行段階まで含めた状態ラベル。stopIndexFor の outcome だけでは
 *  「発注可能」と「指値中」の区別がつかない。 */
export function statusLabel(a: DecisionFlowAction): string {
  if (a.execution_status === 'ordered') return '指値中'
  if (a.execution_status === 'filled') return '約定'
  if (a.execution_status === 'executed') return '実行済み'
  if (a.execution_status === 'cancelled') return '取消・終了'
  if (a.execution_status === 'expired') return '期限切れ'
  if (a.execution_status === 'reprice_required') return '再評価待ち'
  if (a.decision_status === 'ready') return '発注可能'
  if (a.decision_status === 'deferred') return '保留'
  if (a.decision_status === 'filtered') return '除外'
  return '要確認'
}

function fmtJpy(v?: number): string {
  return v == null ? '' : `¥${Math.round(v).toLocaleString('ja-JP')}`
}

function candidateRow(action: DecisionFlowAction, index: number): DecisionRow {
  const stop = stopIndexFor(action)
  const kind = OUTCOME_KIND[stop.outcome] ?? 'defer'
  const subtitle = [actionTypeLabel(action.type), action.account, fmtJpy(action.estimated_notional_jpy)]
    .filter(Boolean).join(' ・ ')
  const reasonMessages = (action.reasons ?? []).map(r => r.message).filter((m): m is string => !!m)
  const headline = reasonMessages[0] ?? action.reason_codes?.join(' · ') ?? statusLabel(action)
  return {
    id: `cand:${action.key || index}`,
    kind: 'candidate',
    ticker: action.ticker || '候補',
    subtitle,
    actionKey: action.key,
    stop: { depth: 3, gate: stop.gate, kind },
    outcomeLabel: statusLabel(action),
    headline,
    detail: reasonMessages.join('\n') || headline,
  }
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

function unselectedDetail(u: DecisionFlowUnselected): string {
  const parts: string[] = []
  const head = [u.tier, TYPE_LABEL[String(u.type ?? '')] ?? u.type].filter(Boolean).join(' ・ ')
  if (head) parts.push(head)
  if (u.confidence_pct != null) parts.push(`確信度 ${u.confidence_pct}%`)
  if (u.estimated_notional_jpy != null) parts.push(`想定 ${fmtJpy(u.estimated_notional_jpy)}`)
  parts.push('AI合成が採用せず')
  return parts.join('\n')
}

function droppedRows(
  unselected: DecisionFlowUnselected[], tickerSeen: Map<string, number>,
): DecisionRow[] {
  return sortUnselected(unselected).map((u, i) => {
    const ticker = u.ticker || '候補'
    // 同じ銘柄が採用側にも不採用側にも上がることがある(XLF は Long/Medium 両方から)。
    // ぶつかった時だけレーン名を足して、どちらの話か分かるようにする。
    const collides = (tickerSeen.get(String(u.ticker ?? '')) ?? 0) > 1
    const subtitle = [u.tier, TYPE_LABEL[String(u.type ?? '')] ?? u.type,
      u.confidence_pct != null ? `確信度${u.confidence_pct}%` : null]
      .filter(Boolean).join(' ・ ')
    return {
      id: `drop:${u.ticker ?? ''}|${u.type ?? ''}|${u.tier ?? ''}|${i}`,
      kind: 'dropped',
      ticker: collides && u.tier ? `${ticker} ${u.tier}` : ticker,
      subtitle,
      stop: { depth: 0, gate: 0, kind: 'reject' },
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
  // 同名行が2つ並ぶと表示バグに見えるので、重なった時だけ手口を足して区別する。
  const seen = new Map<string, number>()
  for (const r of list) seen.set(r.label, (seen.get(r.label) ?? 0) + 1)
  for (const r of list) {
    if ((seen.get(r.label) ?? 0) < 2 || !r.action) continue
    r.label = `${r.label} ${r.action}`
  }
  return list
}

/** 展開したときの全文。提案と判定は別の主張なので分けて出す。 */
export function rebuttalDetail(r: Rebuttal): string {
  const parts: string[] = []
  if (r.expectedReturnPct != null) parts.push(`想定リターン ${r.expectedReturnPct}%`)
  if (r.rationale) parts.push(`提案: ${r.rationale}`)
  if (r.verdictReason) parts.push(`${r.adopted ? '採用' : '棄却'}: ${r.verdictReason}`)
  if (r.adoptedAs) parts.push(`反映先: ${r.adoptedAs}`)
  return parts.join('\n')
}

function rebuttalRows(rebuttals: Rebuttal[]): DecisionRow[] {
  // 採用を先に、それぞれ内部は銘柄名で安定ソート。
  const ordered = [...rebuttals].sort((a, b) =>
    Number(b.adopted) - Number(a.adopted) || a.label.localeCompare(b.label))
  return ordered.map(r => ({
    id: r.id,
    kind: 'rebuttal',
    ticker: r.label,
    subtitle: r.action ? `対案 ・ ${r.action}` : '対案',
    // 対案は対案検証ゲートで採否が決まるだけで、以降の安全ゲート・執行可否は
    // その対案自体の話ではない(採用されれば別の判断に反映される)ので対象外。
    stop: { depth: 1, gate: r.adopted ? 1 : 0, kind: r.adopted ? 'pass' : 'reject' },
    outcomeLabel: r.adopted ? '採用' : '棄却',
    headline: r.verdictReason ?? (r.adopted ? '採用' : '棄却'),
    detail: rebuttalDetail(r),
  }))
}

const CANDIDATE_RANK: Record<string, number> = {
  ordered: 0, filled: 0, executed: 0,
}

/** 今日の候補を「実行に近い順」に並べる。 */
function candidateRank(a: DecisionFlowAction): number {
  if (a.execution_status && a.execution_status in CANDIDATE_RANK) return CANDIDATE_RANK[a.execution_status]
  const byStatus: Record<string, number> = { ready: 1, review: 2, deferred: 3, filtered: 4, closed: 5 }
  return byStatus[a.decision_status] ?? 6
}

export type DecisionRows = {
  /** 今日の実候補。実行に近い順。 */
  today: DecisionRow[]
  /** 検討したが見送ったもの。AI不採用 → 対案(採用→棄却)の順。 */
  considered: DecisionRow[]
}

/**
 * 提案の表を組む。
 *
 * 不採用候補は action_stage_log の tier_generated に1件ずつ残っているので、
 * 束ねずにそのまま1行ずつ出す。個票が無いものを合算して表示すると、
 * 実際より情報があるように見える。
 */
export function buildDecisionRows(
  actions: DecisionFlowAction[] | null | undefined,
  unselected: DecisionFlowUnselected[] | null | undefined,
  rebuttals: Rebuttal[] | null | undefined,
): DecisionRows {
  const list = actions ?? []
  const unselectedList = unselected ?? []
  const reb = rebuttals ?? []

  const tickerSeen = new Map<string, number>()
  for (const t of [...list.map(a => a.ticker), ...unselectedList.map(u => u.ticker)]) {
    const k = String(t ?? '')
    if (k) tickerSeen.set(k, (tickerSeen.get(k) ?? 0) + 1)
  }

  const today = [...list]
    .map((action, i) => ({ action, i }))
    .sort((a, b) => candidateRank(a.action) - candidateRank(b.action)
      || String(a.action.ticker ?? '').localeCompare(String(b.action.ticker ?? '')))
    .map(({ action, i }) => candidateRow(action, i))

  const considered = [...droppedRows(unselectedList, tickerSeen), ...rebuttalRows(reb)]

  return { today, considered }
}

/**
 * 「資金移動 → 再評価 → 買付」の依存手順を、表示する1本ずつに絞る純粋関数。
 *
 * API は同じ必要数量に対して複数の資金元（夫楽天JPY / 夫楽天USD / 夫SBI JPY …）を
 * 並べて返す。全部出すと読めないので、必要数量の種類ごとに1本だけ選ぶ。
 *
 * 選び方で1点だけ注意がある。`source_available_native` は名目通貨の生の数字なので、
 * 通貨をまたいで大小比較してはいけない。実データでは USD 54,256（≒¥8.1M）より
 * JPY 195,324 の方が「大きい」と判定されていた。
 *
 * ここでは まず 為替を挟まない経路を優先し、その中で残高の大きい順に採る。
 * 円建て買付に対して為替不要な経路は必ず円貨なので、比較は常に同一通貨内で閉じる。
 * 手数が少なく為替レート変動も挟まない経路を上に出すのは、表示としても正しい。
 */

export type Dict = Record<string, unknown>

export type FundingRoute = {
  ticker: string
  workflow: Dict
  requirement: Dict
}

function asRecord(value: unknown): Dict {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {}
}

/** 為替の両替を挟む経路か。kind の接頭辞と両替額の双方で判定する。 */
export function needsFx(workflow: Dict): boolean {
  return String(workflow.kind || '').startsWith('fx_') || workflow.minimum_fx_native != null
}

/** 実行可能な経路を、必要数量の種類ごとに1本へ絞る。 */
export function fundingRoutes(value: unknown): FundingRoute[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    const row = asRecord(item)
    const ticker = String(row.ticker || '')
    const workflows = (Array.isArray(row.funding_workflows) ? row.funding_workflows : [])
      .map(asRecord)
      .filter(workflow => workflow.can_fund === true)

    const selected = new Map<string, Dict>()
    for (const workflow of workflows) {
      const key = String(asRecord(workflow.requirement).kind || '')
      const previous = selected.get(key)
      if (!previous || rankBetter(workflow, previous)) selected.set(key, workflow)
    }
    return [...selected.values()].map(workflow => ({
      ticker, workflow, requirement: asRecord(workflow.requirement),
    }))
  })
}

/** candidate の方が表示に向くか。為替不要が最優先、次に残高の大きさ。 */
function rankBetter(candidate: Dict, incumbent: Dict): boolean {
  const candidateFx = needsFx(candidate)
  const incumbentFx = needsFx(incumbent)
  if (candidateFx !== incumbentFx) return !candidateFx
  // ここまで来た2本は同じ段（為替あり同士 / なし同士）なので通貨が揃っている
  return Number(candidate.source_available_native || 0) > Number(incumbent.source_available_native || 0)
}

/** 経路の見出し。為替を挟む場合はその一手を明示する。 */
export function fundingRouteLabel(workflow: Dict): string {
  if (needsFx(workflow)) return '楽天USDを両替 → 妻SBIへ移動'
  return `${String(workflow.source_owner || '資金元')}/${String(workflow.source_broker || '')} → 買付口座`
}

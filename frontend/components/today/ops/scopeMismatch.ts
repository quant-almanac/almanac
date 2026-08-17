/**
 * 「計画のスコープと違う階層の建玉が、その計画の予算を押さえている」状態の表示用整形。
 *
 * 例: 金融セクター不足という long 層の計画に対して、実際に出ている V の注文は
 * medium 層のもの。買い足す根拠(long層で不足)と、押さえている予算(medium層の建玉)が
 * 別の母数を見ているので、機械的にどちらかへ寄せると必ず片方が嘘になる。
 *
 * そのため自動取消はせず予約も維持したまま、人間が判断する対象として持ち上げる。
 * ここはその一行を作るだけで、判定そのものはバックエンドが持つ。
 */

import type { ScopeMismatchRecord } from './types'

export type ScopeMismatchView = {
  count: number
  notionalJpy: number
  records: ScopeMismatchRecord[]
}

type ConsumptionLike = {
  scope_mismatched_open_order_count?: number
  scope_mismatched_open_order_notional_jpy?: number
  scope_mismatched_consumption_records?: ScopeMismatchRecord[]
}

/**
 * 表示に必要な形へ寄せる。
 *
 * 件数は「レコードの実数」と「集計値」の大きい方を採る。集計だけが立って
 * 個票が無い場合に 0件と表示すると、実在する要確認を無かったことにしてしまう。
 */
export function scopeMismatchView(
  consumption: ConsumptionLike | null | undefined,
): ScopeMismatchView | null {
  const c = consumption ?? {}
  const records = Array.isArray(c.scope_mismatched_consumption_records)
    ? c.scope_mismatched_consumption_records
    : []
  const reported = Number(c.scope_mismatched_open_order_count ?? 0)
  const count = Math.max(Number.isFinite(reported) ? reported : 0, records.length)
  if (count <= 0) return null
  const notional = Number(c.scope_mismatched_open_order_notional_jpy ?? 0)
  return { count, notionalJpy: Number.isFinite(notional) ? notional : 0, records }
}

const TIER_LABEL: Record<string, string> = {
  long: '長期', medium: '中期', swing: 'スイング',
}

function tier(name: unknown): string {
  const key = String(name ?? '').trim()
  return TIER_LABEL[key] ?? (key || '不明')
}

/**
 * 1件分の説明。「何の注文が」「どの階層の計画の予算を」押さえているかを、
 * 階層名を両方出して示す。片方だけだと何と何が食い違っているか読めない。
 */
export function scopeMismatchLine(record: ScopeMismatchRecord): string {
  const ticker = String(record.ticker || '銘柄不明')
  const held = tier(record.candidate_investment_type)
  const required = (record.required_investment_types ?? []).map(tier).join('・') || '不明'
  const status = record.status === 'placed' || record.status === 'ordered' ? '発注中' : String(record.status || '')
  const head = status ? `${ticker}（${status}）` : ticker
  return `${head}は${held}の注文ですが、${required}の計画予算を押さえています`
}

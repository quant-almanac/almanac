import { describe, expect, it } from 'vitest'
import { scopeMismatchLine, scopeMismatchView } from '../scopeMismatch'

/** Codex のバックエンドテストと同じ実データ形状（V注文1件・¥229,599）。 */
const vRecord = {
  source: 'action_state',
  id: 'state-v',
  ticker: 'V',
  status: 'placed',
  notional_jpy: 229_599,
  plan_item_id: '2026-08-w34-add-sector-financial-services-003',
  required_investment_types: ['long'],
  candidate_investment_type: 'medium',
}

describe('scopeMismatchView', () => {
  it('surfaces the live V order that keeps its budget reservation', () => {
    const view = scopeMismatchView({
      scope_mismatched_open_order_count: 1,
      scope_mismatched_open_order_notional_jpy: 229_599,
      scope_mismatched_consumption_records: [vRecord],
    })
    expect(view).toEqual({ count: 1, notionalJpy: 229_599, records: [vRecord] })
  })

  it('shows nothing when there is no mismatch', () => {
    expect(scopeMismatchView({
      scope_mismatched_open_order_count: 0,
      scope_mismatched_open_order_notional_jpy: 0,
      scope_mismatched_consumption_records: [],
    })).toBeNull()
  })

  it('shows nothing for a plan written before the fields existed', () => {
    // execution_plan_state.json が修正前に生成されているとキー自体が無い。
    // ここで落ちると画面全体が落ちる。
    expect(scopeMismatchView({})).toBeNull()
    expect(scopeMismatchView(undefined)).toBeNull()
    expect(scopeMismatchView(null)).toBeNull()
  })

  it('does not report zero when the count is missing but records exist', () => {
    // 集計が欠けていても個票があるなら実在する。0件と出すと要確認が消える。
    const view = scopeMismatchView({ scope_mismatched_consumption_records: [vRecord] })
    expect(view?.count).toBe(1)
  })

  it('trusts the larger of the reported count and the materialised records', () => {
    // 個票が間引かれていても、件数は集計側の値を下回らない。
    const view = scopeMismatchView({
      scope_mismatched_open_order_count: 3,
      scope_mismatched_consumption_records: [vRecord],
    })
    expect(view?.count).toBe(3)
    expect(view?.records).toHaveLength(1)
  })

  it('survives malformed values instead of rendering NaN', () => {
    const view = scopeMismatchView({
      scope_mismatched_open_order_count: 1,
      scope_mismatched_open_order_notional_jpy: undefined,
      scope_mismatched_consumption_records: 'nope' as never,
    })
    expect(view).toEqual({ count: 1, notionalJpy: 0, records: [] })
  })
})

describe('scopeMismatchLine', () => {
  it('names both tiers so the actual contradiction is readable', () => {
    // 片方の階層しか出さないと、何と何が食い違っているのか読めない。
    const line = scopeMismatchLine(vRecord)
    expect(line).toContain('V')
    expect(line).toContain('中期')
    expect(line).toContain('長期')
  })

  it('marks an order that is still live', () => {
    expect(scopeMismatchLine(vRecord)).toContain('発注中')
    expect(scopeMismatchLine({ ...vRecord, status: 'ordered' })).toContain('発注中')
  })

  it('does not invent a tier name it was not given', () => {
    const line = scopeMismatchLine({ ticker: 'X', required_investment_types: [] })
    expect(line).toContain('不明')
  })

  it('renders without a ticker rather than throwing', () => {
    expect(() => scopeMismatchLine({})).not.toThrow()
    expect(scopeMismatchLine({})).toContain('銘柄不明')
  })
})

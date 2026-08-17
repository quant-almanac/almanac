import { describe, expect, it } from 'vitest'
import { fundingRouteLabel, fundingRoutes, needsFx } from '../fundingRoutes'

/** 実データ(1489.T)と同じ形。43口は3経路、150口は為替経路だけが実行可能。 */
function live() {
  return [{
    ticker: '1489.T',
    funding_workflows: [
      {
        can_fund: true, kind: 'cross_owner_transfer_then_reprice_buy',
        requirement: { kind: 'minimum_executable', target_quantity: 43 },
        minimum_transfer_native: 3723, minimum_fx_native: null,
        source_owner: 'husband', source_broker: 'rakuten',
        source_wallet_key: 'husband|rakuten|broker_cash|JPY', source_available_native: 9000,
      },
      {
        can_fund: true, kind: 'fx_then_cross_owner_transfer_then_reprice_buy',
        requirement: { kind: 'minimum_executable', target_quantity: 43 },
        minimum_transfer_native: 3723, minimum_fx_native: 23.37,
        source_owner: 'husband', source_broker: 'rakuten',
        source_wallet_key: 'husband|rakuten|broker_cash|USD', source_available_native: 54256.14,
      },
      {
        can_fund: true, kind: 'cross_owner_transfer_then_reprice_buy',
        requirement: { kind: 'minimum_executable', target_quantity: 43 },
        minimum_transfer_native: 3723, minimum_fx_native: null,
        source_owner: 'husband', source_broker: 'sbi',
        source_wallet_key: 'husband|sbi|broker_cash|JPY', source_available_native: 195324,
      },
      {
        can_fund: false, kind: 'cross_owner_transfer_then_reprice_buy',
        requirement: { kind: 'original_quantity', target_quantity: 150 },
        minimum_transfer_native: 378116, minimum_fx_native: null,
        source_owner: 'husband', source_broker: 'rakuten',
        source_wallet_key: 'husband|rakuten|broker_cash|JPY', source_available_native: 9000,
      },
      {
        can_fund: true, kind: 'fx_then_cross_owner_transfer_then_reprice_buy',
        requirement: { kind: 'original_quantity', target_quantity: 150 },
        minimum_transfer_native: 378116, minimum_fx_native: 2374,
        source_owner: 'husband', source_broker: 'rakuten',
        source_wallet_key: 'husband|rakuten|broker_cash|USD', source_available_native: 54256.14,
      },
      {
        can_fund: false, kind: 'cross_owner_transfer_then_reprice_buy',
        requirement: { kind: 'original_quantity', target_quantity: 150 },
        minimum_transfer_native: 378116, minimum_fx_native: null,
        source_owner: 'husband', source_broker: 'sbi',
        source_wallet_key: 'husband|sbi|broker_cash|JPY', source_available_native: 195324,
      },
    ],
  }]
}

describe('fundingRoutes', () => {
  it('shows one route per required quantity, not every candidate', () => {
    const rows = fundingRoutes(live())
    expect(rows.map(r => r.requirement.kind)).toEqual(['minimum_executable', 'original_quantity'])
  })

  it('never offers a route the API said cannot fund', () => {
    // 150口のJPY経路2本は can_fund=false。出したら実行できない手順を勧めることになる。
    const rows = fundingRoutes(live())
    const original = rows.find(r => r.requirement.kind === 'original_quantity')!
    expect(original.workflow.can_fund).toBe(true)
  })

  it('prefers the route that needs no FX when one can fund', () => {
    // 43口は円貨だけで賄える。為替を挟む提案を上に出す理由がない。
    const rows = fundingRoutes(live())
    const minimum = rows.find(r => r.requirement.kind === 'minimum_executable')!
    expect(needsFx(minimum.workflow)).toBe(false)
    expect(minimum.workflow.source_wallet_key).toBe('husband|sbi|broker_cash|JPY')
  })

  it('does not compare balances across currencies', () => {
    // source_available_native は名目通貨の生の数字。USD 54,256(≒¥8.1M)と
    // JPY 195,324 を素で比べると USD が小さい方に見える。
    // 比較は同じ段(為替あり同士/なし同士)の中だけで閉じていること。
    const rows = fundingRoutes(live())
    const minimum = rows.find(r => r.requirement.kind === 'minimum_executable')!
    const usdWallet = 'husband|rakuten|broker_cash|USD'
    expect(minimum.workflow.source_wallet_key).not.toBe(usdWallet)
    // 円貨2本のうちは残高の大きい方(SBI 195,324 > 楽天 9,000)
    expect(minimum.workflow.source_available_native).toBe(195324)
  })

  it('falls back to the FX route when nothing else can fund', () => {
    const rows = fundingRoutes(live())
    const original = rows.find(r => r.requirement.kind === 'original_quantity')!
    expect(needsFx(original.workflow)).toBe(true)
    expect(original.workflow.minimum_fx_native).toBe(2374)
  })

  it('carries the quantity and the minimum so the step can be acted on', () => {
    const rows = fundingRoutes(live())
    expect(rows.map(r => [r.requirement.target_quantity, r.workflow.minimum_transfer_native]))
      .toEqual([[43, 3723], [150, 378116]])
  })

  it('returns nothing rather than throwing on missing or malformed input', () => {
    expect(fundingRoutes(undefined)).toEqual([])
    expect(fundingRoutes(null)).toEqual([])
    expect(fundingRoutes([])).toEqual([])
    expect(fundingRoutes([{ ticker: 'X' }])).toEqual([])
    expect(fundingRoutes([{ ticker: 'X', funding_workflows: 'nope' }])).toEqual([])
  })

  it('omits a ticker whose every route is unfundable', () => {
    const rows = fundingRoutes([{
      ticker: 'Z',
      funding_workflows: [
        { can_fund: false, requirement: { kind: 'minimum_executable' } },
      ],
    }])
    expect(rows).toEqual([])
  })
})

describe('needsFx', () => {
  it('detects FX from the workflow kind', () => {
    expect(needsFx({ kind: 'fx_then_cross_owner_transfer_then_reprice_buy' })).toBe(true)
  })

  it('detects FX from an fx amount even if the kind is new or renamed', () => {
    // kind の文字列が将来変わっても、両替額があるなら為替は挟んでいる。
    expect(needsFx({ kind: 'some_future_route', minimum_fx_native: 12 })).toBe(true)
  })

  it('does not call a plain transfer an FX route', () => {
    expect(needsFx({ kind: 'cross_owner_transfer_then_reprice_buy', minimum_fx_native: null })).toBe(false)
  })
})

describe('fundingRouteLabel', () => {
  it('spells out the extra FX step', () => {
    expect(fundingRouteLabel({ kind: 'fx_then_cross_owner_transfer_then_reprice_buy' }))
      .toBe('楽天USDを両替 → 妻SBIへ移動')
  })

  it('names the source account for a plain transfer', () => {
    expect(fundingRouteLabel({
      kind: 'cross_owner_transfer_then_reprice_buy',
      source_owner: 'husband', source_broker: 'sbi',
    })).toBe('husband/sbi → 買付口座')
  })
})

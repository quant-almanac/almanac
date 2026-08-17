import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import OrderMap from '../OrderMap'
import type { BoardRow } from '../types'

const board: BoardRow[] = [{
  ticker: 'ABBV',
  type: 'add',
  action: '買い増し',
  confidence_pct: 66,
  impact_nav_pct: 0.41,
  estimated_notional_jpy: 120_000,
  lifecycle: { status: 'pending' },
}]

describe('OrderMap', () => {
  it('plots rejected decisions with real coordinates and omits qualitative ones', () => {
    render(
      <OrderMap
        board={board}
        selected={0}
        hovered={null}
        onSelect={vi.fn()}
        onHover={vi.fn()}
        onOpen={vi.fn()}
        rejected={[
          { ticker: 'PLTR', action: '高レバレッジ買い', reason: '集中リスク過大', source: 'RED TEAM', verdict: 'reject' },
          { ticker: 'META', action: '追加購入', reason: '計画枠を消費済み', source: 'PLAN GATE', verdict: 'reject', confidence_pct: 72, impact_nav_pct: 0.61 },
        ]}
      />,
    )

    expect(screen.getByText('採用 1 · 要確認 0 · 不採用 1')).toBeInTheDocument()
    expect(screen.queryByText('NOT ADOPTED · 評価軸外')).not.toBeInTheDocument()
    expect(screen.queryByText('PLTR')).not.toBeInTheDocument()
    expect(screen.getByText('META')).toBeInTheDocument()

    fireEvent.mouseEnter(screen.getByLabelText('META 不採用。計画枠を消費済み'))
    expect(screen.getByText('確信度 72% · 影響 0.61%')).toBeInTheDocument()
    expect(screen.getByText('計画枠を消費済み')).toBeInTheDocument()
  })

  it('plots review candidates even when nothing is orderable', () => {
    // board=0 の日でも review_board に座標があれば地図は成立する。
    // ここが空だと「全件要確認」の日に判断材料が一切描かれない。
    render(
      <OrderMap
        board={[]}
        review={[
          { ticker: 'QQQ', type: 'buy', action: '買い', confidence_pct: 57, impact_nav_pct: 2.63, lifecycle: { status: 'pending' } },
          { ticker: 'COST', type: 'sell', action: '売り', confidence_pct: 41, impact_nav_pct: 0.5, lifecycle: { status: 'pending' } },
          { ticker: 'NOCOORD', type: 'buy', action: '買い', lifecycle: { status: 'pending' } },
        ] as BoardRow[]}
        selected={0}
        hovered={null}
        onSelect={vi.fn()}
        onHover={vi.fn()}
        onOpen={vi.fn()}
      />,
    )

    // 座標を持つ2件だけが点になる
    expect(screen.getByText('採用 0 · 要確認 2 · 不採用 0')).toBeInTheDocument()
    expect(screen.getByText('QQQ')).toBeInTheDocument()
    expect(screen.getByText('COST')).toBeInTheDocument()
    expect(screen.queryByText('NOCOORD')).not.toBeInTheDocument()
  })
})

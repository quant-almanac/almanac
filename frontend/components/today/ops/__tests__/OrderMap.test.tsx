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

    expect(screen.getByText('採用 1 · 不採用 1')).toBeInTheDocument()
    expect(screen.queryByText('NOT ADOPTED · 評価軸外')).not.toBeInTheDocument()
    expect(screen.queryByText('PLTR')).not.toBeInTheDocument()
    expect(screen.getByText('META')).toBeInTheDocument()

    fireEvent.mouseEnter(screen.getByLabelText('META 不採用。計画枠を消費済み'))
    expect(screen.getByText('確信度 72% · 影響 0.61%')).toBeInTheDocument()
    expect(screen.getByText('計画枠を消費済み')).toBeInTheDocument()
  })
})

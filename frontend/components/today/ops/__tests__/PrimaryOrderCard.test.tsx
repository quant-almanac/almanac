import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import PrimaryOrderCard from '../PrimaryOrderCard'
import type { BoardRow } from '../types'

const row: BoardRow = {
  ticker: 'NVDA',
  type: 'add',
  action: '買い増し',
  amount_hint: '買い増し 12株 @$118.40',
  limit_price: 118.4,
  confidence_pct: 72,
  estimated_notional_jpy: 214_000,
  lifecycle: { status: 'pending' },
}

describe('PrimaryOrderCard', () => {
  it('surfaces quantity, limit, confidence and notional as readable figures', () => {
    render(<PrimaryOrderCard row={row} quadrant="主戦場" onOpen={vi.fn()} />)

    expect(screen.getByText('NVDA')).toBeInTheDocument()
    expect(screen.getByText('買い増し')).toBeInTheDocument()
    expect(screen.getByText('12株')).toBeInTheDocument()
    expect(screen.getByText('$118.4')).toBeInTheDocument()
    expect(screen.getByText('72%')).toBeInTheDocument()
    expect(screen.getByText('¥21万')).toBeInTheDocument()
    expect(screen.getByText('◎ 主戦場')).toBeInTheDocument()
  })

  it('opens the existing recording flow rather than writing directly', () => {
    const onOpen = vi.fn()
    render(<PrimaryOrderCard row={row} onOpen={onOpen} />)

    fireEvent.click(screen.getByRole('button', { name: '✓ 発注を記録する' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('does not invent a quantity when the engine did not size the order', () => {
    render(<PrimaryOrderCard row={{ ...row, amount_hint: undefined }} onOpen={vi.fn()} />)

    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.getByText('数量は記録時に確定')).toBeInTheDocument()
  })

  it('falls back to 成行 when there is no limit price', () => {
    render(<PrimaryOrderCard row={{ ...row, limit_price: undefined }} onOpen={vi.fn()} />)

    expect(screen.getByText('成行')).toBeInTheDocument()
  })
})

describe('PrimaryOrderCard warnings', () => {
  it('carries pre-order warnings up from the list row', () => {
    // 一覧行から主役カードへ昇格させたとき、発注直前の注意が消えないこと
    render(<PrimaryOrderCard
      row={{ ...row, market_quote_confirmation_required: true }}
      onOpen={vi.fn()}
    />)
    expect(screen.getByText('発注時に現在値確認')).toBeInTheDocument()
  })

  it('shows the reprice deferral notice', () => {
    render(<PrimaryOrderCard
      row={{ ...row, lifecycle: { status: 'pending', expiry_deferred_until_reprice: true, market_reprice_after: '2026-08-10T00:00:00Z' } }}
      onOpen={vi.fn()}
    />)
    expect(screen.getByText('次回朝分析で再評価')).toBeInTheDocument()
  })
})

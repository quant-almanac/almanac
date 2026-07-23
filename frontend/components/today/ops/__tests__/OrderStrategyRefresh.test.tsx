import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import OrderStrategyRefresh from '../OrderStrategyRefresh'

describe('OrderStrategyRefresh', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('shows the cost notice before any refresh request is made', () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    render(<OrderStrategyRefresh />)

    fireEvent.click(screen.getByRole('button', { name: '⚡ 指値再計算' }))

    expect(screen.getByText('指値・注文方法を再計算しますか？')).toBeInTheDocument()
    expect(screen.getByText(/LLM実行により課金が発生/)).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

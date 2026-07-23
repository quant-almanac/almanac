import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ShortSignalsPanel from '../ShortSignalsPanel'

describe('ShortSignalsPanel', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('renders active short-term signal data from the signals endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ signals: { NVDA: { score: 4, entry_price: 150, target_price: 175, stop_loss: 143, reason: 'momentum' } } }),
    }))

    render(<ShortSignalsPanel />)

    await waitFor(() => expect(screen.getByText('NVDA')).toBeInTheDocument())
    expect(screen.getByText('★ 4')).toBeInTheDocument()
    expect(screen.getByText(/TP 175/)).toBeInTheDocument()
  })
})

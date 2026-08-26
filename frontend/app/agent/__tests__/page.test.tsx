import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import AgentPage from '../page'
import { API_BASE } from '@/lib/api'

/**
 * enabled-modes の取得先が API_BASE を経由していること。
 *
 * 以前は `fetch('/api/agent/enabled-modes')` と相対パスのままで、
 * 3000番 (frontend) 側に叩きに行って 404 になっていた
 * (Codex レビュー round 17 で実測: 127.0.0.1:3000 は 404、:8000 は 200)。
 * fallback が default のみなので見かけ上は動くが、risk/NISA を
 * 再有効化しても永久にタブへ反映されない。
 */
function mockFetch(urls: Record<string, unknown>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    for (const [match, body] of Object.entries(urls)) {
      if (url.includes(match)) {
        return { ok: true, json: async () => body } as Response
      }
    }
    return { ok: false, json: async () => ({}) } as Response
  })
}

describe('AgentPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('fetches enabled-modes from the backend origin, not the frontend origin', async () => {
    const fetchMock = mockFetch({
      '/api/agent/enabled-modes': { enabled_modes: ['default'], all_modes: ['default', 'risk', 'nisa'] },
      '/api/agent/result': { headline: 'h', actions: [] },
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AgentPage />)

    await waitFor(() => {
      const calledUrls = fetchMock.mock.calls.map(c => String(c[0]))
      expect(calledUrls.some(u => u.includes('enabled-modes'))).toBe(true)
    })

    const enabledModesCall = fetchMock.mock.calls
      .map(c => String(c[0]))
      .find(u => u.includes('enabled-modes'))
    expect(enabledModesCall).toBe(`${API_BASE}/api/agent/enabled-modes`)
    // 素の相対パスへ戻っていないこと (これが実際に壊れていた形)。
    expect(enabledModesCall).not.toBe('/api/agent/enabled-modes')
  })
})

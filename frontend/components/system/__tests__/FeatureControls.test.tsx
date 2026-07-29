import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { SWRConfig } from 'swr'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import FeatureControls from '../FeatureControls'

const features = [
  {
    key: 'us_short',
    label: '米国株の空売り',
    category: 'short',
    description: '米国株の下落候補を検出します。',
    configured_enabled: false,
    effective_enabled: false,
    mutable: true,
    mode: 'human_execution_only',
    auto_order_enabled: false,
    reason: 'ユーザー設定でOFFです',
    blockers: [],
    eligible_instruments: 88,
    source_note: '発注画面が権威',
    source_age_hours: 24,
  },
  {
    key: 'ginn',
    label: 'GINNボラティリティ',
    category: 'model',
    description: '検証済みbundleだけを使います。',
    configured_enabled: true,
    effective_enabled: false,
    mutable: false,
    mode: 'promoted_bundle_only',
    auto_order_enabled: false,
    reason: '昇格済みGINN bundleがありません',
    blockers: ['promoted_bundle_missing_or_disabled'],
    control_hint: 'モデル昇格ゲートが権威です',
  },
]

function renderPanel() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <FeatureControls />
    </SWRConfig>,
  )
}

describe('FeatureControls', () => {
  beforeEach(() => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') {
        features[0] = {
          ...features[0],
          configured_enabled: true,
          effective_enabled: true,
          reason: '候補生成を有効化しています',
        }
        return new Response(JSON.stringify(features[0]), { status: 200 })
      }
      return new Response(JSON.stringify({
        generated_at: '2026-07-29T10:00:00+00:00',
        features,
      }), { status: 200 })
    }))
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    features[0] = {
      ...features[0],
      configured_enabled: false,
      effective_enabled: false,
      reason: 'ユーザー設定でOFFです',
    }
  })

  it('shows effective state, disabled reason, and read-only authority', async () => {
    renderPanel()

    const us = await screen.findByTestId('feature-us_short')
    expect(within(us).getAllByText('OFF')).toHaveLength(2)
    expect(within(us).getByText('ユーザー設定でOFFです')).toBeInTheDocument()
    expect(within(us).getByRole('switch')).toHaveAttribute('aria-checked', 'false')

    const ginn = screen.getByTestId('feature-ginn')
    expect(within(ginn).getByText('設定ON・安全停止')).toBeInTheDocument()
    expect(within(ginn).getByText('FAIL-CLOSED')).toBeInTheDocument()
    expect(within(ginn).getByText('参照のみ')).toBeInTheDocument()
    expect(within(ginn).getByText(/モデル昇格ゲートが権威/)).toBeInTheDocument()
  })

  it('toggles a mutable feature through the authenticated write helper', async () => {
    renderPanel()
    const toggle = await screen.findByRole('switch', { name: '米国株の空売りをONにする' })
    fireEvent.click(toggle)

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/features/us_short'),
      expect.objectContaining({ method: 'POST' }),
    ))
    await screen.findByText('米国株の空売りをONにしました。')
  })
})

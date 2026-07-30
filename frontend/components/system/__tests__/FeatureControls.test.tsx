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
    warnings: ['最新の価格取得率が46.7%です'],
    eligible_instruments: 88,
    availability_universe_instruments: 679,
    availability_coverage_pct: 13.0,
    availability_label: '借株proxy該当',
    latest_scan_requested: 157,
    latest_scan_downloaded: 75,
    latest_scan_coverage_pct: 47.8,
    latest_candidates: 11,
    latest_shortable: 9,
    latest_scan_as_of: '2026-07-29T18:30:00+09:00',
    latest_scan_status: 'degraded',
    source: 'data/broker_short_us.json',
    source_as_of: '2026-07-29T18:00:00+09:00',
    freshness_status: 'fresh',
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
    source: 'models/ginn/current.json + promoted manifest',
    freshness_status: 'missing',
    control_hint: 'モデル昇格ゲートが権威です',
    roadmap_status: 'future_update',
    roadmap_label: '将来更新',
    metrics: [
      { label: 'GARCH比MSE', value: 8.15 },
      { label: 'forward観測', value: 0 },
    ],
    detail_sections: [
      {
        title: '現在の判断',
        body: 'GINNという考え方を否定した状態ではありません。',
      },
      {
        title: '原論文との差',
        items: ['現実装は60日窓・2層×64です。'],
      },
      {
        title: '将来の更新条件',
        items: ['未観測期間をshadowで評価します。'],
      },
    ],
    references: [
      {
        label: 'GINN原論文（ICAIF 2024 / arXiv）',
        url: 'https://arxiv.org/abs/2410.00288',
      },
    ],
  },
  {
    key: 'options_signals',
    label: 'options_signals',
    category: 'status_error',
    description: '運用状態の取得に失敗しました。',
    configured_enabled: false,
    effective_enabled: false,
    mutable: false,
    mode: 'status_resolution_error',
    auto_order_enabled: false,
    reason: 'options_signalsの状態を安全に判定できません',
    blockers: ['status_resolution_error'],
    status_resolution_failed: true,
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
    expect(within(us).getByText(/最新の価格取得率/)).toBeInTheDocument()
    expect(within(us).getByText(/借株proxy該当 88\/679/)).toBeInTheDocument()
    expect(within(us).getByText(/最新価格 75\/157/)).toBeInTheDocument()
    expect(within(us).getByText('候補 11')).toBeInTheDocument()
    expect(within(us).getByText('借株可 9')).toBeInTheDocument()
    expect(within(us).getByText(/最新スキャン: 2026-07-29T18:30/)).toBeInTheDocument()
    expect(within(us).getByTestId('feature-us_short-authority')).toHaveTextContent('data/broker_short_us.json')
    expect(within(us).getByTestId('feature-us_short-authority')).toHaveTextContent('新鮮')
    expect(within(us).getByRole('switch')).toHaveAttribute('aria-checked', 'false')

    const ginn = screen.getByTestId('feature-ginn')
    expect(within(ginn).getByText('設定ON・安全停止')).toBeInTheDocument()
    expect(within(ginn).getByText('FAIL-CLOSED')).toBeInTheDocument()
    expect(within(ginn).getByText('将来更新')).toBeInTheDocument()
    expect(within(ginn).getByText('参照のみ')).toBeInTheDocument()
    expect(within(ginn).getByText('現在の判断')).toBeInTheDocument()
    expect(within(ginn).getByText(/GINNという考え方を否定/)).toBeInTheDocument()
    expect(within(ginn).getByText('原論文との差')).toBeInTheDocument()
    expect(within(ginn).getByText('将来の更新条件')).toBeInTheDocument()
    expect(within(ginn).getByRole('link', { name: 'GINN原論文（ICAIF 2024 / arXiv）' })).toHaveAttribute(
      'href',
      'https://arxiv.org/abs/2410.00288',
    )
    expect(within(ginn).getByText(/モデル昇格ゲートが権威/)).toBeInTheDocument()
    expect(within(ginn).getByTestId('feature-ginn-authority')).toHaveTextContent('models/ginn/current.json')

    const failed = screen.getByTestId('feature-options_signals')
    expect(within(failed).getByText('状態取得失敗')).toBeInTheDocument()
    expect(within(failed).getByText('ERROR')).toBeInTheDocument()
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

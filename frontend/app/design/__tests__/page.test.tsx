import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import DesignPage from '../page'

const useSWRMock = vi.hoisted(() => vi.fn())

vi.mock('swr', () => ({ default: useSWRMock }))
vi.mock('@/components/system/FeatureControls', () => ({
  default: () => <div data-testid="feature-controls">feature inventory</div>,
}))

const status = {
  generated_at: '2026-07-29T10:00:00+00:00',
  data_health: { ok: true, missing_count: 0, stale_count: 0, sources: {} },
  auto_tune: { mode: 'apply', audit: { status: 'ok' } },
  model_routes: [{ role: 'final_synthesis', model: 'claude-opus-5', adapter: 'anthropic' }],
  guards: { daily_loss_limit_pct: -5 },
  feature_modes: { execution_plan: 'observe' },
  heartbeat_statuses: [{
    key: 'portfolio_analyst',
    status: 'ok',
    freshness_status: 'fresh',
    monitored: true,
    last_run_iso: '2026-07-29T06:00:00+09:00',
    age_hours: 4,
    max_age_hours: 26,
    error: null,
  }],
  schedules: {},
}

describe('System page', () => {
  beforeEach(() => useSWRMock.mockReset())

  it('shows the feature inventory even when the status endpoint fails', () => {
    useSWRMock.mockReturnValue({ data: undefined, error: new Error('offline'), isLoading: false })
    render(<DesignPage />)

    expect(screen.getByTestId('feature-controls')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('/api/system/status')
  })

  it('shows heartbeat freshness and last successful execution', () => {
    useSWRMock.mockReturnValue({ data: status, error: undefined, isLoading: false })
    render(<DesignPage />)

    const panel = screen.getByTestId('heartbeat-statuses')
    expect(panel).toHaveTextContent('portfolio_analyst')
    expect(panel).toHaveTextContent('fresh')
    expect(panel).toHaveTextContent('2026-07-29 06:00:00')
    expect(panel).toHaveTextContent('4h / 26h')
  })
})

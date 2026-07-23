import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import ActionSection, { formatExecutionPlanRationale } from '../ActionSection'
import type { ExecutionPlan } from '../types'

describe('formatExecutionPlanRationale', () => {
  it('renders structured API rationale as text instead of a React child object', () => {
    expect(formatExecutionPlanRationale({
      reason_code: 'covered_by_open_orders',
      message: '既存注文で計画枠を充足しています。',
    })).toBe('既存注文で計画枠を充足しています。')
  })

  it('keeps legacy string rationale rows compatible', () => {
    expect(formatExecutionPlanRationale('旧形式の理由')).toBe('旧形式の理由')
    expect(formatExecutionPlanRationale({ reason_code: 'legacy' })).toBe('legacy')
  })

  it('shows the compact today-decision card and keeps legacy attribution detail in the full plan modal', () => {
    const executionPlan: ExecutionPlan = {
      status: 'active',
      horizon: {},
      budgets: {},
      consumption: {
        unattributed_monthly_total_count: 2,
        unattributed_monthly_total_notional_jpy: 130_000,
      },
      summary: { items_total: 0, active_items: 0, covered_items: 0, board_count: 0, plan_filtered_count: 0 },
      items: [],
      today_decision: { code: 'wait_candidate', label: '候補待ち', reason: 'テスト用' },
      filtered_summary: {},
      filtered_examples: [],
      warnings: [],
      no_action_rationale: [],
    }

    render(
      <ActionSection
        board={[]}
        notes={[]}
        executionPlan={executionPlan}
        selected={0}
        onSelect={vi.fn()}
      />,
    )

    expect(screen.getByText('EXECUTION PLAN')).toBeInTheDocument()
    expect(screen.getByText('候補待ち')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '計画の全文 →' }))
    expect(screen.getByText(/未帰属の注文・約定 2件/)).toBeInTheDocument()
    expect(screen.getByText(/月次枠に未算入/)).toBeInTheDocument()
  })

  it('renders review candidates without any execution control', () => {
    render(
      <ActionSection
        board={[]}
        reviewBoard={[{
          ticker: 'ROBO',
          type: 'buy',
          action: 'ROBOを成行買い',
          execution_readiness: 'blocked',
          execution_block_reasons: [{ code: 'market_spread_too_wide', message: 'スプレッドが広すぎます' }],
          lifecycle: { status: 'proposed' },
        }]}
        notes={[]}
        selected={0}
        onSelect={vi.fn()}
      />,
    )

    expect(screen.getByText('ROBO')).toBeInTheDocument()
    expect(screen.getByText('BLOCKED')).toBeInTheDocument()
    expect(screen.getByText('スプレッドが広すぎます')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '記録する' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '指値を出した' })).not.toBeInTheDocument()
  })

  it('keeps a morning candidate actionable while showing quote confirmation', () => {
    render(
      <ActionSection
        board={[{
          ticker: '1489.T',
          type: 'buy',
          action: '通勤中に指値注文',
          amount_hint: '100口',
          limit_price: 2_950,
          execution_readiness: 'ready',
          market_quote_confirmation_required: true,
          execution_advisories: [{
            code: 'market_quote_confirmation_required',
            message: '発注時に現在値・スプレッドを確認してください',
          }],
          lifecycle: {
            status: 'pending',
            expiry_starts_at: '2026-07-22T00:00:00+00:00',
            expiry_at: '2026-07-22T04:00:00+00:00',
          },
        }]}
        notes={[]}
        selected={0}
        onSelect={vi.fn()}
      />,
    )

    expect(screen.getByText('発注時に現在値確認')).toBeInTheDocument()
  })
})

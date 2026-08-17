import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('next/navigation', () => ({ usePathname: () => '/' }))

import DecisionFlow from '../DecisionFlow'

const stages = [
  { key: 'candidate_generation', entered: 15, passed: 15, review: 0, rejected: 0, deferred: 0, executed: 0 },
  { key: 'synthesis', entered: 15, passed: 3, review: 0, rejected: 0, deferred: 0, executed: 0 },
  { key: 'policy', entered: 3, passed: 3, review: 0, rejected: 0, deferred: 0, executed: 0 },
  { key: 'post_filter', entered: 3, passed: 3, review: 0, rejected: 0, deferred: 0, executed: 0 },
  { key: 'execution_readiness', entered: 3, passed: 1, review: 2, rejected: 0, deferred: 0, executed: 0 },
].map(s => ({ ...s, provenance: 'action_stage_log', source_stage_keys: [] }))

function flowWith(actions: unknown[], overrides: Record<string, unknown> = {}) {
  return {
    version: 1, analysis_id: 'a1', status: 'complete', stages,
    detail_coverage: { status: 'complete', filtered_total: 0, filtered_materialized: 0, sample_limit: null },
    integrity: { status: 'ok', scope: 'analysis_id', unit: 'account_action', account_branch_count: 0 },
    actions, ...overrides,
  } as never
}

const READY = { key: 'ready', ticker: 'RDY-T', identity_quality: 'exact', decision_status: 'ready', execution_status: 'not_started', stage_states: {}, reason_codes: [], reasons: [] }
const REVIEW = { key: 'review', ticker: 'RVW-T', identity_quality: 'exact', decision_status: 'review', execution_status: 'not_started', stage_states: {}, reason_codes: ['blocked'], reasons: [{ code: 'blocked', message: '安全ゲート確認中', provenance: 'action_stage_log' }] }
const EXPIRED = { key: 'expired', ticker: 'EXP-T', identity_quality: 'exact', decision_status: 'closed', execution_status: 'expired', stage_states: {}, reason_codes: [], reasons: [] }

describe('DecisionFlow', () => {
  it('states the biggest drop in words — 一番落ちた場所が最初に読める', () => {
    render(<DecisionFlow selectedKey={null} onSelect={() => {}} flow={flowWith([READY])} />)

    // synthesis で 15→3、つまり 12件が採用されなかった。
    // ヘッドラインとファネル行の両方に出るので複数ヒットする。
    expect(screen.getAllByText('AI合成').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('12件')).toBeInTheDocument()
    expect(screen.getByText(/最大の脱落/)).toBeInTheDocument()

    // 既定は MAP。FUNNEL に切り替えると、脱落がそれが起きた段の行に紐づく
    fireEvent.click(screen.getByText('FUNNEL'))
    expect(screen.getByText('−12 AIが採用せず')).toBeInTheDocument()
  })

  it('never reports review or expired candidates as approved', () => {
    render(<DecisionFlow selectedKey={null} onSelect={() => {}} flow={flowWith([READY, REVIEW, EXPIRED])} />)

    expect(screen.getByText('発注可能')).toBeInTheDocument()   // ready のみ
    expect(screen.getByText('要確認')).toBeInTheDocument()     // review
    expect(screen.getByText('期限切れ')).toBeInTheDocument()   // expired は承認ではない
    // 期限切れ・要確認が「発注可能」として数えられていないこと
    expect(screen.getAllByText('発注可能')).toHaveLength(1)
  })

  it('shows the stop reason for each candidate', () => {
    render(<DecisionFlow selectedKey={null} onSelect={() => {}} flow={flowWith([REVIEW])} />)
    expect(screen.getByText('安全ゲート確認中')).toBeInTheDocument()
  })

  it('marks the selected candidate track', () => {
    render(<DecisionFlow selectedKey="review" onSelect={() => {}} flow={flowWith([READY, REVIEW])} />)
    const active = document.querySelectorAll('.df-track[data-active="true"]')
    expect(active).toHaveLength(1)
    expect(active[0].textContent).toContain('RVW-T')
  })

  it('reports selection back to the parent so ORDERS stays in sync', async () => {
    const onSelect = vi.fn()
    render(<DecisionFlow selectedKey={null} onSelect={onSelect} flow={flowWith([READY, REVIEW])} />)

    const tracks = document.querySelectorAll<HTMLButtonElement>('.df-track')
    tracks[1].click()
    expect(onSelect).toHaveBeenCalledWith('review')
  })

  it('warns when the ledger and the board disagree', () => {
    render(<DecisionFlow selectedKey={null} onSelect={() => {}} flow={flowWith([READY], {
      integrity: { status: 'mismatch', scope: 'analysis_id', unit: 'account_action', account_branch_count: 1 },
    })} />)
    expect(screen.getByText(/board \/ review_board を正本として表示/)).toBeInTheDocument()
  })

  it('falls back cleanly when the flow is unavailable', () => {
    render(<DecisionFlow selectedKey={null} onSelect={() => {}} flow={{
      version: 1, analysis_id: null, status: 'unavailable', stages: [], actions: [],
      detail_coverage: { status: 'unavailable', filtered_total: 0, filtered_materialized: 0, sample_limit: null },
      integrity: { status: 'ok', scope: 'analysis_id', unit: 'account_action', account_branch_count: 0 },
    } as never} />)
    expect(screen.getByText('今回の分析経路を取得できません。')).toBeInTheDocument()
  })
})

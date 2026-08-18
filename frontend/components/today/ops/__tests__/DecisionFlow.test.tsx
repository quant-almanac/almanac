import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('next/navigation', () => ({ usePathname: () => '/' }))

import DecisionFlow from '../DecisionFlow'

function flowWith(
  actions: unknown[],
  overrides: Record<string, unknown> = {},
  unselected: unknown[] = [],
) {
  return {
    version: 1, analysis_id: 'a1', status: 'complete', stages: [],
    detail_coverage: { status: 'complete', filtered_total: 0, filtered_materialized: 0, sample_limit: null },
    integrity: { status: 'ok', scope: 'analysis_id', unit: 'account_action', account_branch_count: 0 },
    actions, unselected, ...overrides,
  } as never
}

const READY = { key: 'ready', ticker: 'RDY-T', identity_quality: 'exact', decision_status: 'ready', execution_status: 'not_started', stage_states: {}, reason_codes: [], reasons: [] }
const REVIEW = { key: 'review', ticker: 'RVW-T', identity_quality: 'exact', decision_status: 'review', execution_status: 'not_started', stage_states: {}, reason_codes: ['blocked'], reasons: [{ code: 'blocked', message: '安全ゲート確認中', provenance: 'action_stage_log' }] }
const EXPIRED = { key: 'expired', ticker: 'EXP-T', identity_quality: 'exact', decision_status: 'closed', execution_status: 'expired', stage_states: {}, reason_codes: [], reasons: [] }

describe('DecisionFlow', () => {
  it('summarises today’s slate in one line — no click needed to see the outcome', () => {
    render(<DecisionFlow selectedKey={null} onSelect={() => {}} flow={flowWith([READY, REVIEW])} />)
    expect(screen.getByText('発注可能')).toBeInTheDocument()
    expect(screen.getByText('要確認')).toBeInTheDocument()
    // 理由はクリック前から見える（旧 TRACKS の挙動を維持）
    expect(screen.getByText('安全ゲート確認中')).toBeInTheDocument()
  })

  it('never reports review or expired candidates as approved', () => {
    render(<DecisionFlow selectedKey={null} onSelect={() => {}} flow={flowWith([READY, REVIEW, EXPIRED])} />)

    expect(screen.getByText('発注可能')).toBeInTheDocument()   // ready のみ
    expect(screen.getByText('要確認')).toBeInTheDocument()     // review
    expect(screen.getByText('期限切れ')).toBeInTheDocument()   // expired は承認ではない
    expect(screen.getAllByText('発注可能')).toHaveLength(1)
  })

  it('lists dropped candidates and rebuttals in the same table as today’s slate', () => {
    // 依頼: MAP・TRACKS・対案パネルの3箇所を突き合わせずに1箇所で読めること。
    render(<DecisionFlow selectedKey={null} onSelect={() => {}}
      flow={flowWith([READY], {}, [{ ticker: 'AVGO', type: 'trim', tier: 'Long', confidence_pct: 40 }])}
      engine={{
        red_team: [{ ticker: 'CRL', action: 'short', verdict: 'reject', verdict_reason: '踏み上げリスク' }],
        attacks: [], underutilized: [], lanes: [], funnel: [],
      } as never} />)

    expect(screen.getByText('RDY-T')).toBeInTheDocument()   // 今日の候補
    expect(screen.getByText('AVGO')).toBeInTheDocument()    // AI不採用
    expect(screen.getByText('CRL')).toBeInTheDocument()     // 対案
    expect(screen.getByText(/検討して見送ったもの/)).toBeInTheDocument()
  })

  it('marks the selected candidate row', () => {
    render(<DecisionFlow selectedKey="review" onSelect={() => {}} flow={flowWith([READY, REVIEW])} />)
    const active = document.querySelectorAll('.df-row[data-active="true"]')
    expect(active).toHaveLength(1)
    expect(active[0].textContent).toContain('RVW-T')
  })

  it('reports selection back to the parent so ORDERS stays in sync', () => {
    const onSelect = vi.fn()
    render(<DecisionFlow selectedKey={null} onSelect={onSelect} flow={flowWith([READY, REVIEW])} />)

    const rows = document.querySelectorAll<HTMLButtonElement>('.df-row[data-kind="candidate"]')
    fireEvent.click(rows[1])
    expect(onSelect).toHaveBeenCalledWith('review')
  })

  it('expands a row’s full detail on click, and collapses it again on a second click', () => {
    render(<DecisionFlow selectedKey={null} onSelect={() => {}}
      flow={flowWith([], {}, [{ ticker: 'NEM', type: 'trim', tier: 'Long', confidence_pct: 35, estimated_notional_jpy: 18208 }])} />)

    expect(screen.queryByText(/想定 ¥18,208/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('NEM'))
    expect(screen.getByText(/想定 ¥18,208/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('NEM'))
    expect(screen.queryByText(/想定 ¥18,208/)).not.toBeInTheDocument()
  })

  it('does not let a click on a dropped or rebuttal row report a selection', () => {
    const onSelect = vi.fn()
    render(<DecisionFlow selectedKey={null} onSelect={onSelect}
      flow={flowWith([], {}, [{ ticker: 'NEM', type: 'trim', tier: 'Long' }])} />)
    fireEvent.click(screen.getByText('NEM'))
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('collapses a candidate row on a second click even though selecting it never clears', () => {
    // 実機で発見: 展開を選択(selectedKey)と結合していたら、1回目のクリックで
    // 開いて選択され、2回目でexpandedからは外れるのにselectedKeyが残ったままの
    // せいで開いたまま＝二度と閉じられないトグルになっていた。
    render(<DecisionFlow selectedKey={null} onSelect={() => {}} flow={flowWith([REVIEW])} />)
    fireEvent.click(screen.getByText('RVW-T'))
    expect(screen.getByText('安全ゲート確認中')).toBeInTheDocument()
    // 詳細欄は headline と別に出るので、展開時に増える要素で開閉を判定する
    expect(document.querySelector('.df-detail')).toBeTruthy()
    fireEvent.click(screen.getByText('RVW-T'))
    expect(document.querySelector('.df-detail')).toBeFalsy()
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

  it('falls back cleanly when there are no candidates and nothing was considered either', () => {
    render(<DecisionFlow selectedKey={null} onSelect={() => {}} flow={flowWith([])} />)
    expect(screen.getByText('今回の分析で追跡できる候補がありません。')).toBeInTheDocument()
  })
})

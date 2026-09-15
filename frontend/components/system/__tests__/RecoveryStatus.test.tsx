import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import RecoveryStatus, { recoveryState } from '../RecoveryStatus'

describe('recovery diagnostics', () => {
  it.each([undefined, null, {}, { status: 'clear', pending_count: '0', execution_authorized: false },
    { status: 'clear', pending_count: 2, execution_authorized: false },
    { status: 'clear', pending_count: 0, execution_authorized: true }])('does not bless missing/malformed status', value => {
    expect(recoveryState(value)).toBe('unknown')
  })
  it('shows separate counts without an override control', () => {
    render(<RecoveryStatus portfolio={{ status: 'needs_reconciliation', pending_count: 2, execution_authorized: false }}
      brokerImport={{ status: 'clear', pending_count: 0, execution_authorized: false }} />)
    expect(screen.getByTestId('recovery-status')).toHaveTextContent('約定反映: 要照合（未復旧 2件）')
    expect(screen.getByTestId('recovery-status')).toHaveTextContent('残高インポート: 未復旧記録なし')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
  it('does not display cached clear as current after fetch failure', () => {
    render(<RecoveryStatus portfolio={{ status: 'clear', pending_count: 0, execution_authorized: false }} unavailable />)
    expect(screen.getByTestId('recovery-status')).not.toHaveTextContent('未復旧記録なし')
  })
})

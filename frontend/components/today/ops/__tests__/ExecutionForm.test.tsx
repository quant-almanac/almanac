import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ExecutionForm from '../ExecutionForm'
import type { BoardRow } from '../types'

const apiFetchMock = vi.hoisted(() => vi.fn())
const mutateMock = vi.hoisted(() => vi.fn())

vi.mock('@/lib/api', () => ({
  apiFetch: apiFetchMock,
  apiErrorMessage: (_payload: unknown, fallback: string) => fallback,
}))
vi.mock('swr', () => ({ useSWRConfig: () => ({ mutate: mutateMock }) }))

const row: BoardRow = {
  ticker: '1489.T',
  type: 'buy',
  action: '1489.Tを100口買付',
  amount_hint: '100口',
  execution_account: 'NISA成長投資枠',
  execution_investment_type: 'long',
  execution_owner: 'wife',
  execution_broker: 'sbi',
  execution_position_keys: ['1489_WIFE'],
  lifecycle: { status: 'pending' },
}

describe('ExecutionForm execution contract', () => {
  beforeEach(() => {
    apiFetchMock.mockReset()
    mutateMock.mockReset()
  })

  it('parses 口 quantities and prioritizes structured routing defaults', () => {
    render(<ExecutionForm row={row} onClose={vi.fn()} />)
    expect(screen.getByLabelText('数量（口）')).toHaveValue('100')
    expect(screen.getByLabelText('口座')).toHaveValue('NISA成長投資枠')
    expect(screen.getByLabelText('投資区分')).toHaveValue('long')
  })

  it('keeps the same idempotency key across a failed retry', async () => {
    apiFetchMock.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) })
    render(<ExecutionForm row={row} onClose={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('約定価格'), { target: { value: '3300' } })
    fireEvent.click(screen.getByRole('button', { name: '記録する' }))
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: '記録する' }))
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(2))
    const first = JSON.parse(apiFetchMock.mock.calls[0][1].body)
    const second = JSON.parse(apiFetchMock.mock.calls[1][1].body)
    expect(first.idempotency_key).toBeTruthy()
    expect(second.idempotency_key).toBe(first.idempotency_key)
    expect(first.execution_owner).toBe('wife')
    expect(first.execution_position_keys).toEqual(['1489_WIFE'])
  })

  it('records selected approved funding on a routed buy only', async () => {
    apiFetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ ok: true, id: 'exec-1', portfolio: {} }) })
    render(<ExecutionForm row={row} onClose={vi.fn()} fundingOptions={[
      { id: 'wife-sbi-salary', source: 'salary', bucket: 'normal', owner: 'wife', broker: 'sbi', available_jpy: 100_000 },
    ]} />)

    fireEvent.change(screen.getByLabelText('約定価格'), { target: { value: '3300' } })
    fireEvent.change(screen.getByLabelText('承認済み追加資金（任意）'), { target: { value: 'wife-sbi-salary' } })
    fireEvent.click(screen.getByRole('button', { name: '記録する' }))

    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1))
    const payload = JSON.parse(apiFetchMock.mock.calls[0][1].body)
    expect(payload.contribution_id).toBe('wife-sbi-salary')
    expect(payload.execution_owner).toBe('wife')
    expect(payload.execution_broker).toBe('sbi')
  })

  it('treats historical backlog as cancellation-first and removes ordered', () => {
    render(<ExecutionForm row={row} onClose={vi.fn()} historical />)
    const status = screen.getByLabelText('状態')
    expect(status).toHaveValue('cancelled')
    expect(screen.queryByRole('option', { name: '発注のみ（未約定）' })).not.toBeInTheDocument()
  })
})

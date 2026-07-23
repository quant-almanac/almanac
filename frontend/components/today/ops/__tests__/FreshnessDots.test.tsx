import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import FreshnessDots from '../FreshnessDots'
import { OPS } from '../tokens'

const health = {
  sources: {
    guard: { exists: true, age_hours: 1, stale_after_hours: 12, stale: false },
    ai_analysis: { exists: true, age_hours: 20, stale_after_hours: 8, stale: true },
    vix: { exists: false, age_hours: null, stale_after_hours: 12, stale: true },
  },
}

describe('FreshnessDots', () => {
  it('uses existing status colors for ok, stale, and missing sources', () => {
    render(<FreshnessDots health={health} />)

    expect(screen.getByTitle('ガード: ok')).toHaveStyle({ color: OPS.green })
    expect(screen.getByTitle('統合分析: stale')).toHaveStyle({ color: OPS.amber })
    expect(screen.getByTitle('VIX: missing')).toHaveStyle({ color: OPS.vermilion })
  })

  it('opens the dialog and returns focus to its trigger on Escape', () => {
    render(<FreshnessDots health={health} />)

    const trigger = screen.getByRole('button', { name: 'データ鮮度の詳細' })
    fireEvent.click(trigger)
    expect(screen.getByRole('dialog', { name: 'データ鮮度の詳細' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'データ鮮度の詳細' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })
})

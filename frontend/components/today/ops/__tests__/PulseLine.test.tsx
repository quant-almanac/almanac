import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import PulseLine from '../PulseLine'

describe('PulseLine', () => {
  it('turns the pulse into readable market telemetry', () => {
    render(<PulseLine command={{ scenario: 'BULL', stance: 'moderately_aggressive', vix: 15.8, guard: { new_entry_allowed: true, trading_allowed: true, alerts: [], daily_pnl_pct: 0.0019 } }} />)

    expect(screen.getByText('市場の鼓動')).toBeInTheDocument()
    expect(screen.getByText('15.8')).toBeInTheDocument()
    expect(screen.getByText('BULL')).toBeInTheDocument()
    expect(screen.getByText('やや攻め')).toBeInTheDocument()
    expect(screen.getByText('OPEN · +0.19%')).toBeInTheDocument()
  })
})

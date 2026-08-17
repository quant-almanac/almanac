import { describe, expect, it } from 'vitest'

import { solarTerm } from '../ornament'

describe('solarTerm', () => {
  it('returns the term that has already begun, not the next one', () => {
    // 立秋は8/8開始。8/9はその2日目
    const term = solarTerm(new Date(2026, 7, 9))
    expect(term.name).toBe('立秋')
    expect(term.dayIndex).toBe(2)
  })

  it('counts the first day of a term as day 1', () => {
    expect(solarTerm(new Date(2026, 7, 8))).toMatchObject({ name: '立秋', dayIndex: 1 })
    expect(solarTerm(new Date(2026, 1, 4))).toMatchObject({ name: '立春', dayIndex: 1 })
  })

  it('falls back to the previous year の冬至 before 小寒', () => {
    // 1/1 はまだ小寒(1/6)前。前年12/22の冬至が続いている
    const term = solarTerm(new Date(2026, 0, 1))
    expect(term.name).toBe('冬至')
    // 2025-12-22 から 2026-01-01 は10日後 → 11日目
    expect(term.dayIndex).toBe(11)
  })

  it('advances to 小寒 on its start date', () => {
    expect(solarTerm(new Date(2026, 0, 6))).toMatchObject({ name: '小寒', dayIndex: 1 })
  })

  it('uses the last term of the year for late December', () => {
    expect(solarTerm(new Date(2026, 11, 31))).toMatchObject({ name: '冬至' })
  })

  it('always reports a positive day index', () => {
    for (let month = 0; month < 12; month += 1) {
      for (const day of [1, 7, 15, 22, 28]) {
        const term = solarTerm(new Date(2026, month, day))
        expect(term.dayIndex).toBeGreaterThan(0)
        expect(term.name).toBeTruthy()
      }
    }
  })
})

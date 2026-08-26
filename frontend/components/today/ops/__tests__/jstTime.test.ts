import { describe, expect, it } from 'vitest'

import { jstHHMM, jstMinutesOfDay, jstMonthDay } from '../jstTime'

// AlmanacStrip.tsx と StatusLine.tsx の両方がこのモジュールへ委譲する。
// getUTCHours()/getUTCMinutes() ベースなのでホストのタイムゾーン設定に
// 依存しない — この前提を直接固定する。
describe('jstTime helpers', () => {
  it('reads JST regardless of what host-local getHours() would return', () => {
    const at2315JST = new Date('2026-07-15T23:15:00+09:00')
    expect(jstHHMM(at2315JST)).toBe('23:15')
    expect(jstMinutesOfDay(at2315JST)).toBe(23 * 60 + 15)
  })

  it('handles the JST midnight rollover correctly', () => {
    const at0005JST = new Date('2026-07-16T00:05:00+09:00')
    expect(jstHHMM(at0005JST)).toBe('00:05')
    expect(jstMinutesOfDay(at0005JST)).toBe(5)
  })

  it('derives the JST calendar date, which can differ from a UTC date', () => {
    // 2026-07-15T16:00:00Z = 2026-07-16 01:00 JST — UTC の日付とは違う日。
    const crossesDateLine = new Date('2026-07-15T16:00:00Z')
    expect(jstMonthDay(crossesDateLine)).toEqual({ month: 7, date: 16 })
  })
})

import { describe, expect, it } from 'vitest'

import { marketState } from '../StatusLine'
import type { AlmanacSession } from '../types'

// ⚠️ AlmanacStrip.tsx と全く同じバグがここにもあった: 「JST」前提の
// セッション判定を Date.getHours()/getMinutes() (ホストのローカル
// タイムゾーン) で計算していた。共通の jstTime.ts ヘルパーへ切り出した後、
// このテストは marketState が実際にそれを使っていることを直接確認する。
describe('marketState', () => {
  const usRegular: AlmanacSession = {
    label: '米国通常', market: 'US', phase: 'regular',
    start: '22:30', end: '05:00', is_open_day: true,
  }

  it('detects a cross-midnight US session using JST, not host-local time', () => {
    // 23:15 JST は米国通常取引時間 (22:30–05:00 JST, 日またぎ) の中。
    // host-local な実装だと、ホストのタイムゾーン設定次第でこの判定が変わる。
    const at2315JST = new Date('2026-07-15T23:15:00+09:00')
    const state = marketState([usRegular], 'US', '米国', at2315JST)
    expect(state.status).toBe('OPEN')
  })

  it('does not report the session as open just before it starts (JST)', () => {
    // 22:15 JST はまだ開始前。
    const at2215JST = new Date('2026-07-15T22:15:00+09:00')
    const state = marketState([usRegular], 'US', '米国', at2215JST)
    expect(state.status).not.toBe('OPEN')
  })
})

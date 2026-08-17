import { describe, expect, it } from 'vitest'
import {
  BPM_MAX, BPM_MIN, beatSeconds, bpmFor, computeVitals, ecgCyclePath,
  ecgTileDataUri, flatlinePath, marketRiskScore, ownRiskScore,
} from '../pulseVitals'

describe('marketRiskScore', () => {
  it('anchors calm/normal/stress/panic VIX levels', () => {
    expect(marketRiskScore(12)).toBe(0)
    expect(marketRiskScore(20)).toBe(35)
    expect(marketRiskScore(30)).toBe(70)
    expect(marketRiskScore(45)).toBe(100)
  })
  it('interpolates between anchors', () => {
    // 16 は 12(0) と 20(35) の中間 → 17.5
    expect(marketRiskScore(16)).toBeCloseTo(17.5, 1)
  })
  it('clamps outside the anchor range instead of going negative or past 100', () => {
    expect(marketRiskScore(5)).toBe(0)
    expect(marketRiskScore(90)).toBe(100)
  })
  it('returns null (not 0) when VIX is unknown — 未知と平穏を混同しない', () => {
    expect(marketRiskScore(null)).toBeNull()
    expect(marketRiskScore(undefined)).toBeNull()
    expect(marketRiskScore(NaN)).toBeNull()
  })
})

describe('ownRiskScore', () => {
  it('is zero when flat and the guard is fully open', () => {
    expect(ownRiskScore({ monthly_pnl_pct: 0, new_entry_allowed: true, trading_allowed: true, alerts: [] })).toBe(0)
  })
  it('scales with monthly drawdown up to the stage-1 stop level', () => {
    expect(ownRiskScore({ monthly_pnl_pct: -0.05, alerts: [] })).toBe(50)
    expect(ownRiskScore({ monthly_pnl_pct: -0.10, alerts: [] })).toBe(100)
  })
  it('treats gains as zero risk, never negative', () => {
    expect(ownRiskScore({ monthly_pnl_pct: 0.08, alerts: [] })).toBe(0)
  })
  it('adds penalties when the guard actually tightens', () => {
    const blockedEntry = ownRiskScore({ monthly_pnl_pct: 0, new_entry_allowed: false, alerts: [] })
    const blockedTrading = ownRiskScore({ monthly_pnl_pct: 0, trading_allowed: false, alerts: [] })
    expect(blockedEntry).toBe(25)
    expect(blockedTrading).toBe(45)
    // 停止のほうが新規禁止より重い
    expect(blockedTrading).toBeGreaterThan(blockedEntry as number)
  })
  it('caps alert contribution so a noisy alert list cannot dominate', () => {
    const many = ownRiskScore({ monthly_pnl_pct: 0, alerts: [1, 2, 3, 4, 5, 6, 7, 8] })
    expect(many).toBe(24)
  })
  it('clamps the combined result at 100', () => {
    expect(ownRiskScore({
      monthly_pnl_pct: -0.30, new_entry_allowed: false, trading_allowed: false, alerts: [1, 2, 3],
    })).toBe(100)
  })
  it('returns null when there is no guard information at all', () => {
    expect(ownRiskScore(null)).toBeNull()
    expect(ownRiskScore({})).toBeNull()
  })
})

describe('computeVitals', () => {
  it('weights market and own risk into one score', () => {
    const v = computeVitals(100, 0)
    expect(v?.score).toBeCloseTo(55, 1)
    const w = computeVitals(0, 100)
    expect(w?.score).toBeCloseTo(45, 1)
  })
  it('names the dominant side so a composite score is not opaque', () => {
    expect(computeVitals(100, 0)?.driver).toBe('market')
    expect(computeVitals(0, 100)?.driver).toBe('own')
    expect(computeVitals(50, 50)?.driver).toBe('balanced')
  })
  it('falls back to whichever side is known', () => {
    expect(computeVitals(80, null)?.score).toBe(80)
    expect(computeVitals(null, 80)?.score).toBe(80)
    expect(computeVitals(80, null)?.driver).toBe('market')
    expect(computeVitals(null, 80)?.driver).toBe('own')
  })
  it('returns null when nothing is known — フラットラインで「不明」を示すため', () => {
    expect(computeVitals(null, null)).toBeNull()
  })
  it('labels the state by band', () => {
    expect(computeVitals(0, 0)?.state).toBe('平静')
    expect(computeVitals(60, 60)?.state).toBe('緊張')
    expect(computeVitals(100, 100)?.state).toBe('警戒')
  })
  it('beats faster as risk rises — これが「鼓動」の核', () => {
    const calm = computeVitals(0, 0)!
    const panic = computeVitals(100, 100)!
    expect(calm.bpm).toBe(BPM_MIN)
    expect(panic.bpm).toBe(BPM_MAX)
    expect(panic.bpm).toBeGreaterThan(calm.bpm)
  })
})

describe('bpmFor / beatSeconds', () => {
  it('maps the score range onto the bpm range', () => {
    expect(bpmFor(0)).toBe(BPM_MIN)
    expect(bpmFor(100)).toBe(BPM_MAX)
    expect(bpmFor(50)).toBeCloseTo((BPM_MIN + BPM_MAX) / 2, 5)
  })
  it('clamps out-of-range scores', () => {
    expect(bpmFor(-20)).toBe(BPM_MIN)
    expect(bpmFor(500)).toBe(BPM_MAX)
  })
  it('converts bpm to seconds per beat', () => {
    expect(beatSeconds(60)).toBe(1)
    expect(beatSeconds(120)).toBe(0.5)
    // 速いほど1拍が短い = アニメーションが速くなる
    expect(beatSeconds(BPM_MAX)).toBeLessThan(beatSeconds(BPM_MIN))
  })
})

describe('ecgCyclePath', () => {
  it('starts and ends on the baseline so tiles join seamlessly', () => {
    const d = ecgCyclePath(100, 40)
    expect(d.startsWith('M0,20')).toBe(true)
    expect(d.endsWith('L100,20')).toBe(true)
  })
  it('contains a tall R spike above the baseline', () => {
    const d = ecgCyclePath(100, 40)
    // amplitude 0.78 → R peak は基線20から約15.6上 = y≈4.4
    expect(d).toMatch(/L39,4\.4/)
  })
  it('flatline has no spike at all', () => {
    expect(flatlinePath(100, 40)).toBe('M0,20 L100,20')
  })
})

describe('ecgTileDataUri', () => {
  it('produces an inline svg data uri carrying the tone colour', () => {
    const uri = ecgTileDataUri({ width: 100, height: 40, color: '#5FD3A0' })
    expect(uri.startsWith('url("data:image/svg+xml,')).toBe(true)
    expect(decodeURIComponent(uri)).toContain('#5FD3A0')
  })
  it('uses the flat path when asked, so unknown state cannot look like a healthy beat', () => {
    const flat = decodeURIComponent(ecgTileDataUri({ width: 100, height: 40, color: '#fff', flat: true }))
    expect(flat).toContain('M0,20 L100,20')
    expect(flat).not.toContain('L39,')
  })
})

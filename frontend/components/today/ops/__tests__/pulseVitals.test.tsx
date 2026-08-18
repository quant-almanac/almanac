import { describe, expect, it } from 'vitest'
import {
  BPM_MAX, BPM_MIN, beatSeconds, bpmFor, computeVitals, ecgCyclePath, ecgTileDataUri, flatlinePath, marketRiskScore, ownRiskScore, realizedVolAnnualized,
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

describe('realizedVolAnnualized', () => {
  it('returns null on a sample too small to estimate from', () => {
    expect(realizedVolAnnualized([{ close: 100 }, { close: 101 }])).toBeNull()
    expect(realizedVolAnnualized([])).toBeNull()
    expect(realizedVolAnnualized(null)).toBeNull()
  })

  it('reports a flat series as zero volatility', () => {
    const flat = Array.from({ length: 12 }, () => ({ close: 100 }))
    expect(realizedVolAnnualized(flat)).toBe(0)
  })

  it('scores a choppy series above a calm one', () => {
    const calm = Array.from({ length: 21 }, (_, i) => ({ close: 100 + i * 0.05 }))
    const choppy = Array.from({ length: 21 }, (_, i) => ({ close: 100 + (i % 2 ? 4 : -4) }))
    expect(realizedVolAnnualized(choppy)!).toBeGreaterThan(realizedVolAnnualized(calm)!)
  })

  it('ignores malformed points instead of producing NaN', () => {
    const rows = Array.from({ length: 12 }, () => ({ close: 100 }))
    const dirty = [...rows, { close: null }, { close: 0 }, {}] as Array<{ close?: number | null }>
    expect(realizedVolAnnualized(dirty)).toBe(0)
  })
})

describe('marketRiskScore with Japan', () => {
  // 日経の1mo系列から実現ボラを出し、VIXと同じアンカーで採点して混ぜる。
  const vixOnly = marketRiskScore(20)

  it('keeps scoring on VIX alone when Japan data is absent', () => {
    expect(marketRiskScore(20, { japanVol: null, japanWeight: 0.3 })).toBe(vixOnly)
    expect(marketRiskScore(20)).toBe(vixOnly)
  })

  it('pulls the score toward Japan when Japanese volatility is higher', () => {
    const blended = marketRiskScore(20, { japanVol: 40, japanWeight: 0.3 })
    expect(blended!).toBeGreaterThan(vixOnly!)
  })

  it('weights by how much of the portfolio is actually Japanese', () => {
    const light = marketRiskScore(20, { japanVol: 40, japanWeight: 0.1 })
    const heavy = marketRiskScore(20, { japanVol: 40, japanWeight: 0.6 })
    expect(heavy!).toBeGreaterThan(light!)
  })

  it('does not treat a missing weight as zero Japanese exposure', () => {
    // 重み不明を0にすると、日本株の荒れが黙って消える。
    const unknown = marketRiskScore(20, { japanVol: 40 })
    expect(unknown!).toBeGreaterThan(vixOnly!)
  })

  it('scores on Japan alone when VIX is unavailable', () => {
    expect(marketRiskScore(null, { japanVol: 40, japanWeight: 0.3 })).not.toBeNull()
  })

  it('returns null when neither market can be measured', () => {
    expect(marketRiskScore(null, { japanVol: null })).toBeNull()
  })

  it('clamps an out-of-range weight rather than extrapolating', () => {
    expect(marketRiskScore(20, { japanVol: 40, japanWeight: 5 }))
      .toBe(marketRiskScore(20, { japanVol: 40, japanWeight: 1 }))
  })
})

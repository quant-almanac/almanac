import { describe, expect, it } from 'vitest'
import { heatBackground } from '../AlmanacStrip'

/** rgba(...) から alpha を取り出す。 */
function alphaOf(css: string | undefined): number {
  const m = css?.match(/rgba\([^)]*,\s*([\d.]+)\)/)
  return m ? Number(m[1]) : NaN
}

describe('heatBackground（相場暦のヒートマップ）', () => {
  it('colours gains green and losses red', () => {
    expect(heatBackground(50_000, 100_000)).toContain('rgba(95, 211, 160')
    expect(heatBackground(-50_000, 100_000)).toContain('rgba(240, 101, 90')
  })

  it('gets darker as the day gets bigger', () => {
    const small = alphaOf(heatBackground(10_000, 100_000))
    const big = alphaOf(heatBackground(100_000, 100_000))
    expect(big).toBeGreaterThan(small)
  })

  it('keeps a visible floor so small days are still tinted', () => {
    // 1円の日でも「色が付いている」ことが分かる下限を持つ
    expect(alphaOf(heatBackground(1, 1_000_000))).toBeGreaterThan(0.05)
  })

  it('treats a magnitude at or beyond the scale as full intensity, never overshooting', () => {
    const atScale = alphaOf(heatBackground(100_000, 100_000))
    const beyond = alphaOf(heatBackground(500_000, 100_000))
    expect(beyond).toBe(atScale)
    expect(beyond).toBeLessThanOrEqual(0.4)
  })

  it('leaves unknown and break-even days uncoloured — 未取得を「無風」と混同しない', () => {
    expect(heatBackground(null, 100_000)).toBeUndefined()
    expect(heatBackground(undefined, 100_000)).toBeUndefined()
    expect(heatBackground(0, 100_000)).toBeUndefined()
    expect(heatBackground(NaN, 100_000)).toBeUndefined()
  })

  it('does not divide by zero when nothing moved all period', () => {
    expect(() => heatBackground(1000, 0)).not.toThrow()
    expect(alphaOf(heatBackground(1000, 0))).toBeLessThanOrEqual(0.4)
  })
})

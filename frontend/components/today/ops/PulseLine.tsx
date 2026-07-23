'use client'

import { useMemo } from 'react'
import { OPS, STANCE_LABEL } from './tokens'
import type { Command } from './types'

/**
 * Market Pulse — 装飾だけだった固定波形を、現在の市場状態を読めるテレメトリ帯へ昇格。
 * 波形の振幅・速度は VIX に連動し、数値・レジーム・スタンス・ガードを同じ場所で示す。
 */

const H = 54
const MID = H / 2
const TILE_W = 720
const CYCLES = 3
const N = 180

function buildTile(offsetX: number, amp: number, isFirst: boolean): string {
  const pts: string[] = []
  for (let i = 0; i <= N; i++) {
    const x = (i / N) * TILE_W + offsetX
    const t = (i / N) * Math.PI * 2 * CYCLES
    const y = MID + Math.sin(t) * amp + Math.sin(t * 2.25) * amp * 0.2
    pts.push(`${i === 0 ? (isFirst ? 'M' : 'L') : 'L'}${x.toFixed(1)},${y.toFixed(2)}`)
  }
  return pts.join('')
}

function params(vix: number): { duration: number; amplitude: number; mood: string; color: string; note: string } {
  if (vix < 18) return { duration: 8.4, amplitude: 5, mood: '安定', color: OPS.green, note: '変動性は低位。通常の判断速度を維持。' }
  if (vix <= 28) return { duration: 6.2, amplitude: 9, mood: '緊張', color: OPS.amber, note: '変動性が上昇。指値とサイズを慎重に確認。' }
  return { duration: 4.2, amplitude: 14, mood: '警戒', color: OPS.vermilion, note: '高変動域。ガードと損失上限を優先。' }
}

export default function PulseLine({ command, vix: preciseVix }: { command?: Command; vix?: number }) {
  const vix = preciseVix ?? command?.vix ?? 18
  const p = params(vix)
  const stance = command?.stance ? STANCE_LABEL[command.stance] ?? command.stance : '—'
  const guard = command?.guard
  const guardOk = Boolean(guard) && guard?.new_entry_allowed !== false && guard?.trading_allowed !== false && (guard?.alerts.length ?? 0) === 0
  const dailyPct = guard?.daily_pnl_pct != null ? guard.daily_pnl_pct * 100 : null
  const path = useMemo(
    () => buildTile(0, p.amplitude, true) + buildTile(TILE_W, p.amplitude, false),
    [p.amplitude],
  )

  const css = `
    .market-pulse { display:grid; grid-template-columns:minmax(250px,.8fr) minmax(300px,1.4fr) auto; gap:18px; align-items:center; }
    .market-pulse-wave { overflow:hidden; min-width:0; height:${H}px; mask-image:linear-gradient(90deg,transparent,black 8%,black 92%,transparent); }
    .market-pulse-path { animation:marketPulseScroll ${p.duration}s linear infinite; will-change:transform; }
    .market-pulse-stats { display:grid; grid-template-columns:repeat(4,auto); gap:16px; align-items:center; }
    @keyframes marketPulseScroll { from { transform:translateX(0); } to { transform:translateX(-${TILE_W}px); } }
    @container ops-content (max-width:900px) { .market-pulse { grid-template-columns:1fr; gap:10px; } .market-pulse-stats { grid-template-columns:repeat(4,minmax(0,1fr)); } }
    @container ops-content (max-width:520px) { .market-pulse-stats { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    @media (prefers-reduced-motion:reduce) { .market-pulse-path { animation:none; } }
  `

  return (
    <section
      aria-label="市場の鼓動"
      style={{
        background: OPS.panel,
        border: `1px solid ${OPS.border}`,
        borderLeft: `3px solid ${p.color}`,
        borderRadius: 10,
        padding: '12px 16px',
        overflow: 'hidden',
      }}
    >
      <style dangerouslySetInnerHTML={{ __html: css }} />
      <div className="market-pulse">
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 9, flexWrap: 'wrap' }}>
            <span style={{ fontFamily: OPS.mono, fontSize: 13, fontWeight: 700, color: OPS.gold, letterSpacing: '0.14em' }}>MARKET PULSE</span>
            <span style={{ fontSize: 13, color: OPS.text, fontWeight: 600 }}>市場の鼓動</span>
            <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, fontSize: 12.5, color: p.color }}>● {p.mood}</span>
          </div>
          <p style={{ color: OPS.sub, fontSize: 13, lineHeight: 1.55, margin: '6px 0 0' }}>{p.note}</p>
        </div>

        <div className="market-pulse-wave" aria-hidden>
          <svg width={TILE_W * 2} height={H} className="market-pulse-path" style={{ display: 'block' }}>
            <line x1="0" x2={TILE_W * 2} y1={MID} y2={MID} stroke={OPS.hairline} strokeWidth="1" strokeDasharray="3 5" />
            <path d={path} stroke={p.color} strokeWidth="1.8" fill="none" opacity="0.85" />
          </svg>
        </div>

        <div className="market-pulse-stats">
          <PulseStat label="VIX" value={vix.toFixed(1)} color={p.color} />
          <PulseStat label="REGIME" value={command?.scenario ?? '—'} color={command?.scenario === 'BULL' ? OPS.green : OPS.sub} />
          <PulseStat label="STANCE" value={stance} color={OPS.gold} />
          <PulseStat
            label="GUARD / DAY"
            value={`${guardOk ? 'OPEN' : 'BLOCK'}${dailyPct != null ? ` · ${dailyPct >= 0 ? '+' : ''}${dailyPct.toFixed(2)}%` : ''}`}
            color={guardOk ? OPS.green : OPS.vermilion}
          />
        </div>
      </div>
    </section>
  )
}

function PulseStat({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5, letterSpacing: '0.08em', whiteSpace: 'nowrap' }}>{label}</div>
      <div style={{ color, fontFamily: OPS.mono, fontSize: 13.5, fontWeight: 700, marginTop: 3, whiteSpace: 'nowrap' }}>{value}</div>
    </div>
  )
}

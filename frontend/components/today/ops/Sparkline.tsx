'use client'
import { OPS } from './tokens'

/**
 * スパークライン — 60日終値 + 指値ライン。アクションカード内蔵の小型チャート。
 */
export default function Sparkline({
  series,
  limit,
  height = 40,
}: {
  series: { d: string; c: number }[]
  limit?: number | null
  height?: number
}) {
  if (!series || series.length < 2) return null

  const W = 300
  const H = height
  const PAD = 3
  const vals = series.map(p => p.c)
  let min = Math.min(...vals, ...(limit != null ? [limit] : []))
  let max = Math.max(...vals, ...(limit != null ? [limit] : []))
  if (max === min) {
    max += 1
    min -= 1
  }
  const range = max - min
  min -= range * 0.06
  max += range * 0.06

  const toX = (i: number) => PAD + (i / (series.length - 1)) * (W - PAD * 2)
  const toY = (v: number) => PAD + (1 - (v - min) / (max - min)) * (H - PAD * 2)

  const path = vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join('')
  const last = vals[vals.length - 1]
  const first = vals[0]
  const up = last >= first

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      style={{ width: '100%', height, display: 'block' }}
      preserveAspectRatio="none"
      aria-hidden
    >
      {/* 指値ライン */}
      {limit != null && (
        <>
          <line
            x1={PAD}
            y1={toY(limit)}
            x2={W - PAD}
            y2={toY(limit)}
            stroke={OPS.gold}
            strokeWidth={1}
            strokeDasharray="4 3"
            opacity={0.75}
          />
          <text
            x={W - PAD}
            y={toY(limit) - 3}
            fontSize={9}
            fill={OPS.gold}
            textAnchor="end"
            fontFamily={OPS.mono}
          >
            {limit}
          </text>
        </>
      )}
      {/* 価格ライン */}
      <path d={path} stroke={up ? OPS.green : OPS.redSoft} strokeWidth={1.4} fill="none" opacity={0.9} />
      {/* 終値ドット */}
      <circle cx={toX(vals.length - 1)} cy={toY(last)} r={2.4} fill={up ? OPS.green : OPS.redSoft} />
    </svg>
  )
}

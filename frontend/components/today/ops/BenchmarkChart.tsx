'use client'
import { OPS, fmtJpy } from './tokens'
import type { BenchmarkData } from './types'

const W = 460
const H = 190
const PAD = { l: 8, r: 52, t: 12, b: 20 }

/**
 * ベンチマーク比較 — 入出金調整済み TWR vs S&P500 vs 日経平均（同一起点のリターン%）。
 * 勝ち負けを outperf バッジで即答する。
 */
export default function BenchmarkChart({ data }: { data: BenchmarkData }) {
  const n = data.dates.length
  if (n < 2) return null

  const seriesDefs = [
    { key: 'portfolio' as const, label: 'Portfolio TWR', color: OPS.gold, width: 2.2 },
    { key: 'sp500' as const, label: 'S&P500（円換算）', color: OPS.blue, width: 1.4 },
    { key: 'nikkei' as const, label: '日経平均', color: OPS.redSoft, width: 1.4 },
  ].filter(s => Array.isArray(data[s.key]))

  const all: number[] = []
  for (const s of seriesDefs) for (const v of data[s.key] as (number | null)[]) if (v != null) all.push(v)
  let min = Math.min(...all, 0)
  let max = Math.max(...all, 0)
  const range = max - min || 1
  min -= range * 0.08
  max += range * 0.08

  const toX = (i: number) => PAD.l + (i / (n - 1)) * (W - PAD.l - PAD.r)
  const toY = (v: number) => PAD.t + (1 - (v - min) / (max - min)) * (H - PAD.t - PAD.b)

  const pathOf = (vals: (number | null)[]) => {
    let d = ''
    let started = false
    vals.forEach((v, i) => {
      if (v == null) return
      d += `${started ? 'L' : 'M'}${toX(i).toFixed(1)},${toY(v).toFixed(1)}`
      started = true
    })
    return d
  }

  const lastOf = (vals: (number | null)[]) => {
    for (let i = vals.length - 1; i >= 0; i--) if (vals[i] != null) return vals[i] as number
    return null
  }

  const endpointLabelY = (v: number) => {
    const raw = toY(v)
    const zero = toY(0)
    if (Math.abs(raw - zero) >= 12) return raw
    const shifted = raw + (v < 0 ? 14 : -12)
    return Math.max(PAD.t + 6, Math.min(H - PAD.b - 6, shifted))
  }

  return (
    <div
      className="ops-card"
      style={{ background: OPS.panel, border: `1px solid ${OPS.border}`, borderRadius: 10, padding: '14px 16px' }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 6, flexWrap: 'wrap' }}>
        <span style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.gold, letterSpacing: '0.14em', fontWeight: 600 }}>
          TWR VS BENCHMARK
        </span>
        <span style={{ fontSize: 11, color: OPS.dim }}>
          同一起点 0% · {data.dates[0]} → {data.dates[n - 1]}
        </span>
        <StatusBadge confirmed={data.confirmed} />
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
          {data.outperf.sp500 != null && <OutperfBadge label="S&P500円" v={data.outperf.sp500} />}
          {data.outperf.nikkei != null && <OutperfBadge label="日経" v={data.outperf.nikkei} />}
        </span>
      </div>

      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}
        aria-label="入出金調整済みTWRとベンチマークの比較チャート">
        <line x1={PAD.l} y1={toY(0)} x2={W - PAD.r} y2={toY(0)} stroke={OPS.border} strokeWidth={1} strokeDasharray="3 3" />
        <text x={W - PAD.r + 4} y={toY(0) + 4} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono}>0%</text>

        {seriesDefs.map(s => {
          const vals = data[s.key] as (number | null)[]
          const last = lastOf(vals)
          const rawY = last != null ? toY(last) : null
          const labelY = last != null ? endpointLabelY(last) : null
          return (
            <g key={s.key}>
              <path d={pathOf(vals)} stroke={s.color} strokeWidth={s.width} fill="none"
                opacity={s.key === 'portfolio' ? 1 : 0.8} />
              {last != null && rawY != null && labelY != null && (
                <>
                  {rawY !== labelY && (
                    <line x1={W - PAD.r} y1={rawY} x2={W - PAD.r + 3} y2={labelY}
                      stroke={s.color} strokeWidth={0.8} opacity={0.7} />
                  )}
                  <text x={W - PAD.r + 4} y={labelY + 4} fontSize={10.5} fill={s.color} fontFamily={OPS.mono}>
                    {last >= 0 ? '+' : ''}{last.toFixed(1)}%
                  </text>
                </>
              )}
            </g>
          )
        })}

        <text x={toX(0)} y={H - 4} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono}>{data.dates[0]}</text>
        <text x={toX(n - 1)} y={H - 4} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono} textAnchor="end">
          {data.dates[n - 1]}
        </text>
      </svg>

      <div style={{ display: 'flex', gap: 16, marginTop: 6, fontSize: 11, fontFamily: OPS.mono }}>
        {seriesDefs.map(s => (
          <span key={s.key} style={{ color: s.color }}>
            ─ {s.label}
          </span>
        ))}
      </div>
      <div style={{ marginTop: 7, fontSize: 10.5, color: OPS.dim, lineHeight: 1.6 }}>
        Modified Dietz · 入出金調整済み
        {data.period_days_actual != null && ` · 実測 ${data.period_days_actual}日`}
        {data.net_cash_flow != null && data.net_cash_flow !== 0 && ` · 純入出金 ${fmtJpy(data.net_cash_flow)}`}
        <br />
        S&P500は為替込みの円換算 · ベンチマークは配当を含まない価格騰落率
      </div>
    </div>
  )
}

function StatusBadge({ confirmed }: { confirmed: boolean }) {
  const color = confirmed ? OPS.green : OPS.amber
  const bg = confirmed ? OPS.greenBg : OPS.amberBg
  return (
    <span
      title={confirmed ? 'クリーン期間と最低実測日数を満たしたTWR' : '最低実測日数に達する前の参考値'}
      style={{
        fontFamily: OPS.mono,
        fontSize: 10.5,
        fontWeight: 600,
        color,
        background: bg,
        border: `1px solid ${color}44`,
        borderRadius: 4,
        padding: '1px 6px',
      }}
    >
      {confirmed ? '確定' : '暫定'}
    </span>
  )
}

function OutperfBadge({ label, v }: { label: string; v: number }) {
  const win = v >= 0
  return (
    <span
      style={{
        fontFamily: OPS.mono,
        fontSize: 11.5,
        fontWeight: 600,
        color: win ? OPS.green : OPS.redSoft,
        background: win ? OPS.greenBg : OPS.vermilionBg,
        border: `1px solid ${win ? OPS.green : OPS.redSoft}44`,
        borderRadius: 4,
        padding: '2px 8px',
      }}
    >
      vs {label} {v >= 0 ? '+' : ''}
      {v.toFixed(2)}pt {win ? '勝ち' : '負け'}
    </span>
  )
}

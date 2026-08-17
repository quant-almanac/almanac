'use client'

import { useMemo, useState } from 'react'
import { OPS, fmtJpy } from './tokens'
import { computePerformanceStats, type PnlPoint } from './performanceStats'
import type { BenchmarkData } from './types'

/**
 * 成績チャート — 「腕前(%)」と「金額(円)」を1枠にまとめる。
 *
 * 以前は TWR チャートと P&L チャートが別枠で並んでいた。見た目が似ているのに
 * 中身は別物(片方は%でベンチ比較、もう片方は円の累積)で、違いが読めなかった。
 * 同じ枠のタブにして、切り替えた瞬間に「何が違うのか」を1行で明示する。
 *
 * どちらも入出金は差し引き済み。積立¥20万/月が成績に混ざらない。
 */

const W = 520
const H = 200
const PAD = { l: 8, r: 56, t: 12, b: 20 }

type Tab = 'skill' | 'amount'

export default function PerformanceChart({
  benchmark, pnl,
}: {
  benchmark?: BenchmarkData | null
  pnl?: PnlPoint[]
}) {
  const hasBench = !!benchmark && benchmark.dates.length >= 2
  const series = useMemo(() => (pnl ?? []).filter(p => p && Number.isFinite(p.v)), [pnl])
  const hasPnl = series.length >= 2
  const [tab, setTab] = useState<Tab>(hasBench ? 'skill' : 'amount')
  const stats = useMemo(() => computePerformanceStats(series), [series])

  if (!hasBench && !hasPnl) return null
  const active: Tab = tab === 'skill' && !hasBench ? 'amount' : tab === 'amount' && !hasPnl ? 'skill' : tab

  return (
    <section className="perf ops-elev" aria-label="成績チャート">
      <style dangerouslySetInnerHTML={{ __html: `
        .perf { border-radius:10px; padding:12px 15px 13px; background:${OPS.panel}; }
        .perf-head { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
        .perf-tabs { display:flex; gap:5px; }
        .perf-tab { border:1px solid ${OPS.hairline}; border-radius:999px; background:transparent;
          color:${OPS.dim}; padding:2px 11px; font:10px ${OPS.sans}; cursor:pointer; }
        .perf-tab[aria-pressed="true"] { color:${OPS.gold}; border-color:${OPS.gold}88; background:${OPS.goldBg}; font-weight:600; }
        .perf-what { color:${OPS.dim}; font-family:${OPS.sans}; font-size:9.5px; line-height:1.5; margin-left:auto; text-align:right; max-width:340px; }
        .perf-body { display:grid; grid-template-columns:minmax(0,1fr) 168px; gap:14px; align-items:start; margin-top:8px; }
        .perf-stats { display:flex; flex-direction:column; gap:0; }
        .perf-stat { display:flex; align-items:baseline; justify-content:space-between; gap:8px;
          padding:5px 0; border-bottom:1px solid ${OPS.hairline}; }
        .perf-stat:last-child { border-bottom:0; }
        .perf-stat span { color:${OPS.dim}; font-family:${OPS.sans}; font-size:9.5px; }
        .perf-stat b { color:${OPS.text}; font-family:${OPS.mono}; font-size:12px; font-weight:600; }
        .perf-legend { display:flex; gap:14px; margin-top:5px; font-family:${OPS.mono}; font-size:10px; flex-wrap:wrap; }
        .perf-note { margin-top:7px; color:${OPS.dim}; font-size:9.5px; line-height:1.6; }
        @media (max-width:760px) { .perf-body { grid-template-columns:1fr; } .perf-what { max-width:none; text-align:left; margin-left:0; } }
      ` }} />

      <div className="perf-head">
        <strong className="ops-latin" style={{ color: OPS.gold, fontSize: 12.5 }}>PERFORMANCE</strong>
        <span style={{ fontFamily: OPS.display, color: OPS.sub, fontSize: 11.5, letterSpacing: '.06em' }}>成績</span>
        <span className="perf-tabs">
          {hasBench && (
            <button type="button" className="perf-tab" aria-pressed={active === 'skill'}
              onClick={() => setTab('skill')}>腕前 %</button>
          )}
          {hasPnl && (
            <button type="button" className="perf-tab" aria-pressed={active === 'amount'}
              onClick={() => setTab('amount')}>金額 円</button>
          )}
        </span>
        <span className="perf-what">
          {active === 'skill'
            ? '市場に対して勝てているか。入出金の影響を除いた率(Modified Dietz)でベンチマークと比べる'
            : '実際にいくら増えたか。積立・入出金を差し引いた売買損益の累積'}
        </span>
      </div>

      <div className="perf-body">
        <div>
          {active === 'skill' && benchmark
            ? <SkillChart data={benchmark} />
            : <AmountChart series={series} />}
        </div>

        <div className="perf-stats">
          {active === 'skill' && benchmark ? (
            <>
              <Stat label="TWR（自分）" value={fmtPct(lastOf(benchmark.portfolio))} tone={toneOf(lastOf(benchmark.portfolio))} />
              <Stat label="S&P500（円）" value={fmtPct(lastOf(benchmark.sp500))} />
              <Stat label="日経平均" value={fmtPct(lastOf(benchmark.nikkei))} />
              <Stat label="対S&P500" value={fmtPt(benchmark.outperf.sp500)} tone={toneOf(benchmark.outperf.sp500)} />
              <Stat label="対日経" value={fmtPt(benchmark.outperf.nikkei)} tone={toneOf(benchmark.outperf.nikkei)} />
              <Stat label="判定" value={benchmark.confirmed ? '確定' : '暫定'}
                tone={benchmark.confirmed ? OPS.green : OPS.amber} />
              {benchmark.period_days_actual != null && (
                <Stat label="実測期間" value={`${benchmark.period_days_actual}日`} />
              )}
            </>
          ) : stats ? (
            <>
              <Stat label="累積損益" value={signedJpy(stats.total)} tone={toneOf(stats.total)} />
              <Stat label="直近5日" value={stats.last5 == null ? '—' : signedJpy(stats.last5)} tone={toneOf(stats.last5)} />
              <Stat label="勝率" value={stats.winRate == null ? '—' : `${stats.winRate}%`} />
              <Stat label="勝ち / 日数" value={`${stats.winDays} / ${stats.totalDays}`} />
              <Stat label="最大の落ち込み" value={`−${fmtJpy(stats.maxDrawdown)}`} tone={OPS.redSoft} />
              <Stat label="最良の日" value={stats.best ? `${stats.best.d} ${signedJpy(stats.best.v)}` : '—'} tone={OPS.green} />
              <Stat label="最悪の日" value={stats.worst ? `${stats.worst.d} ${signedJpy(stats.worst.v)}` : '—'} tone={OPS.redSoft} />
            </>
          ) : null}
        </div>
      </div>

      <div className="perf-note">
        {active === 'skill'
          ? <>Modified Dietz · 入出金調整済み{benchmark?.net_cash_flow ? ` · 純入出金 ${fmtJpy(benchmark.net_cash_flow)}` : ''}<br />S&P500は為替込みの円換算 · ベンチマークは配当を含まない価格騰落率</>
          : <>積立・入出金は event_ledger の cash_flow で差し引き済み。相場で動いた分だけを累積している</>}
      </div>
    </section>
  )
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <span className="perf-stat">
      <span>{label}</span>
      <b style={tone ? { color: tone } : undefined}>{value}</b>
    </span>
  )
}

function lastOf(vals?: (number | null)[]): number | null {
  if (!vals?.length) return null
  for (let i = vals.length - 1; i >= 0; i -= 1) {
    const v = vals[i]
    if (typeof v === 'number' && Number.isFinite(v)) return v
  }
  return null
}
const fmtPct = (v: number | null) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`)
const fmtPt = (v: number | null | undefined) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}pt`)
const signedJpy = (v: number) => (v >= 0 ? `+${fmtJpy(v)}` : `−${fmtJpy(Math.abs(v))}`)
const toneOf = (v: number | null | undefined) =>
  v == null ? undefined : v >= 0 ? OPS.green : OPS.redSoft

/* ── 腕前: TWR vs ベンチマーク ── */
function SkillChart({ data }: { data: BenchmarkData }) {
  const n = data.dates.length
  const defs = [
    { key: 'portfolio' as const, label: '自分 TWR', color: OPS.gold, width: 2.2 },
    { key: 'sp500' as const, label: 'S&P500（円）', color: OPS.blue, width: 1.3 },
    { key: 'nikkei' as const, label: '日経平均', color: OPS.redSoft, width: 1.3 },
  ].filter(s => Array.isArray(data[s.key]))

  const all: number[] = []
  for (const s of defs) for (const v of data[s.key] as (number | null)[]) if (v != null) all.push(v)
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

  return (
    <>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}
        aria-label="入出金調整済みTWRとベンチマークの比較">
        <line x1={PAD.l} y1={toY(0)} x2={W - PAD.r} y2={toY(0)} stroke={OPS.border} strokeWidth={1} strokeDasharray="3 3" />
        <text x={W - PAD.r + 4} y={toY(0) + 4} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono}>0%</text>
        {defs.map(s => {
          const vals = data[s.key] as (number | null)[]
          const last = lastOf(vals)
          return (
            <g key={s.key}>
              <path d={pathOf(vals)} stroke={s.color} strokeWidth={s.width} fill="none"
                opacity={s.key === 'portfolio' ? 1 : 0.75} />
              {last != null && (
                <text x={W - PAD.r + 4} y={toY(last) + 4} fontSize={10.5} fill={s.color} fontFamily={OPS.mono}>
                  {last >= 0 ? '+' : ''}{last.toFixed(1)}%
                </text>
              )}
            </g>
          )
        })}
        <text x={toX(0)} y={H - 4} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono}>{data.dates[0]}</text>
        <text x={toX(n - 1)} y={H - 4} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono} textAnchor="end">{data.dates[n - 1]}</text>
      </svg>
      <div className="perf-legend">
        {defs.map(s => <span key={s.key} style={{ color: s.color }}>─ {s.label}</span>)}
      </div>
    </>
  )
}

/* ── 金額: 累積損益 + 日次バー ── */
function AmountChart({ series }: { series: PnlPoint[] }) {
  if (series.length < 2) return null
  const vals = series.map(p => p.v)
  let min = Math.min(...vals, 0)
  let max = Math.max(...vals, 0)
  const range = max - min || 1
  min -= range * 0.1
  max += range * 0.1

  const BAR_H = 42
  const LINE_H = H - BAR_H - 10
  const toX = (i: number) => PAD.l + (i / (series.length - 1)) * (W - PAD.l - PAD.r)
  const toY = (v: number) => PAD.t + (1 - (v - min) / (max - min)) * (LINE_H - PAD.t - PAD.b)

  const line = vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join('')
  const area = `${line}L${toX(vals.length - 1).toFixed(1)},${toY(0)}L${toX(0).toFixed(1)},${toY(0)}Z`
  const last = vals[vals.length - 1]
  const color = last >= 0 ? OPS.green : OPS.redSoft

  // 日次バーで「どの日が効いたか」を出す。累積線だけだとスカスカになる。
  const deltas = series.slice(1).map((p, i) => ({ d: p.d, v: p.v - series[i].v }))
  const maxAbs = Math.max(1, ...deltas.map(p => Math.abs(p.v)))
  const barW = Math.max(1.5, (W - PAD.l - PAD.r) / Math.max(1, deltas.length) - 1.2)
  const barBase = LINE_H + 8 + BAR_H / 2

  return (
    <>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}
        aria-label="入出金を差し引いた累積損益と日次損益">
        <line x1={PAD.l} y1={toY(0)} x2={W - PAD.r} y2={toY(0)} stroke={OPS.border} strokeWidth={1} strokeDasharray="3 3" />
        <text x={W - PAD.r + 5} y={toY(0) + 3} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono}>¥0</text>
        <path d={area} fill={color} opacity={0.1} />
        <path d={line} stroke={color} strokeWidth={1.7} fill="none" />
        <circle cx={toX(vals.length - 1)} cy={toY(last)} r={2.8} fill={color} />
        <text x={W - PAD.r + 5} y={toY(last) + 4} fontSize={10.5} fill={color} fontFamily={OPS.mono}>
          {last >= 0 ? '+' : ''}{Math.round(last / 10000)}万
        </text>

        <line x1={PAD.l} y1={barBase} x2={W - PAD.r} y2={barBase} stroke={OPS.hairline} strokeWidth={1} />
        {deltas.map((p, i) => {
          const h = (Math.abs(p.v) / maxAbs) * (BAR_H / 2)
          return (
            <rect key={i} x={toX(i + 1) - barW / 2} width={barW}
              y={p.v >= 0 ? barBase - h : barBase} height={Math.max(0.8, h)}
              fill={p.v >= 0 ? OPS.green : OPS.redSoft} opacity={0.62} />
          )
        })}
        <text x={PAD.l} y={barBase + BAR_H / 2 + 12} fontSize={9} fill={OPS.dim} fontFamily={OPS.mono}>日次</text>
        <text x={toX(series.length - 1)} y={H - 3} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono} textAnchor="end">
          {series[series.length - 1].d}
        </text>
      </svg>
      <div className="perf-legend">
        <span style={{ color }}>─ 累積（入出金差し引き済み）</span>
        <span style={{ color: OPS.dim }}>▮ 日次損益</span>
      </div>
    </>
  )
}

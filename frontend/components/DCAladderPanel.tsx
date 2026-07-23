'use client'

import { useEffect, useState } from 'react'
import { OPS } from '@/components/today/ops/tokens'
import { Bar, Chip, Panel, PanelTitle, Stat } from '@/components/today/ops/PageKit'

/**
 * DCA ラダー発動状態パネル
 * /api/dca を 5 分間隔で polling し、active_tranche と条件を可視化する。
 */

interface RecommendedBuy {
  ticker: string
  target_jpy: number
  urgency: string
  tranche: string
  rationale: string
}

interface DCASignals {
  evaluated_at: string | null
  active_tranche: 'T1' | 'T2' | 'T3' | null
  tranche_reasons: string[]
  dd: {
    current_value_jpy: number | null
    peak_value_jpy: number | null
    peak_date: string | null
    dd_from_peak: number | null
    dd_mtd_pct: number | null
  }
  panic: {
    panic_score: number | null
    vix: number | null
    fear_greed: number | null
    put_call: number | null
    hy_oas_bps: number | null
  }
  vix_extract: {
    level: number | null
    classification: string | null
    decay_from_peak_5d_pct: number | null
  }
  breadth: {
    sectors_below_ma20: number
    total: number
    breadth_score: number | null
    broad_selloff: boolean
  }
  volume_capitulation: boolean
  rsi_state: { reversed: boolean; rsi_latest: number | null; trough: number | null }
  evaluations: Record<string, { met: boolean; reasons: string[] }>
  recommended_buys: RecommendedBuy[]
  state: { annual_remaining_pct: number; cooldown_active: Record<string, boolean> }
  note?: string
  error?: string
}

const TRANCHE_COLOR: Record<string, string> = {
  T1: OPS.amber,
  T2: OPS.amber,
  T3: OPS.vermilion,
}

export default function DCAladderPanel() {
  const [data, setData] = useState<DCASignals | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const res = await fetch('/api/dca', { cache: 'no-store' })
        if (!res.ok) throw new Error(`${res.status}`)
        const json = await res.json()
        if (!cancelled) setData(json)
      } catch (e) {
        console.error('[DCA] fetch failed', e)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    const timer = setInterval(load, 5 * 60 * 1000)  // 5分ごと
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  if (loading) {
    return (
      <Panel pad="16px" style={{ color: OPS.sub, fontSize: 14 }}>
        DCA ラダー評価中…
      </Panel>
    )
  }

  if (!data || data.error) {
    return (
      <Panel pad="16px" style={{ color: OPS.sub, fontSize: 14 }}>
        DCA データ未取得 {data?.error ? `(${data.error})` : ''}
      </Panel>
    )
  }

  const active = data.active_tranche
  const borderColor = active ? TRANCHE_COLOR[active] : OPS.border

  const handleRefresh = async () => {
    setLoading(true)
    try {
      await fetch('/api/dca/evaluate', { method: 'POST' })
      const res = await fetch('/api/dca', { cache: 'no-store' })
      setData(await res.json())
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  // 次 tranche への距離（approx）
  const nextTrancheHint = (): string | null => {
    if (active) return null
    const dd = data.dd.dd_from_peak
    if (dd === null) return null
    const dt1 = -0.08 - dd
    if (dd > -0.08 && dt1 < 0) return `T1 まで ${(Math.abs(dt1) * 100).toFixed(1)}% の追加ドローダウン必要`
    const dt2 = -0.12 - dd
    if (dd > -0.12 && dt2 < 0) return `T2 まで ${(Math.abs(dt2) * 100).toFixed(1)}% の追加ドローダウン必要`
    const dt3 = -0.18 - dd
    if (dd > -0.18 && dt3 < 0) return `T3 まで ${(Math.abs(dt3) * 100).toFixed(1)}% の追加ドローダウン必要`
    return '平時（ラダー非発動）'
  }

  return (
    <Panel pad="18px 20px" style={{ borderColor }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <PanelTitle>🩸 底打ち買い下がり (DCA Ladder)</PanelTitle>
          {active && (
            <Chip color={TRANCHE_COLOR[active]} bg={`${TRANCHE_COLOR[active]}22`} mono>{active} Active</Chip>
          )}
        </div>
        <button
          onClick={handleRefresh}
          style={{ fontSize: 12, padding: '4px 8px', borderRadius: 6, background: OPS.panelAlt, border: `1px solid ${OPS.border}`, color: OPS.sub, cursor: 'pointer' }}
        >
          再評価
        </button>
      </div>

      {/* 主要指標グリッド */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 10, marginBottom: 12 }}>
        <Metric label="Portfolio DD" value={data.dd.dd_from_peak !== null ? `${(data.dd.dd_from_peak * 100).toFixed(2)}%` : '—'} critical={data.dd.dd_from_peak !== null && data.dd.dd_from_peak <= -0.08} />
        <Metric label="VIX" value={data.vix_extract.level?.toFixed(1) ?? '—'} sub={data.vix_extract.decay_from_peak_5d_pct !== null ? `5d decay ${data.vix_extract.decay_from_peak_5d_pct.toFixed(1)}%` : undefined} critical={data.vix_extract.level !== null && data.vix_extract.level > 25} />
        <Metric label="Fear & Greed" value={data.panic.fear_greed?.toString() ?? '—'} critical={data.panic.fear_greed !== null && data.panic.fear_greed <= 25} />
        <Metric label="HY OAS" value={data.panic.hy_oas_bps !== null ? `${data.panic.hy_oas_bps.toFixed(0)}bps` : '—'} critical={data.panic.hy_oas_bps !== null && data.panic.hy_oas_bps > 500} />
      </div>

      {/* Panic score bar */}
      {data.panic.panic_score !== null && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', color: OPS.sub, fontSize: 11, marginBottom: 4 }}>
            <span>Panic Score</span><span>{data.panic.panic_score}/100</span>
          </div>
          <Bar pct={data.panic.panic_score} color={data.panic.panic_score > 70 ? OPS.vermilion : data.panic.panic_score > 40 ? OPS.amber : OPS.green} />
        </div>
      )}

      {/* Active tranche details or next-tranche hint */}
      {active ? (
        <div style={{ display: 'grid', gap: 10 }}>
          <div style={{ padding: 12, borderRadius: 8, background: OPS.inset, border: `1px solid ${OPS.border}` }}>
            <div style={{ fontSize: 12, color: OPS.sub, marginBottom: 4 }}>発動根拠</div>
            <ul style={{ fontSize: 12, color: OPS.text, margin: 0, paddingLeft: 0, listStyle: 'none' }}>
              {data.tranche_reasons.map((r, i) => <li key={i}>✓ {r}</li>)}
            </ul>
          </div>

          {data.recommended_buys.length > 0 && (
            <div style={{ padding: 12, borderRadius: 8, background: OPS.inset, border: `1px solid ${OPS.border}` }}>
              <div style={{ fontSize: 12, color: OPS.sub, marginBottom: 8 }}>Recommended buys</div>
              <div style={{ display: 'grid', gap: 4 }}>
                {data.recommended_buys.map((b) => (
                  <div key={b.ticker} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                    <span style={{ color: OPS.text, fontWeight: 600 }}>{b.ticker}</span>
                    <span style={{ color: OPS.sub }}>¥{b.target_jpy.toLocaleString()}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      ) : (
        <div style={{ fontSize: 12, color: OPS.sub, padding: 8, borderRadius: 6, background: OPS.inset, border: `1px solid ${OPS.border}` }}>
          {nextTrancheHint()}
        </div>
      )}

      {/* サブ条件 */}
      <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', gap: 8, fontSize: 10 }}>
        <Tag ok={data.breadth.broad_selloff} label={`Breadth ${data.breadth.sectors_below_ma20}/${data.breadth.total || 11}`} />
        <Tag ok={data.volume_capitulation} label="Volume capitulation" />
        <Tag ok={data.rsi_state.reversed} label={`RSI reversed (${data.rsi_state.rsi_latest ?? '?'})`} />
        <Tag ok={data.state.annual_remaining_pct > 0.05} label={`Budget ${(data.state.annual_remaining_pct * 100).toFixed(1)}% left`} />
      </div>

      {data.evaluated_at && (
        <div style={{ marginTop: 12, fontSize: 10, color: OPS.dim }}>
          評価時刻: {new Date(data.evaluated_at).toLocaleString('ja-JP')}
        </div>
      )}
    </Panel>
  )
}

function Metric({ label, value, sub, critical }: { label: string; value: string; sub?: string; critical?: boolean }) {
  return (
    <Stat label={label} value={value} color={critical ? OPS.vermilion : OPS.text} sub={sub} />
  )
}

function Tag({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span style={{ padding: '2px 8px', borderRadius: 999, border: `1px solid ${ok ? `${OPS.vermilion}66` : OPS.border}`, background: ok ? OPS.vermilionBg : OPS.inset, color: ok ? OPS.redSoft : OPS.dim }}>
      {ok ? '✓ ' : '○ '}
      {label}
    </span>
  )
}

'use client'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS } from './tokens'

interface StrategyData {
  scenario?: string
  scenario_name?: string
  scenario_description?: string
  cash_ratio_target?: number
  long_bias?: boolean
  short_allowed?: boolean
  leverage_allowed?: boolean
  regime?: { spy_above?: boolean; nk_above?: boolean; updated?: string; stale?: boolean }
}

/**
 * シナリオストリップ — Strategy ページの核（シナリオ・レジーム・許可フラグ）を統合。
 */
export default function ScenarioStrip() {
  const { data } = useSWR<StrategyData>('/api/strategy', fetcher, { refreshInterval: 300000 })
  if (!data?.scenario) return null

  const r = data.regime ?? {}
  const flag = (ok: boolean | undefined, on: string, off: string) => (
    <span style={{ color: ok ? OPS.green : OPS.dim }}>{ok ? on : off}</span>
  )

  return (
    <div
      className="ops-card"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        flexWrap: 'wrap',
        background: OPS.panel,
        border: `1px solid ${OPS.border}`,
        borderLeft: `3px solid ${OPS.green}`,
        borderRadius: 10,
        padding: '10px 16px',
        marginBottom: 16,
        fontSize: 12.5,
      }}
    >
      <span style={{ fontFamily: OPS.mono, fontWeight: 600, fontSize: 14, color: OPS.green }}>
        {data.scenario}
      </span>
      <span style={{ color: OPS.text, fontWeight: 500 }}>{data.scenario_name}</span>
      <span style={{ color: OPS.sub, fontSize: 12 }}>{data.scenario_description}</span>
      <span style={{ marginLeft: 'auto', display: 'flex', gap: 14, fontFamily: OPS.mono, fontSize: 11.5 }}>
        <span title="S&P500 が 50日移動平均より上か">
          S&P{'>'}MA50 {flag(r.spy_above, '✓', '✗')}
        </span>
        <span title="日経平均が 50日移動平均より上か">
          日経{'>'}MA50 {flag(r.nk_above, '✓', '✗')}
        </span>
        <span>空売り {flag(data.short_allowed, '許可', '禁止')}</span>
        <span>信用 {flag(data.leverage_allowed, '許可', '禁止')}</span>
        <span style={{ color: OPS.sub }}>現金目標 {data.cash_ratio_target ?? '—'}%</span>
      </span>
    </div>
  )
}

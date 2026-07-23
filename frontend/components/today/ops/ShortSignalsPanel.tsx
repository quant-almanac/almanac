'use client'

import useSWR from 'swr'
import { fetcher, type Signal, type SignalsData } from '@/lib/api'
import { OPS } from './tokens'
import { Chip, Panel, PanelTitle } from './PageKit'

function price(value?: number): string { return value == null ? '—' : value.toLocaleString() }

export default function ShortSignalsPanel() {
  const { data, isLoading } = useSWR<SignalsData>('/api/signals', fetcher, { refreshInterval: 120000 })
  const signals = Object.entries(data?.signals ?? {})

  return (
    <Panel pad="14px 16px">
      <PanelTitle right={isLoading ? '読み込み中…' : `${signals.length} 件`}>短期シグナル</PanelTitle>
      {signals.length === 0 ? <p style={{ color: OPS.dim, fontSize: 12.5, margin: 0 }}>アクティブな短期シグナルはありません。</p> : <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 10 }}>{signals.map(([ticker, signal]) => <SignalRow key={ticker} ticker={ticker} signal={signal} />)}</div>}
    </Panel>
  )
}

function SignalRow({ ticker, signal }: { ticker: string; signal: Signal }) {
  const score = signal.score ?? 0
  const color = score >= 4 ? OPS.green : score >= 2 ? OPS.amber : OPS.vermilion
  return <div style={{ background: OPS.inset, border: `1px solid ${OPS.hairline}`, borderRadius: 7, padding: '10px 11px' }}>
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, marginBottom: 7 }}><span style={{ color: OPS.text, fontFamily: OPS.mono, fontWeight: 700 }}>{ticker}</span><Chip color={color} bg={score >= 4 ? OPS.greenBg : score >= 2 ? OPS.amberBg : OPS.vermilionBg} mono>★ {score}</Chip></div>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 5, color: OPS.sub, fontFamily: OPS.mono, fontSize: 11 }}><span>IN {price(signal.entry_price)}</span><span style={{ color: OPS.green }}>TP {price(signal.target_price)}</span><span style={{ color: OPS.vermilion }}>SL {price(signal.stop_loss)}</span></div>
    {signal.reason && <p style={{ color: OPS.dim, fontSize: 11.5, lineHeight: 1.55, margin: '8px 0 0' }}>{signal.reason}</p>}
  </div>
}

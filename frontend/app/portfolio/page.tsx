'use client'

import { useState } from 'react'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, PanelTitle, Stat, Chip, Bar, Loading, Grid } from '@/components/today/ops/PageKit'
import { Modal } from '@/components/today/ops/PageKit'
import HoldingsEditor from '@/components/HoldingsEditor'

interface Position {
  key?: string; ticker?: string; name?: string; currency?: string; shares?: number
  current_price?: number; value_jpy?: number; cost_jpy?: number; unrealized_jpy?: number
  unrealized_pct?: number; sector?: string; investment_type?: string; account?: string
}
interface PortfolioData {
  positions?: Position[]; total_jpy?: number; cash_jpy?: number; cash_total_jpy?: number; cash_jpy_native?: number; cash_usd_native?: number; cash_usd_jpy?: number
  sector_breakdown?: Record<string, { value_jpy: number; ratio: number }>
  currency_breakdown?: Record<string, { value_jpy?: number; ratio?: number }>
  as_of?: string
}

const TIER_COLOR: Record<string, string> = { long: OPS.blue, medium: OPS.amber, swing: OPS.green }
const SECTOR_COLOR = [OPS.gold, OPS.blue, OPS.green, OPS.amber, OPS.vermilion, OPS.orchid, OPS.redSoft, OPS.sub]

function yen(v?: number): string { return v == null ? '—' : `¥${Math.round(v).toLocaleString()}` }

export default function PortfolioPage() {
  const { data, isLoading } = useSWR<PortfolioData>('/api/portfolio', fetcher, { refreshInterval: 120000 })
  const [tier, setTier] = useState<'all' | 'long' | 'medium' | 'swing'>('all')
  const [editorOpen, setEditorOpen] = useState(false)

  const positions = [...(data?.positions ?? [])].sort((a, b) => (b.value_jpy ?? 0) - (a.value_jpy ?? 0))
  const filtered = tier === 'all' ? positions : positions.filter(p => p.investment_type === tier)
  const total = data?.total_jpy ?? 0
  const cash = data?.cash_total_jpy ?? data?.cash_jpy ?? 0
  const totalUnreal = positions.reduce((s, p) => s + (p.unrealized_jpy ?? 0), 0)
  const sectors = Object.entries(data?.sector_breakdown ?? {}).sort((a, b) => b[1].ratio - a[1].ratio)

  return (
    <OpsPage
      en="PORTFOLIO"
      title="ポートフォリオ"
      subtitle="保有全銘柄・セクター配分・含み損益。3層（Long / Medium / Swing）でフィルタできる。"
      right={<div style={{ display: 'flex', gap: 7, alignItems: 'center' }}><button onClick={() => setEditorOpen(true)} style={{ background: 'transparent', border: `1px solid ${OPS.hairline}`, borderRadius: 5, color: OPS.sub, cursor: 'pointer', fontFamily: OPS.mono, fontSize: 11.5, padding: '5px 8px' }}>編集</button>{data && <Chip color={OPS.dim} mono>更新 {data.as_of?.slice(5, 16).replace('T', ' ')}</Chip>}</div>}
      widthMode="wide"
    >
      {isLoading && <Loading />}
      {data && (
        <>
          <Grid cols={4} gap={12}>
            <Stat label="総資産" value={yen(total)} color={OPS.gold} />
            <Stat label="現金" value={yen(cash)} sub={total ? `${((cash / total) * 100).toFixed(1)}%` : ''} />
            <Stat label="含み損益合計" value={`${totalUnreal >= 0 ? '+' : ''}${yen(totalUnreal)}`} color={totalUnreal >= 0 ? OPS.green : OPS.redSoft} />
            <Stat label="銘柄数" value={`${positions.length}`} unit="銘柄" />
          </Grid>

          {/* セクター配分 */}
          <div style={{ marginTop: 22 }}>
            <Panel pad="16px 18px">
              <PanelTitle>セクター配分</PanelTitle>
              <div style={{ display: 'flex', height: 12, borderRadius: 6, overflow: 'hidden', marginBottom: 12 }}>
                {sectors.map(([s, v], i) => (
                  <div key={s} title={`${s} ${(v.ratio * 100).toFixed(1)}%`} style={{ width: `${v.ratio * 100}%`, background: SECTOR_COLOR[i % SECTOR_COLOR.length] }} />
                ))}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
                {sectors.map(([s, v], i) => (
                  <span key={s} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: OPS.sub, fontFamily: OPS.mono }}>
                    <span style={{ width: 8, height: 8, borderRadius: 2, background: SECTOR_COLOR[i % SECTOR_COLOR.length] }} />
                    {s} {(v.ratio * 100).toFixed(1)}%
                  </span>
                ))}
              </div>
            </Panel>
          </div>

          {/* tier filter */}
          <div style={{ display: 'flex', gap: 4, margin: '22px 0 12px' }}>
            {(['all', 'long', 'medium', 'swing'] as const).map(t => {
              const on = t === tier
              return (
                <button key={t} onClick={() => setTier(t)} style={{
                  background: on ? OPS.goldBg : 'transparent', border: `1px solid ${on ? OPS.gold + '66' : OPS.hairline}`,
                  borderRadius: 5, color: on ? OPS.gold : OPS.sub, fontSize: 12.5, padding: '4px 13px', cursor: 'pointer', fontFamily: OPS.mono,
                }}>
                  {t === 'all' ? 'ALL' : t}
                </button>
              )
            })}
          </div>

          <Panel pad="8px 16px">
            <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', minWidth: 720, borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ color: OPS.dim, fontSize: 12, textAlign: 'left' }}>
                  <th style={th}>銘柄</th>
                  <th style={th}>種別</th>
                  <th style={{ ...th, textAlign: 'right' }}>数量</th>
                  <th style={{ ...th, textAlign: 'right' }}>評価額</th>
                  <th style={{ ...th, textAlign: 'right' }}>比率</th>
                  <th style={{ ...th, textAlign: 'right' }}>損益</th>
                  <th style={{ ...th, width: '16%' }} />
                </tr>
              </thead>
              <tbody>
                {filtered.map((p, i) => {
                  const w = total ? ((p.value_jpy ?? 0) / total) * 100 : 0
                  const pnl = (p.unrealized_pct ?? 0) * 100
                  const tc = TIER_COLOR[p.investment_type ?? ''] ?? OPS.dim
                  return (
                    <tr key={`${p.key}-${i}`} className="ops-row" style={{ borderTop: `1px solid ${OPS.hairline}` }}>
                      <td style={td}>
                        <span style={{ fontFamily: OPS.mono, fontWeight: 500, color: OPS.text }}>{p.ticker}</span>
                        <span style={{ color: OPS.dim, fontSize: 11.5, marginLeft: 8 }}>{p.name}</span>
                      </td>
                      <td style={{ ...td, color: tc, fontSize: 12 }}>{p.investment_type}</td>
                      <td style={{ ...td, textAlign: 'right', fontFamily: OPS.mono, color: OPS.sub }}>{p.shares?.toLocaleString() ?? '—'}</td>
                      <td style={{ ...td, textAlign: 'right', fontFamily: OPS.mono, color: OPS.text }}>{yen(p.value_jpy)}</td>
                      <td style={{ ...td, textAlign: 'right', fontFamily: OPS.mono, color: OPS.sub }}>{w.toFixed(1)}%</td>
                      <td style={{ ...td, textAlign: 'right', fontFamily: OPS.mono, fontWeight: 500, color: pnl >= 0 ? OPS.green : OPS.redSoft }}>
                        {pnl >= 0 ? '+' : ''}{pnl.toFixed(1)}%
                      </td>
                      <td style={{ ...td, paddingLeft: 12 }}>
                        <Bar pct={Math.min(100, w * 5)} color={tc} height={4} />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            </div>
          </Panel>
        </>
      )}
      <Modal open={editorOpen} onClose={() => setEditorOpen(false)} width={1120} fitViewport>
        <div style={{ overflowY: 'auto', paddingRight: 4 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}><span style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 12, letterSpacing: '0.1em' }}>HOLDINGS EDITOR</span><Chip color={OPS.green} bg={OPS.greenBg} mono>記録可</Chip></div>
          <HoldingsEditor />
        </div>
      </Modal>
    </OpsPage>
  )
}

const th: React.CSSProperties = { padding: '6px 8px', fontWeight: 400 }
const td: React.CSSProperties = { padding: '7px 8px', verticalAlign: 'middle' }

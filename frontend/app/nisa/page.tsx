'use client'

import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, PanelTitle, Chip, Bar, Loading, Grid } from '@/components/today/ops/PageKit'

interface Person {
  broker?: string
  tsumitate_limit_annual?: number
  growth_limit_annual?: number
  lifetime_limit?: number
  tsumitate_used_this_year?: number
  growth_used_this_year?: number
  lifetime_used_estimate?: number
  nisa_assets_value?: number
  nisa_unrealized_gain?: number
  nisa_unrealized_pct?: number
  holdings?: Record<string, unknown>
}
interface Proposal {
  ticker?: string
  name?: string
  score?: number
  recommended_account?: string
  current_account?: string
  expected_return_pct?: number
  dividend_yield?: number
}
interface NisaData {
  husband: Person
  wife: Person
  last_updated?: string
  placement_proposals?: Proposal[]
}

function yen(v?: number): string {
  if (v == null) return '—'
  return `¥${Math.round(v).toLocaleString()}`
}

export default function NisaPage() {
  const { data, isLoading } = useSWR<NisaData>('/api/nisa', fetcher, { refreshInterval: 300000 })

  return (
    <OpsPage
      en="NISA"
      title="NISA 枠・配置最適化"
      subtitle="本人（楽天）・妻（SBI）の非課税枠の使用状況と、成長投資枠に置くべき銘柄の配置提案（表示のみ）。"
      right={data && <Chip color={OPS.dim} mono>更新 {data.last_updated}</Chip>}
    >
      {isLoading && <Loading />}
      {data && (
        <>
          <Grid cols={2} gap={16}>
            <PersonCard title="本人 NISA" person={data.husband} />
            <PersonCard title="妻 NISA" person={data.wife} />
          </Grid>

          {data.placement_proposals && data.placement_proposals.length > 0 && (
            <div style={{ marginTop: 26 }}>
              <Panel pad="16px 18px">
                <PanelTitle right={`${data.placement_proposals.length} 件 · 表示のみ`}>成長投資枠 配置提案</PanelTitle>
                <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', minWidth: 620, borderCollapse: 'collapse', fontSize: 13 }}>
                  <thead>
                    <tr style={{ color: OPS.dim, fontSize: 12, textAlign: 'left' }}>
                      <th style={th}>銘柄</th>
                      <th style={th}>推奨口座</th>
                      <th style={th}>現在</th>
                      <th style={{ ...th, textAlign: 'right' }}>スコア</th>
                      <th style={{ ...th, textAlign: 'right' }}>期待</th>
                      <th style={{ ...th, textAlign: 'right' }}>配当</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.placement_proposals.slice(0, 16).map((p, i) => (
                      <tr key={i} className="ops-row" style={{ borderTop: `1px solid ${OPS.hairline}` }}>
                        <td style={td}>
                          <span style={{ fontFamily: OPS.mono, fontWeight: 500, color: OPS.text }}>{p.ticker}</span>
                          <span style={{ color: OPS.dim, fontSize: 11.5, marginLeft: 8 }}>{p.name}</span>
                        </td>
                        <td style={{ ...td, color: OPS.green }}>{p.recommended_account}</td>
                        <td style={{ ...td, color: OPS.dim }}>{p.current_account ?? '—'}</td>
                        <td style={{ ...td, textAlign: 'right', fontFamily: OPS.mono, color: OPS.gold }}>{p.score ?? '—'}</td>
                        <td style={{ ...td, textAlign: 'right', fontFamily: OPS.mono, color: OPS.sub }}>
                          {p.expected_return_pct != null ? `${p.expected_return_pct}%` : '—'}
                        </td>
                        <td style={{ ...td, textAlign: 'right', fontFamily: OPS.mono, color: OPS.sub }}>
                          {p.dividend_yield != null ? `${(p.dividend_yield * 100).toFixed(1)}%` : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
              </Panel>
            </div>
          )}
        </>
      )}
    </OpsPage>
  )
}

function PersonCard({ title, person }: { title: string; person: Person }) {
  const gain = person.nisa_unrealized_gain ?? 0
  return (
    <Panel pad="18px 20px">
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 16 }}>
        <span style={{ fontSize: 15, fontWeight: 600, color: OPS.text }}>{title}</span>
        <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, fontSize: 12, color: OPS.dim }}>{person.broker}</span>
      </div>

      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 4 }}>
        <span style={{ fontFamily: OPS.mono, fontSize: 26, fontWeight: 500, color: OPS.text }}>
          {yen(person.nisa_assets_value)}
        </span>
        <span style={{ fontFamily: OPS.mono, fontSize: 14, color: gain >= 0 ? OPS.green : OPS.redSoft }}>
          {gain >= 0 ? '+' : ''}{yen(Math.abs(gain))}
          {person.nisa_unrealized_pct != null && ` (${person.nisa_unrealized_pct >= 0 ? '+' : ''}${person.nisa_unrealized_pct}%)`}
        </span>
      </div>
      <div style={{ fontSize: 11, color: OPS.dim, marginBottom: 16 }}>NISA 資産評価額 / 含み損益</div>

      <FrameBar label="成長投資枠（今年）" used={person.growth_used_this_year} limit={person.growth_limit_annual} color={OPS.gold} />
      <FrameBar label="つみたて投資枠（今年）" used={person.tsumitate_used_this_year} limit={person.tsumitate_limit_annual} color={OPS.green} />
      <FrameBar label="生涯投資枠" used={person.lifetime_used_estimate} limit={person.lifetime_limit} color={OPS.blue} />
    </Panel>
  )
}

function FrameBar({ label, used, limit, color }: { label: string; used?: number; limit?: number; color: string }) {
  const u = used ?? 0
  const l = limit ?? 0
  const pct = l > 0 ? (u / l) * 100 : 0
  const remain = Math.max(0, l - u)
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5, marginBottom: 5 }}>
        <span style={{ color: OPS.sub }}>{label}</span>
        <span style={{ fontFamily: OPS.mono, color: OPS.dim }}>
          {yen(u)} / {yen(l)}　残 <span style={{ color }}>{yen(remain)}</span>
        </span>
      </div>
      <Bar pct={pct} color={color} height={7} />
    </div>
  )
}

const th: React.CSSProperties = { padding: '5px 8px', fontWeight: 400 }
const td: React.CSSProperties = { padding: '7px 8px', verticalAlign: 'middle' }

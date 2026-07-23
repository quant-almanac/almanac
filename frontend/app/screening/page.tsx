'use client'

import { useState } from 'react'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, PanelTitle, Stat, Chip, Modal, Loading, Grid } from '@/components/today/ops/PageKit'
import ShortSignalsPanel from '@/components/today/ops/ShortSignalsPanel'

interface ScreenItem {
  ticker?: string; name?: string; sector?: string; industry?: string; currency?: string
  price?: number; market_cap?: number; roe?: number; roa?: number
  eps_growth?: number; eps_growth_5y?: number; rev_growth?: number
  gross_margin?: number; operating_margin?: number; net_margin?: number
  [k: string]: unknown
}
interface ScreeningData {
  long_term?: { passed?: ScreenItem[]; rejected_count?: number; total_screened?: number; criteria?: Record<string, unknown>; as_of?: string }
  optimization?: { recommended?: string; regime?: string; as_of?: string }
  short_term?: { candidates?: unknown[]; regime?: string; vix?: number; vix_blocked?: boolean; as_of?: string }
}

function pct(v?: number): string { return v == null ? '—' : `${(v * 100).toFixed(1)}%` }

export default function ScreeningPage() {
  const { data, isLoading } = useSWR<ScreeningData>('/api/screening', fetcher, { refreshInterval: 600000 })
  const [open, setOpen] = useState<ScreenItem | null>(null)
  const [tab, setTab] = useState<'long' | 'signals'>('long')

  const lt = data?.long_term
  const passed = lt?.passed ?? []

  return (
    <OpsPage
      en="SCREENING"
      title="スクリーニング"
      subtitle="長期のファンダメンタル通過銘柄、ポートフォリオ最適化の推奨、短期テクニカル候補。行クリックで銘柄の全指標が開く。"
      right={lt && <Chip color={OPS.dim} mono>{lt.as_of}</Chip>}
      widthMode="wide"
    >
      {isLoading && <Loading />}
      {data && (
        <>
          <div role="tablist" aria-label="スクリーニング表示" style={{ display: 'flex', gap: 6, marginBottom: 16 }}>
            {([['long', '長期スクリーニング'], ['signals', '短期シグナル']] as const).map(([key, label]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)} style={{ background: tab === key ? OPS.goldBg : 'transparent', border: `1px solid ${tab === key ? `${OPS.gold}66` : OPS.hairline}`, borderRadius: 5, color: tab === key ? OPS.gold : OPS.sub, cursor: 'pointer', fontFamily: OPS.mono, fontSize: 11.5, padding: '6px 10px' }}>{label}</button>)}
          </div>
          {tab === 'signals' ? <ShortSignalsPanel /> : <>
          <Grid cols={4} gap={12}>
            <Stat label="長期スクリーン対象" value={`${lt?.total_screened ?? '—'}`} unit="銘柄" />
            <Stat label="通過" value={`${passed.length}`} unit="銘柄" color={OPS.green} />
            <Stat label="却下" value={`${lt?.rejected_count ?? '—'}`} unit="銘柄" color={OPS.dim} />
            <Stat label="最適化推奨" value={data.optimization?.recommended ?? '—'} sub={data.optimization?.regime} color={OPS.gold} />
          </Grid>

          {data.short_term && (
            <div style={{ marginTop: 16, display: 'flex', alignItems: 'center', gap: 12, fontSize: 12.5, color: OPS.sub }}>
              <Chip color={OPS.blue} bg={OPS.blueBg} mono>短期候補 {data.short_term.candidates?.length ?? 0}</Chip>
              {data.short_term.vix != null && <span style={{ fontFamily: OPS.mono }}>VIX {data.short_term.vix.toFixed(1)}</span>}
              {data.short_term.vix_blocked && <span style={{ color: OPS.amber }}>VIX ブロック中</span>}
              <span style={{ fontFamily: OPS.mono, color: OPS.dim }}>レジーム {data.short_term.regime}</span>
            </div>
          )}

          <div style={{ marginTop: 22 }}>
            <Panel pad="8px 16px">
              <PanelTitle right={`通過 ${passed.length} 銘柄`}>長期ファンダメンタル通過</PanelTitle>
              <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', minWidth: 520, borderCollapse: 'collapse', fontSize: 13 }}>
                <thead>
                  <tr style={{ color: OPS.dim, fontSize: 12, textAlign: 'left' }}>
                    <th style={th}>銘柄</th>
                    <th style={th}>セクター</th>
                    <th style={{ ...th, textAlign: 'right' }}>ROE</th>
                    <th style={{ ...th, textAlign: 'right' }}>EPS成長</th>
                    <th style={{ ...th, textAlign: 'right' }}>売上成長</th>
                    <th style={{ ...th, textAlign: 'right' }}>純利益率</th>
                  </tr>
                </thead>
                <tbody>
                  {passed.map((p, i) => (
                    <tr key={`${p.ticker}-${i}`} className="ops-row ops-clickable" role="button" tabIndex={0} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setOpen(p) } }} onClick={() => setOpen(p)} style={{ borderTop: `1px solid ${OPS.hairline}`, cursor: 'pointer' }}>
                      <td style={td}>
                        <span style={{ fontFamily: OPS.mono, fontWeight: 500, color: OPS.text }}>{p.ticker}</span>
                        <span style={{ color: OPS.dim, fontSize: 11.5, marginLeft: 8 }}>{p.name}</span>
                      </td>
                      <td style={{ ...td, color: OPS.sub, fontSize: 12 }}>{p.sector}</td>
                      <PctTd v={p.roe} />
                      <PctTd v={p.eps_growth} />
                      <PctTd v={p.rev_growth} />
                      <PctTd v={p.net_margin} />
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
              {passed.length === 0 && <p style={{ fontSize: 12, color: OPS.dim, padding: '10px 0' }}>通過銘柄なし</p>}
            </Panel>
          </div>
          </>}
        </>
      )}

      <Modal open={!!open} onClose={() => setOpen(null)} width={600}>
        {open && (
          <>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 4 }}>
              <span style={{ fontFamily: OPS.mono, fontSize: 18, fontWeight: 700, color: OPS.gold }}>{open.ticker}</span>
              <span style={{ color: OPS.text, fontSize: 14 }}>{open.name}</span>
            </div>
            <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.dim, marginBottom: 16 }}>
              {open.sector} · {open.industry} {open.price != null && `· ${open.currency === 'USD' ? '$' : '¥'}${open.price}`}
            </div>
            <Grid cols={3} gap={10}>
              <Stat label="ROE" value={pct(open.roe)} color={OPS.green} />
              <Stat label="ROA" value={pct(open.roa)} />
              <Stat label="EPS成長" value={pct(open.eps_growth)} />
              <Stat label="EPS成長 5年" value={pct(open.eps_growth_5y)} />
              <Stat label="売上成長" value={pct(open.rev_growth)} />
              <Stat label="粗利率" value={pct(open.gross_margin)} />
              <Stat label="営業利益率" value={pct(open.operating_margin)} />
              <Stat label="純利益率" value={pct(open.net_margin)} color={OPS.gold} />
              <Stat label="時価総額" value={open.market_cap != null ? `${(open.market_cap / 1e9).toFixed(0)}B` : '—'} />
            </Grid>
          </>
        )}
      </Modal>
    </OpsPage>
  )
}

function PctTd({ v }: { v?: number }) {
  if (v == null) return <td style={{ ...td, textAlign: 'right', color: OPS.dim }}>—</td>
  const p = v * 100
  return <td style={{ ...td, textAlign: 'right', fontFamily: OPS.mono, color: p >= 0 ? OPS.green : OPS.redSoft }}>{p.toFixed(1)}%</td>
}

const th: React.CSSProperties = { padding: '6px 8px', fontWeight: 400 }
const td: React.CSSProperties = { padding: '7px 8px', verticalAlign: 'middle' }

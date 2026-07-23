'use client'

import { useState } from 'react'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, Stat, Chip, Bar, Modal, Loading, Grid } from '@/components/today/ops/PageKit'

interface SignalDetail {
  type: string
  key: string
  matched: boolean
  detail?: string
  value?: number
  threshold?: number
}
interface Scenario {
  name: string
  status: string
  readiness: number
  signals_met: number
  signals_total: number
  signal_details?: SignalDetail[]
  playbook?: string[] | string
  narrative?: string
}
interface ScenariosData {
  scenarios: Record<string, Scenario>
  active_count: number
  partial_count: number
  watching_count: number
  overall_alert_level: string
  evaluated_at: string
}

const STATUS_META: Record<string, { label: string; color: string }> = {
  active: { label: '発動中', color: OPS.vermilion },
  partial: { label: '部分発動', color: OPS.amber },
  watching: { label: '監視', color: OPS.blue },
  dormant: { label: '待機', color: OPS.dim },
}
const ALERT_COLOR: Record<string, string> = { high: OPS.vermilion, elevated: OPS.amber, normal: OPS.green, low: OPS.green }

export default function ScenariosPage() {
  const { data, isLoading } = useSWR<ScenariosData>('/api/scenarios', fetcher, { refreshInterval: 300000 })
  const [openKey, setOpenKey] = useState<string | null>(null)

  const list = data
    ? Object.entries(data.scenarios).sort((a, b) => b[1].readiness - a[1].readiness)
    : []
  const open = openKey && data ? data.scenarios[openKey] : null

  return (
    <OpsPage
      en="SCENARIOS"
      title="シナリオ・レーダー"
      subtitle="13 の相場シナリオを常時採点。各シナリオの発動条件（ニュース・指標）がどこまで揃っているかを readiness で可視化し、発動時のプレイブックを事前に用意する。"
      right={
        data && (
          <Chip color={ALERT_COLOR[data.overall_alert_level] ?? OPS.sub} bg={OPS.dimBg} mono>
            警戒レベル {data.overall_alert_level.toUpperCase()}
          </Chip>
        )
      }
    >
      {isLoading && <Loading />}
      {data && (
        <>
          <Grid cols={4} gap={12}>
            <Stat label="発動中" value={`${data.active_count}`} unit="件" color={OPS.vermilion} />
            <Stat label="部分発動" value={`${data.partial_count}`} unit="件" color={OPS.amber} />
            <Stat label="監視" value={`${data.watching_count}`} unit="件" color={OPS.blue} />
            <Stat label="評価時刻" value={data.evaluated_at.slice(5, 16).replace('T', ' ')} />
          </Grid>

          <div style={{ marginTop: 24 }}>
            <Grid minmax={340} gap={12}>
              {list.map(([key, s]) => {
                const meta = STATUS_META[s.status] ?? { label: s.status, color: OPS.dim }
                const pct = Math.round(s.readiness * 100)
                return (
                  <Panel key={key} hover onClick={() => setOpenKey(key)} pad="14px 16px">
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
                      <span style={{ fontSize: 14, fontWeight: 600, color: OPS.text }}>{s.name}</span>
                      <span style={{ marginLeft: 'auto' }}>
                        <Chip color={meta.color} bg={OPS.dimBg} mono>
                          ● {meta.label}
                        </Chip>
                      </span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                      <Bar pct={pct} color={meta.color} />
                      <span style={{ fontFamily: OPS.mono, fontSize: 14, color: meta.color, minWidth: 40, textAlign: 'right' }}>
                        {pct}%
                      </span>
                    </div>
                    <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.dim }}>
                      条件成立 {s.signals_met}/{s.signals_total} · クリックで詳細
                    </div>
                  </Panel>
                )
              })}
            </Grid>
          </div>
        </>
      )}

      <Modal open={!!open} onClose={() => setOpenKey(null)} width={620}>
        {open && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
              <h2 style={{ fontSize: 20, fontWeight: 700, color: OPS.text, margin: 0 }}>{open.name}</h2>
              <Chip color={(STATUS_META[open.status] ?? { color: OPS.dim }).color} bg={OPS.dimBg} mono>
                {(STATUS_META[open.status] ?? { label: open.status }).label}
              </Chip>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '12px 0 18px' }}>
              <span style={{ fontFamily: OPS.mono, fontSize: 13, color: OPS.gold }}>
                readiness {Math.round(open.readiness * 100)}%
              </span>
              <Bar pct={open.readiness * 100} color={(STATUS_META[open.status] ?? { color: OPS.gold }).color} />
              <span style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.sub }}>
                {open.signals_met}/{open.signals_total} 条件
              </span>
            </div>
            {open.narrative && (
              <p style={{ fontSize: 13, color: OPS.sub, lineHeight: 1.8, margin: '0 0 16px' }}>{open.narrative}</p>
            )}
            <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.gold, letterSpacing: '0.12em', marginBottom: 8 }}>
              発動条件（{open.signal_details?.length ?? 0}）
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: '46vh', overflowY: 'auto' }}>
              {(open.signal_details ?? []).map((sd, i) => (
                <div
                  key={i}
                  style={{
                    display: 'flex',
                    alignItems: 'baseline',
                    gap: 10,
                    padding: '7px 10px',
                    background: OPS.inset,
                    borderRadius: 6,
                    borderLeft: `2px solid ${sd.matched ? OPS.green : OPS.dim}`,
                    fontSize: 12,
                  }}
                >
                  <span style={{ color: sd.matched ? OPS.green : OPS.dim, fontFamily: OPS.mono }}>
                    {sd.matched ? '✓' : '·'}
                  </span>
                  <span style={{ fontFamily: OPS.mono, color: OPS.sub, minWidth: 110 }}>{sd.key}</span>
                  <span style={{ color: sd.matched ? OPS.text : OPS.dim, flex: 1 }}>{sd.detail}</span>
                </div>
              ))}
            </div>
            {Array.isArray(open.playbook) && open.playbook.length > 0 && (
              <>
                <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.gold, letterSpacing: '0.12em', margin: '16px 0 8px' }}>
                  プレイブック
                </div>
                {open.playbook.map((p, i) => (
                  <div key={i} style={{ fontSize: 12.5, color: OPS.sub, lineHeight: 1.7, display: 'flex', gap: 8 }}>
                    <span style={{ color: OPS.gold }}>›</span>
                    {p}
                  </div>
                ))}
              </>
            )}
          </>
        )}
      </Modal>
    </OpsPage>
  )
}

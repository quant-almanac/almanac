'use client'

import { useState } from 'react'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS, TYPE_META, STANCE_LABEL } from '@/components/today/ops/tokens'
import { OpsPage, Panel, PanelTitle, Chip, Modal, Loading } from '@/components/today/ops/PageKit'

interface AnalysisSnap {
  as_of?: string
  overall_stance?: string
  stance_reason?: string
  weekly_theme?: string
  priority_actions?: { ticker?: string; type?: string; action?: string }[]
  risk_warnings?: string[]
  geopolitical_note?: string
}
interface Execution {
  id?: string
  ticker?: string
  direction?: string
  action?: string
  status?: string
  price?: number
  quantity?: number
  currency?: string
  saved_at?: string
  filled_at?: string
}

const DIR_LABEL: Record<string, { label: string; color: string }> = {
  buy: { label: '買い', color: OPS.green },
  margin_buy: { label: '信用買い', color: OPS.green },
  sell: { label: '売り', color: OPS.vermilion },
  short: { label: '空売り', color: OPS.vermilion },
  cover: { label: '買戻し', color: OPS.blue },
  hold: { label: '保持', color: OPS.blue },
}

export default function HistoryPage() {
  const { data: hist, isLoading } = useSWR<AnalysisSnap[] | { history?: AnalysisSnap[] }>('/api/ai-analysis/history', fetcher)
  const { data: exec } = useSWR<{ executions: Execution[] }>('/api/actions/executions', fetcher, { refreshInterval: 60000 })
  const [openIdx, setOpenIdx] = useState<number | null>(null)

  const snaps = (Array.isArray(hist) ? hist : hist?.history ?? []).slice().reverse()
  const execs = (exec?.executions ?? []).slice().reverse()
  const open = openIdx != null ? snaps[openIdx] : null

  return (
    <OpsPage
      en="HISTORY"
      title="分析・執行の履歴"
      subtitle="過去の統合分析スナップショットと、記録された売買執行の台帳。分析カードをクリックすると当時の全文が開く。"
    >
      {isLoading && <Loading />}

      <div style={{ display: 'grid', gridTemplateColumns: '1.1fr 1fr', gap: 22, alignItems: 'start' }}>
        {/* 分析タイムライン */}
        <div>
          <PanelTitle right={`${snaps.length} 回`}>統合分析タイムライン</PanelTitle>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {snaps.map((s, i) => (
              <Panel key={i} hover onClick={() => setOpenIdx(i)} pad="13px 16px">
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                  <span style={{ fontFamily: OPS.mono, fontSize: 12.5, color: OPS.gold }}>{s.as_of}</span>
                  {s.overall_stance && (
                    <Chip color={OPS.amber} bg={OPS.amberBg}>{STANCE_LABEL[s.overall_stance] ?? s.overall_stance}</Chip>
                  )}
                  <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, fontSize: 11, color: OPS.dim }}>
                    {(s.priority_actions?.length ?? 0)} アクション
                  </span>
                </div>
                {s.weekly_theme && (
                  <div style={{ fontSize: 12.5, color: OPS.sub, lineHeight: 1.6, overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                    {s.weekly_theme}
                  </div>
                )}
                <div style={{ display: 'flex', gap: 5, marginTop: 8, flexWrap: 'wrap' }}>
                  {(s.priority_actions ?? []).slice(0, 5).map((a, j) => {
                    const t = a.type ? TYPE_META[a.type] : null
                    return (
                      <Chip key={j} color={t?.color ?? OPS.sub} bg={OPS.dimBg} mono>
                        {a.ticker}
                      </Chip>
                    )
                  })}
                </div>
              </Panel>
            ))}
            {snaps.length === 0 && <p style={{ fontSize: 12, color: OPS.dim }}>履歴なし</p>}
          </div>
        </div>

        {/* 執行台帳 */}
        <div>
          <PanelTitle right={`${execs.length} 件`}>執行台帳</PanelTitle>
          <Panel pad="6px 14px">
            <div style={{ maxHeight: '70vh', overflowY: 'auto' }}>
              {execs.map((e, i) => {
                const dir = e.direction ? DIR_LABEL[e.direction] : null
                const filled = e.status === 'executed' || e.status === 'filled' || !!e.filled_at
                return (
                  <div
                    key={e.id ?? i}
                    className="ops-row"
                    style={{
                      display: 'flex',
                      alignItems: 'baseline',
                      gap: 10,
                      padding: '8px 4px',
                      borderTop: i > 0 ? `1px solid ${OPS.hairline}` : 'none',
                      fontSize: 12,
                    }}
                  >
                    <span style={{ fontFamily: OPS.mono, color: OPS.dim, minWidth: 72 }}>
                      {(e.saved_at ?? '').slice(5, 10)}
                    </span>
                    <span style={{ fontFamily: OPS.mono, fontWeight: 500, color: OPS.text, minWidth: 52 }}>{e.ticker}</span>
                    {dir && <span style={{ color: dir.color, minWidth: 52, flexShrink: 0 }}>{dir.label}</span>}
                    <span style={{ fontFamily: OPS.mono, color: OPS.sub, minWidth: 60 }}>
                      {e.price != null ? `${e.currency === 'USD' ? '$' : '¥'}${e.price}` : '—'}
                    </span>
                    <span style={{ fontFamily: OPS.mono, color: OPS.dim }}>×{e.quantity ?? '—'}</span>
                    <span style={{ marginLeft: 'auto' }}>
                      <Chip color={filled ? OPS.green : OPS.amber} bg={filled ? OPS.greenBg : OPS.amberBg} mono>
                        {filled ? '約定' : e.status}
                      </Chip>
                    </span>
                  </div>
                )
              })}
              {execs.length === 0 && <p style={{ fontSize: 12, color: OPS.dim, padding: '10px 0' }}>執行なし</p>}
            </div>
          </Panel>
        </div>
      </div>

      <Modal open={!!open} onClose={() => setOpenIdx(null)} width={640}>
        {open && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
              <h2 style={{ fontSize: 18, fontWeight: 700, color: OPS.gold, margin: 0, fontFamily: OPS.mono }}>{open.as_of}</h2>
              {open.overall_stance && (
                <Chip color={OPS.amber} bg={OPS.amberBg}>{STANCE_LABEL[open.overall_stance] ?? open.overall_stance}</Chip>
              )}
            </div>
            {open.weekly_theme && <HField label="今週のテーマ">{open.weekly_theme}</HField>}
            {open.stance_reason && <HField label="スタンス理由">{open.stance_reason}</HField>}
            {open.geopolitical_note && <HField label="地政学">{open.geopolitical_note}</HField>}
            {open.priority_actions && open.priority_actions.length > 0 && (
              <HField label="優先アクション">
                {open.priority_actions.map((a, i) => (
                  <div key={i} style={{ marginBottom: 3 }}>
                    <span style={{ fontFamily: OPS.mono, color: OPS.text }}>{a.ticker}</span>{' '}
                    <span style={{ color: OPS.sub }}>{a.action}</span>
                  </div>
                ))}
              </HField>
            )}
            {open.risk_warnings && open.risk_warnings.length > 0 && (
              <HField label="リスク警告">
                {open.risk_warnings.map((w, i) => (
                  <div key={i} style={{ color: OPS.redSoft, marginBottom: 3 }}>▪ {w}</div>
                ))}
              </HField>
            )}
          </>
        )}
      </Modal>
    </OpsPage>
  )
}

function HField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.gold, letterSpacing: '0.1em', marginBottom: 5 }}>{label}</div>
      <div style={{ fontSize: 13, color: OPS.sub, lineHeight: 1.75 }}>{children}</div>
    </div>
  )
}

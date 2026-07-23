'use client'

import { useState, useMemo } from 'react'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, Chip, Modal, Loading } from '@/components/today/ops/PageKit'

interface Feature {
  ticker?: string
  market?: string
  source?: string
  disclosure_type?: string
  publish_time?: string
  summary?: string
  core?: string | Record<string, unknown>
  context?: string | Record<string, unknown>
  evidence?: string | Record<string, unknown>
  source_url?: string
  model_id?: string
  prompt_version?: string
}
interface DiscData {
  generated_at: string
  count: number
  observe_only: boolean
  status_note?: string
  features: Feature[]
}

export default function DisclosuresPage() {
  const { data, isLoading } = useSWR<DiscData>('/api/disclosure-features', fetcher, { refreshInterval: 600000 })
  const [openIdx, setOpenIdx] = useState<number | null>(null)
  const [market, setMarket] = useState<'all' | 'JP' | 'US'>('all')

  const features = useMemo(() => {
    const f = data?.features ?? []
    return market === 'all' ? f : f.filter(x => (x.market ?? '').toUpperCase().startsWith(market))
  }, [data, market])
  const open = openIdx != null ? features[openIdx] : null

  return (
    <OpsPage
      en="DISCLOSURE AI"
      title="開示 AI 特徴量"
      subtitle="公開開示（適時開示 / EDINET / EDGAR）を LLM が構造化した観測専用の特徴量。売買判断・サイズ決定には一切使っていない参考データ。"
      right={
        <div style={{ display: 'flex', gap: 4 }}>
          {(['all', 'JP', 'US'] as const).map(m => (
            <button
              key={m}
              onClick={() => setMarket(m)}
              style={{
                background: market === m ? OPS.goldBg : 'transparent',
                border: `1px solid ${market === m ? OPS.gold + '66' : OPS.hairline}`,
                borderRadius: 5,
                color: market === m ? OPS.gold : OPS.sub,
                fontSize: 12,
                fontFamily: OPS.mono,
                padding: '4px 12px',
                cursor: 'pointer',
              }}
            >
              {m === 'all' ? 'ALL' : m}
            </button>
          ))}
        </div>
      }
    >
      {isLoading && <Loading />}
      {data && (
        <>
          <div
            className="ops-sec"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: '10px 14px',
              borderRadius: 8,
              background: OPS.amberBg,
              border: `1px solid ${OPS.amber}33`,
              marginBottom: 20,
              fontSize: 12.5,
              color: OPS.amber,
            }}
          >
            <span style={{ fontFamily: OPS.mono, fontWeight: 600 }}>OBSERVE ONLY</span>
            <span style={{ color: OPS.sub }}>{data.status_note ?? '参考のみ。売買判断には使用していません。'}</span>
            <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, color: OPS.dim }}>
              {features.length} 件 · 生成 {data.generated_at.slice(5, 16).replace('T', ' ')}
            </span>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {features.map((f, i) => (
              <Panel key={i} hover onClick={() => setOpenIdx(i)} pad="12px 16px">
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
                  <span style={{ fontFamily: OPS.mono, fontSize: 14, fontWeight: 600, color: OPS.text, minWidth: 64 }}>
                    {f.ticker ?? '—'}
                  </span>
                  {f.market && <Chip color={OPS.blue} bg={OPS.blueBg} mono>{f.market}</Chip>}
                  {f.disclosure_type && <Chip color={OPS.sub}>{f.disclosure_type}</Chip>}
                  <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, fontSize: 11, color: OPS.dim }}>
                    {(f.publish_time ?? '').slice(0, 16).replace('T', ' ')}
                  </span>
                </div>
                <div style={{ fontSize: 13, color: OPS.sub, lineHeight: 1.6, marginTop: 6, overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                  {f.summary ?? (typeof f.core === 'string' ? f.core : '—')}
                </div>
              </Panel>
            ))}
          </div>
        </>
      )}

      <Modal open={!!open} onClose={() => setOpenIdx(null)} width={680}>
        {open && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8, flexWrap: 'wrap' }}>
              <span style={{ fontFamily: OPS.mono, fontSize: 18, fontWeight: 700, color: OPS.gold }}>{open.ticker}</span>
              {open.market && <Chip color={OPS.blue} bg={OPS.blueBg} mono>{open.market}</Chip>}
              {open.disclosure_type && <Chip color={OPS.sub}>{open.disclosure_type}</Chip>}
              {open.source && <Chip color={OPS.dim}>{open.source}</Chip>}
            </div>
            <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.dim, marginBottom: 14 }}>
              {(open.publish_time ?? '').replace('T', ' ')} · {open.model_id} · {open.prompt_version}
            </div>
            {open.summary && (
              <Field label="要約">{open.summary}</Field>
            )}
            <Field label="コア">{fmtField(open.core)}</Field>
            <Field label="文脈">{fmtField(open.context)}</Field>
            <Field label="根拠">{fmtField(open.evidence)}</Field>
            {open.source_url && (
              <a
                href={open.source_url}
                target="_blank"
                rel="noreferrer"
                style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.blue, display: 'inline-block', marginTop: 8 }}
              >
                → 開示原文
              </a>
            )}
          </>
        )}
      </Modal>
    </OpsPage>
  )
}

function fmtField(v: unknown): string {
  if (v == null) return '—'
  if (typeof v === 'string') return v
  if (typeof v === 'object') {
    return Object.entries(v as Record<string, unknown>)
      .map(([k, val]) => `${k}: ${val}`)
      .join(' · ')
  }
  return String(v)
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.gold, letterSpacing: '0.1em', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 13, color: OPS.sub, lineHeight: 1.75 }}>{children}</div>
    </div>
  )
}

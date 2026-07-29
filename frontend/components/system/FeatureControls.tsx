'use client'

import { useState } from 'react'
import useSWR from 'swr'

import { apiErrorMessage, apiFetch, fetcher } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { Chip, Panel, PanelTitle } from '@/components/today/ops/PageKit'

export interface FeatureStatus {
  key: string
  label: string
  category: string
  description: string
  configured_enabled: boolean
  effective_enabled: boolean
  mutable: boolean
  mode: string
  auto_order_enabled: boolean
  reason: string
  blockers: string[]
  warnings?: string[]
  updated_at?: string | null
  updated_by?: string | null
  control_hint?: string
  eligible_instruments?: number
  availability_universe_instruments?: number
  availability_coverage_pct?: number | null
  latest_scan_requested?: number | null
  latest_scan_downloaded?: number | null
  latest_scan_coverage_pct?: number | null
  latest_candidates?: number | null
  latest_shortable?: number | null
  latest_scan_as_of?: string | null
  latest_scan_status?: string | null
  source_as_of?: string | null
  source_age_hours?: number | null
  freshness_status?: string
  source?: string
  source_note?: string
  model_version?: string | null
  availability_label?: string
  availability_metric_kind?: string
  metrics?: Array<{ label: string; value: unknown }>
}

interface FeatureResponse {
  generated_at: string
  features: FeatureStatus[]
}

const CATEGORY_LABEL: Record<string, string> = {
  short: '空売り',
  model: 'モデル',
  shadow: '影実行',
  policy: '方針',
  automation: '自動化',
  candidate: '候補',
  signal: '分析シグナル',
  data: '入力・監査',
}

const FRESHNESS_LABEL: Record<string, string> = {
  fresh: '新鮮',
  stale: '期限切れ',
  missing: '未取得',
  unknown: '時刻不明',
  not_applicable: '時刻対象外',
}

export default function FeatureControls() {
  const { data, error, isLoading, mutate } = useSWR<FeatureResponse>(
    '/api/features',
    fetcher,
    { refreshInterval: 60_000 },
  )
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  async function toggle(feature: FeatureStatus) {
    const next = !feature.configured_enabled
    if (
      next
      && !window.confirm(
        `${feature.label}を有効にします。候補生成だけがONになり、自動発注は行いません。発注時に借株可否・料率・規制を証券会社画面で再確認してください。`,
      )
    ) return
    setBusy(feature.key)
    setMessage(null)
    try {
      const response = await apiFetch(`/api/features/${feature.key}`, {
        method: 'POST',
        body: JSON.stringify({
          enabled: next,
          rationale: 'WebUIの運用機能スイッチから変更',
        }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(apiErrorMessage(payload, '機能の切替に失敗しました'))
      await mutate()
      setMessage(`${feature.label}を${next ? 'ON' : 'OFF'}にしました。`)
    } catch (toggleError) {
      setMessage(String(toggleError))
    } finally {
      setBusy(null)
    }
  }

  return <Panel pad="18px 20px">
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
      <PanelTitle>運用機能</PanelTitle>
      <Chip color={OPS.blue} mono>実効状態</Chip>
      <span style={{ marginLeft: 'auto', color: OPS.dim, fontSize: 10.5, fontFamily: OPS.mono }}>
        {data?.generated_at?.slice(0, 19).replace('T', ' ') ?? '確認中'}
      </span>
    </div>
    <p style={{ color: OPS.sub, fontSize: 12, lineHeight: 1.7, margin: '7px 0 13px' }}>
      設定ONと実効ONを分けて表示します。入力不足や期限切れでは設定がONでも安全側に停止します。
      空売りはすべて人間実行専用で、この画面から自動注文は有効になりません。
    </p>
    {message && <div role="status" style={{ color: message.includes('失敗') || message.startsWith('Error') ? OPS.redSoft : OPS.green, fontSize: 12, marginBottom: 10 }}>{message}</div>}
    {error && <div role="alert" style={{ color: OPS.redSoft, fontSize: 12 }}>/api/features を取得できません。</div>}
    {isLoading && <div style={{ color: OPS.dim, fontSize: 12 }}>機能状態を確認中…</div>}
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {data?.features.map(feature => {
        const effectiveColor = feature.effective_enabled
          ? OPS.green
          : feature.configured_enabled
            ? OPS.amber
            : OPS.dim
        const effectiveLabel = feature.effective_enabled
          ? '実効 ON'
          : feature.configured_enabled && feature.blockers.length > 0
            ? '設定ON・安全停止'
            : feature.configured_enabled
              ? '設定ON・観測中'
            : 'OFF'
        return <div
          key={feature.key}
          data-testid={`feature-${feature.key}`}
          style={{
            display: 'flex',
            flexWrap: 'wrap',
            gap: 16,
            alignItems: 'center',
            padding: '14px 0',
            borderTop: `1px solid ${OPS.hairline}`,
          }}
        >
          <div style={{ flex: '0 1 180px' }}>
            <div style={{ display: 'flex', gap: 7, alignItems: 'center', flexWrap: 'wrap' }}>
              <span style={{ color: OPS.text, fontWeight: 650, fontSize: 13.5 }}>{feature.label}</span>
              <span style={{ color: OPS.dim, fontSize: 9.5, fontFamily: OPS.mono }}>{CATEGORY_LABEL[feature.category] ?? feature.category}</span>
            </div>
            <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10, marginTop: 5 }}>
              {feature.key} · {feature.mode}
            </div>
          </div>
          <div style={{ flex: '1 1 300px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}>
              <Chip color={effectiveColor} mono>{effectiveLabel}</Chip>
              {feature.configured_enabled && !feature.effective_enabled && feature.blockers.length > 0 && <Chip color={OPS.amber} bg={OPS.amberBg} mono>FAIL-CLOSED</Chip>}
              {feature.auto_order_enabled === false && <Chip color={OPS.dim} mono>自動注文なし</Chip>}
            </div>
            <div style={{ color: OPS.sub, fontSize: 11.5, lineHeight: 1.6, marginTop: 6 }}>{feature.reason}</div>
            {feature.blockers.length > 0 && <div style={{ color: OPS.amber, fontSize: 10.5, lineHeight: 1.55, marginTop: 3 }}>
              無効理由: {feature.blockers.join(' / ')}
            </div>}
            {(feature.warnings?.length ?? 0) > 0 && <div style={{ color: OPS.amber, fontSize: 10.5, lineHeight: 1.55, marginTop: 3 }}>
              注意: {feature.warnings?.join(' / ')}
            </div>}
            <div style={{ color: OPS.dim, fontSize: 10.5, lineHeight: 1.55, marginTop: 3 }}>{feature.description}</div>
            {feature.category === 'short' && <div
              data-testid={`feature-${feature.key}-funnel`}
              style={{
                display: 'flex',
                gap: 10,
                flexWrap: 'wrap',
                color: OPS.dim,
                fontFamily: OPS.mono,
                fontSize: 10,
                marginTop: 5,
              }}
            >
              {feature.eligible_instruments != null && <span>
                {feature.availability_label ?? '借株適格'} {feature.eligible_instruments}
                {feature.availability_universe_instruments != null ? `/${feature.availability_universe_instruments}` : ''}
                {feature.availability_coverage_pct != null ? ` (${feature.availability_coverage_pct}%)` : ''}
              </span>}
              {feature.latest_scan_downloaded != null && <span>
                最新価格 {feature.latest_scan_downloaded}
                {feature.latest_scan_requested != null ? `/${feature.latest_scan_requested}` : ''}
                {feature.latest_scan_coverage_pct != null ? ` (${feature.latest_scan_coverage_pct}%)` : ''}
              </span>}
              {feature.latest_candidates != null && <span>候補 {feature.latest_candidates}</span>}
              {feature.latest_shortable != null && <span>借株可 {feature.latest_shortable}</span>}
            </div>}
            {feature.category === 'short' && (feature.latest_scan_as_of || feature.latest_scan_status) && <div style={{ color: OPS.dim, fontSize: 10, marginTop: 3 }}>
              最新スキャン: {feature.latest_scan_as_of ?? '時刻不明'}
              {feature.latest_scan_status ? ` · ${feature.latest_scan_status}` : ''}
            </div>}
            {(feature.metrics?.length ?? 0) > 0 && <div style={{
              display: 'flex',
              gap: 10,
              flexWrap: 'wrap',
              color: OPS.dim,
              fontFamily: OPS.mono,
              fontSize: 10,
              marginTop: 5,
            }}>
              {feature.metrics?.map(metric => <span key={metric.label}>{metric.label} {String(metric.value ?? '—')}</span>)}
            </div>}
            {(feature.source || feature.source_as_of || feature.updated_at) && <div
              data-testid={`feature-${feature.key}-authority`}
              style={{ color: OPS.dim, fontSize: 10, marginTop: 4 }}
            >
              権威: {feature.source ?? '運用state'}
              {(feature.source_as_of || feature.updated_at) ? ` · 最終確認 ${feature.source_as_of ?? feature.updated_at}` : ''}
              {feature.source_age_hours != null ? ` · ${feature.source_age_hours}時間前` : ''}
              {feature.freshness_status ? ` · ${FRESHNESS_LABEL[feature.freshness_status] ?? feature.freshness_status}` : ''}
            </div>}
            {feature.source_note && <div style={{ color: OPS.dim, fontSize: 10, marginTop: 3 }}>
              判定境界: {feature.source_note}
            </div>}
            {!feature.mutable && feature.control_hint && <div style={{ color: OPS.blue, fontSize: 10, marginTop: 3 }}>変更方法: {feature.control_hint}</div>}
          </div>
          <div style={{ minWidth: 92, marginLeft: 'auto', textAlign: 'right' }}>
            {feature.mutable
              ? <button
                  type="button"
                  role="switch"
                  aria-checked={feature.configured_enabled}
                  aria-label={`${feature.label}を${feature.configured_enabled ? 'OFF' : 'ON'}にする`}
                  disabled={busy === feature.key}
                  onClick={() => toggle(feature)}
                  style={switchStyle(feature.configured_enabled, busy === feature.key)}
                >
                  <span style={knobStyle(feature.configured_enabled)} />
                  <span>{busy === feature.key ? '…' : feature.configured_enabled ? 'ON' : 'OFF'}</span>
                </button>
              : <span style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10 }}>参照のみ</span>}
          </div>
        </div>
      })}
    </div>
  </Panel>
}

const switchStyle = (enabled: boolean, disabled: boolean): React.CSSProperties => ({
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 7,
  minWidth: 78,
  background: enabled ? OPS.greenBg : OPS.panelAlt,
  border: `1px solid ${enabled ? OPS.green + '77' : OPS.border}`,
  borderRadius: 999,
  color: enabled ? OPS.green : OPS.dim,
  fontFamily: OPS.mono,
  fontSize: 11,
  fontWeight: 700,
  padding: '6px 10px',
  cursor: disabled ? 'wait' : 'pointer',
  opacity: disabled ? 0.65 : 1,
})

const knobStyle = (enabled: boolean): React.CSSProperties => ({
  display: 'inline-block',
  width: 8,
  height: 8,
  borderRadius: '50%',
  background: enabled ? OPS.green : OPS.dim,
  boxShadow: enabled ? `0 0 8px ${OPS.green}` : 'none',
})

'use client'

import useSWR from 'swr'
import { fetcher, type DashboardDataHealth } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, PanelTitle, Chip, Grid, Loading } from '@/components/today/ops/PageKit'
import FreshnessDots from '@/components/today/ops/FreshnessDots'

interface SystemStatus {
  generated_at: string
  data_health: DashboardDataHealth
  auto_tune: {
    mode: string; last_run?: string; last_status?: string; allowlist?: string[]
    audit?: { status?: string; issue_count?: number }
  }
  model_routes: Array<{ role: string; model: string; adapter: string }>
  guards: Record<string, number>
  feature_modes: Record<string, string>
  schedules: { auto_tune?: { timezone?: string; weekdays?: string[]; times?: string[]; source?: string } }
}

const GUARD_LABEL: Record<string, string> = {
  daily_loss_limit_pct: '日次新規停止', monthly_stage1_pct: '月次Stage 1', monthly_stage2_pct: '月次Stage 2',
  monthly_stage3_pct: '月次全停止', max_short_positions: '空売り上限', sector_rebalance_threshold_pct: 'セクター警告', sector_max_pct: 'セクター上限',
}

export default function DesignPage() {
  const { data, error, isLoading } = useSWR<SystemStatus>('/api/system/status', fetcher, { refreshInterval: 60000 })
  const systemLabel = !data ? '確認中' : data.data_health.ok ? '正常' : data.data_health.missing_count ? '障害' : 'データ遅延'
  const systemColor = !data ? OPS.dim : data.data_health.ok ? OPS.green : data.data_health.missing_count ? OPS.vermilion : OPS.amber
  const schedule = data?.schedules.auto_tune

  return <OpsPage en="SYSTEM STATUS" title="システム" subtitle="ハードコードした設計説明ではなく、現在のモデル、ガード、実行モード、データ鮮度を表示する。" right={<Chip color={systemColor} mono>{systemLabel}</Chip>}>
    {isLoading && <Loading />}
    {error && <Panel><span role="alert" style={{ color: OPS.redSoft }}>/api/system/status を取得できません。</span></Panel>}
    {data && <>
      <Grid cols={2} gap={16}>
        <Panel pad="18px 20px">
          <PanelTitle>運用状態</PanelTitle>
          <StatusRow label="データ" value={systemLabel} color={systemColor} tail={<FreshnessDots health={data.data_health} />} />
          <StatusRow label="Auto Tune" value={data.auto_tune.mode.toUpperCase()} color={data.auto_tune.mode === 'apply' ? OPS.green : data.auto_tune.mode === 'shadow' ? OPS.amber : OPS.dim} />
          <StatusRow label="Auto Tune監査" value={data.auto_tune.audit?.status ?? '—'} color={data.auto_tune.audit?.status === 'ok' ? OPS.green : OPS.amber} />
          <StatusRow label="最終Auto Tune" value={`${data.auto_tune.last_run?.slice(0, 16).replace('T', ' ') ?? '未実行'} · ${data.auto_tune.last_status ?? '—'}`} />
          <StatusRow label="Auto Tune予定" value={`${schedule?.weekdays?.join('/') ?? '—'} ${schedule?.times?.join(' · ') ?? '—'} ${schedule?.timezone ?? ''}`} />
        </Panel>
        <Panel pad="18px 20px">
          <PanelTitle>機能モード</PanelTitle>
          {Object.entries(data.feature_modes).map(([key, value]) => <StatusRow key={key} label={key} value={String(value)} color={value === 'apply' || value === 'enforce' ? OPS.green : OPS.amber} />)}
        </Panel>
      </Grid>

      <div style={{ marginTop: 22 }}><PanelTitle>現在のガード値</PanelTitle><Grid minmax={210} gap={10}>
        {Object.entries(data.guards).map(([key, value]) => <Panel key={key} pad="13px 15px"><div style={{ color: OPS.dim, fontSize: 11 }}>{GUARD_LABEL[key] ?? key}</div><div style={{ color: OPS.text, fontFamily: OPS.mono, fontSize: 18, marginTop: 5 }}>{value}{key.includes('pct') ? '%' : ''}</div><div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 9.5, marginTop: 4 }}>{key}</div></Panel>)}
      </Grid></div>

      <div style={{ marginTop: 22 }}><PanelTitle>実効モデルルーティング</PanelTitle><div style={{ overflowX: 'auto' }}><table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}><tbody>
        {data.model_routes.map((route, index) => <tr key={route.role} style={{ borderTop: index ? `1px solid ${OPS.hairline}` : 'none' }}><td style={cell}>{route.role}</td><td style={{ ...cell, color: OPS.gold, fontFamily: OPS.mono }}>{route.model}</td><td style={{ ...cell, color: OPS.dim }}>{route.adapter}</td></tr>)}
      </tbody></table></div></div>

      <p style={{ fontSize: 11, color: OPS.dim, marginTop: 24 }}>状態生成 {data.generated_at.slice(0, 19).replace('T', ' ')} · 値はAPIと運用stateから取得しています。</p>
    </>}
  </OpsPage>
}

function StatusRow({ label, value, color = OPS.sub, tail }: { label: string; value: string; color?: string; tail?: React.ReactNode }) {
  return <div style={{ display: 'grid', gridTemplateColumns: '130px minmax(0,1fr) auto', gap: 10, alignItems: 'center', padding: '9px 0', borderTop: `1px solid ${OPS.hairline}`, fontSize: 12.5 }}><span style={{ color: OPS.dim }}>{label}</span><span style={{ color, fontFamily: OPS.mono, overflowWrap: 'anywhere' }}>{value}</span>{tail}</div>
}
const cell: React.CSSProperties = { padding: '9px 8px', color: OPS.sub, verticalAlign: 'top' }

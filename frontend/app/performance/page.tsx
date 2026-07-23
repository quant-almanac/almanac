'use client'

import useSWR from 'swr'
import { fetcher, type UpgradeComparison } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { Bar, Chip, Grid, Loading, OpsPage, Panel, PanelTitle, Stat } from '@/components/today/ops/PageKit'
import type { ScoreRow, TodayOps } from '@/components/today/ops/types'

type TwrResult = {
  twr_pct?: number | null
  benchmark_twr_pct?: number | null
  excess_return_pct?: number | null
  excess_suppressed_reason?: string | null
  confirmed?: boolean
  period_days_actual?: number
  error?: string | null
}
type ObjectiveStatus = {
  as_of?: string
  twr: TwrResult
  max_dd_12m: { dd_pct?: number | null; confirmed?: boolean; period_days_actual?: number; error?: string | null }
  judgment: 'pending' | 'met' | 'not_met'
  clean_days: number
  required_days: number
  clean_since?: string
  thresholds: { excess_pct_min: number; max_dd_pct_limit: number }
}
type PolicyDecisions = { accepted_count?: number; rejected_count?: number; modified_count?: number; as_of?: string; error?: string }

const SUPPRESSED_REASON: Record<string, string> = {
  no_nav_data: 'NAV未記録',
  v_start_before_clean_since: '起点前データ',
  portfolio_twr_unconfirmed: '実測日数不足',
  benchmark_error: 'ベンチ取得失敗',
}

function pct(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

function shiftIsoDate(iso: string, days: number): string {
  const result = new Date(`${iso}T00:00:00Z`)
  result.setUTCDate(result.getUTCDate() - days)
  return result.toISOString().slice(0, 10)
}

function suppressedReason(reason?: string | null): string | null {
  if (!reason) return null
  const key = Object.keys(SUPPRESSED_REASON).find(candidate => reason.startsWith(candidate))
  return key ? SUPPRESSED_REASON[key] : reason
}

export default function PerformancePage() {
  const { data: objective, isLoading } = useSWR<ObjectiveStatus>('/api/objective-status', fetcher, { refreshInterval: 300000 })
  const asOf = objective?.as_of
  const from30 = asOf ? shiftIsoDate(asOf, 30) : null
  const cleanFrom = objective?.clean_since ?? null
  const { data: recent } = useSWR<TwrResult>(asOf && from30 ? `/api/twr?from=${from30}&to=${asOf}` : null, fetcher, { refreshInterval: 300000 })
  const { data: cleanPeriod } = useSWR<TwrResult>(asOf && cleanFrom ? `/api/twr?from=${cleanFrom}&to=${asOf}` : null, fetcher, { refreshInterval: 300000 })
  const { data: today } = useSWR<TodayOps>('/api/today', fetcher, { refreshInterval: 120000 })
  const { data: policy } = useSWR<PolicyDecisions>('/api/policy-decisions', fetcher, { refreshInterval: 300000 })
  const { data: comparison } = useSWR<UpgradeComparison>('/api/upgrade-comparison', fetcher, { refreshInterval: 300000 })

  return (
    <OpsPage en="VERIFICATION" title="検証" subtitle="クリーンなNAV履歴に限定し、超過収益と最大ドローダウンを同じ観測窓で検証する。" widthMode="wide">
      {isLoading && <Loading />}
      {objective && (
        <>
          <ObjectiveCard objective={objective} />
          <div style={{ marginTop: 18 }}><Grid minmax={280} gap={14}><PeriodPanel title="直近30日" data={recent} /><PeriodPanel title={`クリーン期間 ${cleanPeriod?.period_days_actual ?? 0}日`} data={cleanPeriod} /></Grid></div>
          <div style={{ marginTop: 20 }}><Scorecard rows={today?.scorecard.rows ?? []} /></div>
          <div style={{ marginTop: 20 }}><Grid minmax={280} gap={14}><PolicyPanel policy={policy} /><ComparisonPanel comparison={comparison} /></Grid></div>
        </>
      )}
    </OpsPage>
  )
}

function ObjectiveCard({ objective }: { objective: ObjectiveStatus }) {
  const pending = objective.judgment === 'pending'
  const met = objective.judgment === 'met'
  const color = pending ? OPS.sub : met ? OPS.green : OPS.vermilion
  const title = pending ? `判定待ち — クリーン期間 ${objective.clean_days}/${objective.required_days}日（起点 ${objective.clean_since ?? '—'}）` : met ? '目標達成' : '目標未達'
  const reason = suppressedReason(objective.twr.excess_suppressed_reason)
  const neutral = pending ? OPS.sub : color

  return (
    <Panel pad="18px 20px" style={{ borderColor: `${color}66` }}>
      <PanelTitle right={<Chip color={color} bg={pending ? OPS.dimBg : met ? OPS.greenBg : OPS.vermilionBg} mono>{pending ? 'PENDING' : met ? 'MET' : 'NOT MET'}</Chip>}>365日目標判定</PanelTitle>
      <div style={{ color, fontSize: 18, fontWeight: 700, marginBottom: 10 }}>{title}</div>
      {pending && <Bar pct={Math.min(100, objective.clean_days / objective.required_days * 100)} color={OPS.sub} height={7} />}
      <div style={{ marginTop: 15 }}><Grid minmax={150} gap={10}>
        <Stat label="Portfolio TWR" value={pct(objective.twr.twr_pct)} color={neutral} />
        <Stat label="Benchmark" value={pct(objective.twr.benchmark_twr_pct)} color={neutral} />
        <Stat label="Excess α" value={pct(objective.twr.excess_return_pct)} color={neutral} sub={reason ?? undefined} />
        <Stat label="最大DD" value={pct(objective.max_dd_12m.dd_pct)} color={neutral} sub={`下限 ${objective.thresholds.max_dd_pct_limit}%`} />
      </Grid></div>
    </Panel>
  )
}

function PeriodPanel({ title, data }: { title: string; data?: TwrResult }) {
  return (
    <Panel pad="15px 17px">
      <PanelTitle>{title}</PanelTitle>
      {!data ? <p style={{ color: OPS.dim, fontSize: 12.5, margin: 0 }}>参照データを読み込み中…</p> : data.error ? <p style={{ color: OPS.sub, fontSize: 12.5, lineHeight: 1.6, margin: 0 }}>{data.error}</p> : <Grid minmax={130} gap={8}>
        <Stat label="TWR" value={pct(data.twr_pct)} color={OPS.sub} />
        <Stat label="Benchmark" value={pct(data.benchmark_twr_pct)} color={OPS.sub} />
        <Stat label="Excess α" value={pct(data.excess_return_pct)} color={OPS.sub} sub={suppressedReason(data.excess_suppressed_reason) ?? undefined} />
      </Grid>}
    </Panel>
  )
}

function Scorecard({ rows }: { rows: ScoreRow[] }) {
  return (
    <Panel pad="15px 17px">
      <PanelTitle right={`${rows.length} rows`}>信頼度スコアカード</PanelTitle>
      {rows.length === 0 ? <p style={{ color: OPS.dim, fontSize: 12.5, margin: 0 }}>計測結果はまだありません。</p> : <div style={{ overflowX: 'auto' }}><table style={{ width: '100%', minWidth: 700, borderCollapse: 'collapse', fontSize: 12.5 }}><thead><tr style={{ color: OPS.dim, textAlign: 'left' }}><th style={th}>agent</th><th style={th}>role</th><th style={th}>n</th><th style={th}>win rate</th><th style={th}>excess bps</th><th style={th}>payoff</th></tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.agent}-${row.role}-${index}`} style={{ borderTop: `1px solid ${OPS.hairline}`, color: row.measured === false ? OPS.dim : OPS.sub }}><td style={td}>{row.agent}</td><td style={td}>{row.role}</td><td style={td}>{row.n ?? '—'}</td><td style={td}>{row.win_rate != null ? `${(row.win_rate * 100).toFixed(1)}%` : '—'}</td><td style={td}>{row.excess_bps ?? '—'}</td><td style={td}>{row.payoff ?? '—'}</td></tr>)}</tbody></table></div>}
    </Panel>
  )
}

function PolicyPanel({ policy }: { policy?: PolicyDecisions }) {
  return <Panel pad="15px 17px"><PanelTitle>政策判定</PanelTitle>{!policy ? <p style={empty}>読み込み中…</p> : policy.error ? <p style={empty}>{policy.error}</p> : <Grid minmax={120} gap={8}><Stat label="accepted" value={`${policy.accepted_count ?? 0}`} color={OPS.green} /><Stat label="rejected" value={`${policy.rejected_count ?? 0}`} color={OPS.vermilion} /><Stat label="modified" value={`${policy.modified_count ?? 0}`} color={OPS.amber} /></Grid>}</Panel>
}

function ComparisonPanel({ comparison }: { comparison?: UpgradeComparison }) {
  const rows = Object.entries(comparison?.comparison ?? {})
  return <Panel pad="15px 17px"><PanelTitle right={comparison?.period ? `${comparison.period.start}–${comparison.period.end}` : undefined}>手法比較</PanelTitle>{rows.length === 0 ? <p style={empty}>{comparison?.error ?? 'バックテスト結果はありません。'}</p> : <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>{rows.map(([name, row]) => <div key={name} style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) auto auto', gap: 10, borderTop: `1px solid ${OPS.hairline}`, paddingTop: 7, fontFamily: OPS.mono, fontSize: 11.5 }}><span style={{ color: OPS.sub }}>{name}</span><span style={{ color: OPS.green }}>Sharpe {row.sharpe.toFixed(2)}</span><span style={{ color: OPS.dim }}>DD {(row.max_dd * 100).toFixed(1)}%</span></div>)}</div>}</Panel>
}

const th: React.CSSProperties = { padding: '7px 8px', fontWeight: 400 }
const td: React.CSSProperties = { padding: '8px', fontFamily: OPS.mono }
const empty: React.CSSProperties = { color: OPS.dim, fontSize: 12.5, margin: 0 }

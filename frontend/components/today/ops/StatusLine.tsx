'use client'
import { OPS, STANCE_LABEL, fmtAge } from './tokens'
import type { Command, TodayOps } from './types'
import FreshnessDots from './FreshnessDots'

/**
 * ステータスライン — チップをやめ、1行のモノスペース文で市場状態を静かに示す。
 */
export default function StatusLine({ command, asOf, snapshot }: { command: Command; asOf?: string; snapshot: TodayOps['snapshot_meta'] }) {
  const g = command.guard
  const guardOk = g.new_entry_allowed !== false && g.trading_allowed !== false && g.alerts.length === 0
  const stale = (command.data_age_hours ?? 0) > 24
  const dailyPct = g.daily_pnl_pct != null ? g.daily_pnl_pct * 100 : null

  const parts: React.ReactNode[] = []
  const push = (node: React.ReactNode, key: string) => {
    if (parts.length > 0) parts.push(<span key={`sep-${key}`} style={{ color: OPS.dim, margin: '0 10px' }}>·</span>)
    parts.push(<span key={key}>{node}</span>)
  }

  if (command.scenario) push(<span style={{ color: OPS.green }}>{command.scenario}</span>, 'scenario')
  if (command.vix != null) push(<>VIX {command.vix.toFixed(1)}</>, 'vix')
  if (command.yield_10y != null) push(<>10Y {command.yield_10y.toFixed(2)}%</>, '10y')
  if (command.stance) push(<>スタンス {STANCE_LABEL[command.stance] ?? command.stance}</>, 'stance')
  push(
    guardOk ? (
      <span style={{ color: OPS.green }}>ガード ✓</span>
    ) : (
      <span style={{ color: OPS.vermilion }} title={g.alerts.join(' / ')}>ガード ⚠ {g.alerts.length}</span>
    ),
    'guard'
  )
  if (dailyPct != null)
    push(
      <span style={{ color: dailyPct >= 0 ? OPS.green : OPS.redSoft }}>
        日次 {dailyPct >= 0 ? '+' : ''}{dailyPct.toFixed(2)}%
      </span>,
      'daily'
    )
  if (command.usd_ratio_pct != null && command.usd_target_pct != null)
    push(<>USD {command.usd_ratio_pct.toFixed(1)}%（目標 {command.usd_target_pct}%）</>, 'usd')

  return (
    <div
      style={{
        position: 'sticky',
        top: 54,
        zIndex: 40,
        display: 'flex',
        alignItems: 'center',
        padding: '8px 20px',
        background: 'rgba(11, 13, 18, 0.95)',
        backdropFilter: 'blur(12px)',
        WebkitBackdropFilter: 'blur(12px)',
        borderBottom: `1px solid ${OPS.hairline}`,
        fontFamily: OPS.mono,
        fontSize: 13,
        color: OPS.sub,
        letterSpacing: '0.02em',
      }}
    >
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{parts}</span>
      <span style={{ marginLeft: 'auto', flexShrink: 0, color: stale ? OPS.amber : OPS.dim }}>
        {/* 狭幅は経過時間のみ。日付詳細は広幅で */}
        <span className="hidden md:inline">分析 {asOf ?? '—'}（{fmtAge(command.data_age_hours)}）</span>
        <span className="md:hidden">{fmtAge(command.data_age_hours)}</span>
      </span>
      <span style={{ marginLeft: 12, flexShrink: 0 }}><FreshnessDots health={snapshot.data_health} /></span>
    </div>
  )
}

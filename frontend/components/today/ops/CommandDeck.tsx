'use client'

import { useState, type ReactNode } from 'react'
import Hero from './Hero'
import { Chip } from './PageKit'
import { ExecutionPlanModal } from './PlanRail'
import { OPS, fmtJpy } from './tokens'
import type { ExecutionPlan, TodayOps } from './types'

const COMMAND_DECK_CSS = `
.command-deck-grid { display:grid; grid-template-columns:minmax(0,1fr) 400px; gap:28px; align-items:stretch; }
.command-metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px; }
.command-decision-card { animation:commandDecisionBreath 3.6s ease-in-out infinite; }
.command-metric { transition:transform .18s ease,border-color .18s ease,background .18s ease; }
.command-metric:hover { transform:translateY(-2px); border-color:${OPS.gold}55 !important; background:${OPS.panelAlt} !important; }
@keyframes commandDecisionBreath { 0%,100% { box-shadow:0 0 0 rgba(201,167,93,0); } 50% { box-shadow:0 0 22px rgba(201,167,93,0.07); } }
@container ops-content (min-width: 1600px) { .command-deck-grid { grid-template-columns:minmax(0,1fr) 460px; gap:36px; } }
@container ops-content (max-width: 900px) { .command-deck-grid { grid-template-columns:1fr; gap:16px; } }
@container ops-content (max-width: 520px) { .command-metrics { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media (prefers-reduced-motion:reduce) { .command-decision-card { animation:none; } .command-metric { transition:none; } }
`

function decisionColor(plan?: ExecutionPlan): string {
  if (plan?.today_decision.code === 'actions_available') return OPS.green
  if (plan?.today_decision.code === 'disabled' || plan?.today_decision.code === 'warning') return OPS.amber
  return OPS.gold
}

export default function CommandDeck({ data, children }: { data: TodayOps; children?: ReactNode }) {
  const [planOpen, setPlanOpen] = useState(false)
  const plan = data.execution_plan
  const operational = data.command.operational_stance
  const color = decisionColor(plan)
  const guard = data.command.guard
  const guardOk = guard.new_entry_allowed !== false && guard.trading_allowed !== false && guard.alerts.length === 0
  const needsAction = data.board.filter(row => !['placed', 'filled', 'cancelled', 'expired', 'reprice_required'].includes(row.lifecycle.status)).length
  const planPct = plan?.consumption.normal_plan_budget_consumed_pct
  const scenario = data.scenario_summary

  return (
    <section aria-label="今日の指令" style={{ paddingTop: 4 }}>
      <style dangerouslySetInnerHTML={{ __html: COMMAND_DECK_CSS }} />
      <div className="command-deck-grid">
        <Hero data={data} />
        <aside className="command-decision-card" style={{ background: OPS.panel, border: `1px solid ${color}55`, borderTop: `2px solid ${color}`, borderRadius: 10, padding: '16px 18px', alignSelf: 'stretch' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 9 }}>
            <span style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 11.5, letterSpacing: '0.12em' }}>TODAY&apos;S DECISION</span>
            <span style={{ marginLeft: 'auto', color: guardOk ? OPS.green : OPS.vermilion, fontFamily: OPS.mono, fontSize: 11.5 }}>{guardOk ? 'GUARD OK' : `GUARD ⚠${guard.alerts.length}`}</span>
          </div>
          <div style={{ color, fontSize: 20, fontWeight: 700, lineHeight: 1.4 }}>{plan?.today_decision.label ?? (data.board.length ? '発注候補あり' : '観察継続')}</div>
          <p style={{ color: OPS.sub, fontSize: 14, lineHeight: 1.7, margin: '7px 0 14px' }}>{operational?.code && operational.code !== 'actionable' ? operational.reason : (plan?.today_decision.reason ?? data.engine.stance_reason ?? '現在の市場と保有状況を継続観測します。')}</p>

          <div className="command-metrics">
            <Metric label="ORDERS" value={`${data.board.length}`} sub={`要対応 ${needsAction}`} color={needsAction ? OPS.vermilion : OPS.sub} />
            <Metric label="PLAN" value={planPct != null ? `${planPct.toFixed(1)}%` : '—'} sub={`残 ${fmtJpy(plan?.consumption.remaining_normal_jpy)}`} color={(planPct ?? 0) >= 100 ? OPS.amber : OPS.gold} />
            <Metric label="SCENARIO" value={scenario ? `${scenario.active}/${scenario.partial}` : '—'} sub="発動 / 部分" color={scenario?.active ? OPS.vermilion : OPS.sub} />
            <Metric label="STALE" value={data.command.data_age_hours != null ? `${Math.round(data.command.data_age_hours)}h` : '—'} sub="分析経過" color={(data.command.data_age_hours ?? 0) > 24 ? OPS.amber : OPS.sub} />
          </div>

          {plan?.consumption.monthly_attribution_incomplete && <div style={{ marginTop: 9 }}><Chip color={OPS.amber} bg={OPS.amberBg} mono>月次帰属確認中</Chip></div>}
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginTop: 13, paddingTop: 10, borderTop: `1px solid ${OPS.hairline}` }}>
            <a href="#orders-section" style={{ color: needsAction ? OPS.vermilion : OPS.gold, fontFamily: OPS.mono, fontSize: 12.5, textDecoration: 'none' }}>{needsAction ? '要対応の発注を見る →' : '発注状況を見る →'}</a>
            {plan && <button type="button" onClick={() => setPlanOpen(true)} style={{ marginLeft: 'auto', background: 'none', border: 'none', color: OPS.sub, cursor: 'pointer', fontFamily: OPS.mono, fontSize: 12 }}>計画詳細</button>}
          </div>
        </aside>
      </div>
      {children && <div style={{ marginTop: 16 }}>{children}</div>}
      <ExecutionPlanModal plan={plan} open={planOpen} onClose={() => setPlanOpen(false)} />
    </section>
  )
}

function Metric({ label, value, sub, color }: { label: string; value: string; sub: string; color: string }) {
  return <div className="command-metric" style={{ minWidth: 0, background: OPS.inset, border: `1px solid ${OPS.hairline}`, borderRadius: 7, padding: '9px 10px' }}><div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5, letterSpacing: '0.08em' }}>{label}</div><div style={{ color, fontFamily: OPS.mono, fontSize: 16, fontWeight: 700, marginTop: 3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{value}</div><div style={{ color: OPS.dim, fontSize: 11.5, marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sub}</div></div>
}

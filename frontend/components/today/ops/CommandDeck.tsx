'use client'

import { useState, type ReactNode } from 'react'
import Hero from './Hero'
import AnalysisRefresh from './AnalysisRefresh'
import AllocatorAuditPanel from './AllocatorAuditPanel'
import { Chip } from './PageKit'
import { ExecutionPlanModal } from './PlanRail'
import { OPS, fmtJpy } from './tokens'
import type { ExecutionPlan, TodayOps } from './types'

const COMMAND_DECK_CSS = `
.command-deck-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(440px,1.02fr); gap:16px; align-items:stretch; }
.command-deck-grid > * { min-height:348px; }
.command-metrics { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:0; border-top:1px solid ${OPS.paperBorder}; border-bottom:1px solid ${OPS.paperBorder}; }
.command-metric { transition:border-color .15s ease,background .15s ease; }
.command-metric:hover { border-color:${OPS.paperControlBorder} !important; background:rgba(255,255,255,.58) !important; }
.command-decision { position:relative; isolation:isolate; }
.command-decision::after { content:''; position:absolute; inset:0; pointer-events:none; border-radius:inherit; background:radial-gradient(circle at 92% 8%,rgba(255,255,255,.46),transparent 34%); mix-blend-mode:soft-light; }
@container ops-content (min-width: 1600px) { .command-deck-grid { grid-template-columns:minmax(0,1fr) minmax(560px,1.08fr); gap:18px; } }
@container ops-content (max-width: 900px) { .command-deck-grid { grid-template-columns:1fr; gap:14px; } .command-deck-grid > * { min-height:auto; } }
@container ops-content (max-width: 620px) { .command-metrics { grid-template-columns:repeat(2,minmax(0,1fr)); gap:6px; border:0; } }
@media (prefers-reduced-motion:reduce) { .command-metric { transition:none; } }
`

function decisionColor(plan?: ExecutionPlan): string {
  if (plan?.today_decision.code === 'actions_available') return OPS.paperGreenInk
  if (plan?.today_decision.code === 'disabled' || plan?.today_decision.code === 'warning') return OPS.paperAmberInk
  return OPS.paperText
}

export default function CommandDeck({ data, children, onRefreshed }: { data: TodayOps; children?: ReactNode; onRefreshed?: () => void }) {
  const [planOpen, setPlanOpen] = useState(false)
  const plan = data.execution_plan
  const operational = data.command.operational_stance
  const color = decisionColor(plan)
  const guard = data.command.guard
  const guardOk = guard.new_entry_allowed !== false && guard.trading_allowed !== false && guard.alerts.length === 0
  const summary = data.decision_summary
  const noAction = data.board.length === 0
  const reviewCount = summary?.review_count ?? data.review_board?.length ?? 0
  const decisionEdge = data.board.length > 0 ? OPS.green : reviewCount > 0 ? OPS.amber : OPS.blue

  return (
    <section aria-label="今日の指令" style={{ paddingTop: 4 }}>
      <style dangerouslySetInnerHTML={{ __html: COMMAND_DECK_CSS }} />
      <div className="command-deck-grid">
        <Hero data={data} />
        <aside
          className="command-decision"
          style={{
            background: `linear-gradient(135deg,#F5F0E5 0%,${OPS.paper} 54%,#EDE4D2 100%)`,
            border: `1px solid ${OPS.paperBorder}`,
            borderTop: `3px solid ${decisionEdge}`,
            borderRadius: 12,
            padding: '22px 26px 18px',
            alignSelf: 'stretch',
            display: 'flex',
            flexDirection: 'column',
            boxShadow: `0 0 0 1px ${decisionEdge}33, 0 0 28px -8px ${decisionEdge}88`,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
            <span style={{ fontFamily: OPS.display, color: OPS.paperSealInk, fontSize: 17, fontWeight: 600, letterSpacing: '0.12em' }}>今日の判断</span>
            <span className="ops-latin" style={{ fontSize: 10, color: OPS.paperSub }}>DECISION</span>
            <span style={{ marginLeft: 'auto' }}><AnalysisRefresh compact ageHours={data.command.data_age_hours} onDone={onRefreshed} /></span>
          </div>
          <div style={{ fontFamily: OPS.display, color, fontSize: 'clamp(38px,3.8vw,58px)', fontWeight: 600, lineHeight: 1.05, letterSpacing: '.02em', paddingBottom: 15, borderBottom: `1px solid ${OPS.paperBorder}` }}>{plan?.today_decision.label ?? (data.board.length ? '発注候補あり' : '観察継続')}</div>
          <div className="command-metrics">
            <Metric paper label="候補" value={summary ? `${summary.candidate_count} → ${summary.executable_count}` : `${data.board.length}`} sub="生成 → 最終" color={OPS.paperText} />
            <Metric paper label="安全ゲート" value={`${reviewCount}`} sub="要確認" color={reviewCount > 0 ? OPS.paperAmberInk : OPS.paperText} />
            <Metric paper label="予算ゲート" value={`${plan?.summary.plan_filtered_count ?? 0}`} sub="計画除外" color={(plan?.summary.plan_filtered_count ?? 0) > 0 ? OPS.paperVermilionInk : OPS.paperText} />
            <Metric paper label="残り予算" value={fmtJpy(plan?.consumption.remaining_normal_jpy)} sub={<><span>通常枠</span>{plan?.consumption.normal_plan_budget_consumed_pct != null && <span style={{ display: 'block' }}>{plan.consumption.normal_plan_budget_consumed_pct.toFixed(1)}%</span>}</>} color={OPS.paperText} />
            <Metric paper label="GUARD" value={guardOk ? 'OK' : 'CHECK'} sub={guardOk ? '発注可否' : `警告 ${guard.alerts.length}`} color={guardOk ? OPS.paperGreenInk : OPS.paperVermilionInk} />
          </div>

          <p style={{ color: OPS.paperSub, fontSize: 13.5, lineHeight: 1.65, margin: '13px 0 12px', maxWidth: 680 }}>{operational?.code && operational.code !== 'actionable' ? operational.reason : (plan?.today_decision.reason ?? data.engine.stance_reason ?? '現在の市場と保有状況を継続観測します。')}</p>

          {plan?.consumption.monthly_attribution_incomplete && <div style={{ marginTop: 9 }}><Chip color={OPS.amber} bg={OPS.amberBg} mono>月次帰属確認中</Chip></div>}

          <div style={{ position: 'relative', zIndex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 12, marginTop: 'auto', paddingTop: 13, borderTop: `1px solid ${OPS.paperBorder}`, color: noAction ? OPS.paperText : OPS.paperGreenInk, fontFamily: OPS.brand, fontSize: 18, letterSpacing: '.06em' }}>
            <span aria-hidden style={{ fontFamily: OPS.sans }}>♢</span>
            <span>{noAction ? 'NO ACTION / DISCIPLINED' : `${data.board[0]?.ticker ?? 'ACTION'} / READY`}</span>
            <span className="sr-only">{guardOk ? 'GUARD OK' : `GUARD CHECK ${guard.alerts.length}`}</span>
            <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 14 }}>
              <a href="#orders-section" style={{ color: OPS.paperBlueInk, fontFamily: OPS.mono, fontSize: 11, textDecoration: 'none', letterSpacing: 0 }}>発注状況を見る →</a>
              {plan && <button type="button" onClick={() => setPlanOpen(true)} style={{ background: 'none', border: 'none', color: OPS.paperBlueInk, cursor: 'pointer', fontFamily: OPS.mono, fontSize: 11 }}>計画詳細</button>}
            </span>
          </div>
        </aside>
      </div>
      <AllocatorAuditPanel data={data} />
      {children && <div style={{ marginTop: 16 }}>{children}</div>}
      <ExecutionPlanModal plan={plan} open={planOpen} onClose={() => setPlanOpen(false)} />
    </section>
  )
}

function Metric({ label, value, sub, color, paper = false }: { label: string; value: string; sub: ReactNode; color: string; paper?: boolean }) {
  return <div className="command-metric" style={{ minWidth: 0, background: paper ? 'transparent' : OPS.panelAlt, border: 'none', borderLeft: `1px solid ${paper ? OPS.paperBorder : OPS.hairline}`, borderRadius: 0, padding: '10px 11px' }}><div className="ops-latin" style={{ color: paper ? OPS.paperSub : OPS.dim, fontSize: 9 }}>{label}</div><div style={{ color, fontFamily: OPS.display, fontSize: 25, fontWeight: 600, marginTop: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{value}</div><div style={{ color: paper ? OPS.paperSub : OPS.dim, fontSize: 10.5, marginTop: 3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sub}</div></div>
}

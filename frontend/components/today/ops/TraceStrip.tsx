'use client'

import { useEffect, useId, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import useSWR from 'swr'
import { fetcher, type DashboardData } from '@/lib/api'
import { OPS, STANCE_LABEL } from './tokens'
import { FreshnessPanel } from './FreshnessDots'
import { ExecutionPlanModal } from './PlanRail'
import type { TodayOps } from './types'

function funnelCount(today: TodayOps, key: string): number | null {
  return today.engine?.funnel?.find(item => item.key === key)?.count ?? null
}

function jumpTo(id: string) {
  document.getElementById(id)?.scrollIntoView()
}

const stageButton = (color: string = OPS.sub) => ({
  display: 'inline-flex', alignItems: 'center', gap: 4, flexShrink: 0,
  background: 'none', border: 'none', color, cursor: 'pointer', padding: '3px 0',
  fontFamily: OPS.mono, fontSize: 13, letterSpacing: '0.01em', textAlign: 'left' as const,
})

export default function TraceStrip({ today }: { today: TodayOps }) {
  const router = useRouter()
  const { data: dashboard } = useSWR<DashboardData>('/api/dashboard', fetcher, {
    refreshInterval: 60000,
    revalidateOnFocus: false,
  })
  const [planOpen, setPlanOpen] = useState(false)
  const [obsOpen, setObsOpen] = useState(false)
  const obsButtonRef = useRef<HTMLButtonElement>(null)
  const sectionRef = useRef<HTMLElement>(null)
  const obsDialogId = useId()
  useEffect(() => {
    if (!obsOpen) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setObsOpen(false)
        obsButtonRef.current?.focus()
      }
    }
    const onPointerDown = (event: PointerEvent) => {
      if (!sectionRef.current?.contains(event.target as Node)) setObsOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('pointerdown', onPointerDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('pointerdown', onPointerDown)
    }
  }, [obsOpen])
  const health = dashboard?.data_health
  const sources = health?.sources ? Object.values(health.sources) : []
  const okCount = sources.filter(source => source.exists !== false && !source.stale).length
  const staleCount = health?.stale_count
  const scenario = today.scenario_summary
  const tiers = funnelCount(today, 'tiers')
  const adopted = today.engine.red_team.filter(item => item.verdict !== 'reject').length
  const rejected = today.engine.red_team.filter(item => item.verdict === 'reject').length
  const filtered = today.execution_plan?.summary.plan_filtered_count
  const guard = today.command.guard
  const guardOk = guard.new_entry_allowed !== false && guard.trading_allowed !== false && guard.alerts.length === 0
  const guardWarnings = guard.alerts.length
  const boardCount = today.board.length

  return (
    <section ref={sectionRef} aria-label="判断トレース" style={{ position: 'relative', borderTop: `1px solid ${OPS.hairline}`, borderBottom: `1px solid ${OPS.hairline}`, padding: '12px 0' }}>
      {/* ポップオーバーをクリップしないよう、スクロールは内側のdivに閉じる */}
      <div style={{ overflowX: 'auto' }}>
      <div style={{ display: 'inline-flex', alignItems: 'center', gap: 10, minWidth: 'max-content', padding: '0 2px' }}>
        <button
          ref={obsButtonRef}
          type="button"
          aria-haspopup="dialog"
          aria-expanded={obsOpen}
          aria-controls={obsDialogId}
          onClick={() => setObsOpen(value => !value)}
          style={stageButton()}
        >
          <span style={{ color: OPS.dim }}>観測</span> <span style={{ color: sources.length ? OPS.green : OPS.dim }}>ok {sources.length ? okCount : '—'}</span><span style={{ color: OPS.dim }}>·</span><span style={{ color: staleCount ? OPS.amber : OPS.dim }}>停滞 {staleCount ?? '—'}</span>
        </button>
        <Arrow />
        <button type="button" onClick={() => jumpTo('rationale-section')} style={stageButton(OPS.sub)}><span style={{ color: OPS.dim }}>市場</span> {today.command.scenario ?? '—'} · VIX {today.command.vix != null ? today.command.vix.toFixed(1) : '—'} · {today.command.stance ? STANCE_LABEL[today.command.stance] ?? today.command.stance : '—'}</button>
        <Arrow />
        <button type="button" onClick={() => router.push('/scenarios')} style={stageButton(scenario?.active ? OPS.vermilion : OPS.sub)}><span style={{ color: OPS.dim }}>シナリオ</span> 発動{scenario?.active ?? '—'} · 部分{scenario?.partial ?? '—'} · 監視{scenario?.watching ?? '—'}</button>
        <Arrow />
        <button type="button" onClick={() => jumpTo('analyst-section')} style={stageButton()}><span style={{ color: OPS.dim }}>候補</span> ティア{tiers ?? '—'}</button>
        <Arrow />
        <button type="button" onClick={() => jumpTo('analyst-section')} style={stageButton()}><span style={{ color: OPS.dim }}>反証</span> 採用{adopted} / 棄却{rejected}</button>
        <Arrow />
        <button type="button" onClick={() => setPlanOpen(true)} style={stageButton(today.execution_plan ? OPS.gold : OPS.dim)}><span style={{ color: OPS.dim }}>計画ゲート</span> 除外{filtered ?? '—'}</button>
        <Arrow />
        <button type="button" onClick={() => jumpTo('orders-section')} style={stageButton(guardOk ? OPS.green : OPS.amber)}><span style={{ color: OPS.dim }}>ガード</span> {guardOk ? '✓' : `⚠${guardWarnings}`}</button>
        <Arrow />
        <button type="button" onClick={() => jumpTo('orders-section')} style={stageButton(boardCount > 0 ? OPS.gold : OPS.sub)}><span style={{ color: OPS.gold }}>◆ 結論</span> 発注{boardCount}件{boardCount === 0 && <span style={{ color: OPS.dim, maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}> · {today.execution_plan?.today_decision.reason ?? '—'}</span>}</button>
      </div>
      </div>
      {obsOpen && (
        <FreshnessPanel
          health={health}
          id={obsDialogId}
          style={{ position: 'absolute', left: 2, top: 'calc(100% + 4px)' }}
        />
      )}
      <ExecutionPlanModal plan={today.execution_plan} open={planOpen} onClose={() => setPlanOpen(false)} />
    </section>
  )
}

function Arrow() {
  return <span aria-hidden style={{ color: OPS.dim, flexShrink: 0 }}>→</span>
}

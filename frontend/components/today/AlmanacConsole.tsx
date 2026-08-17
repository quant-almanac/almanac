'use client'

import { useState } from 'react'
import Link from 'next/link'
import useSWR from 'swr'
import { MotionConfig } from 'framer-motion'
import { fetcher } from '@/lib/api'
import { OPS } from './ops/tokens'
import { OPS_MOTION_CSS } from './ops/motion'
import { ORNAMENT_CSS } from './ops/ornament'
import type { TodayOps } from './ops/types'
import StatusLine from './ops/StatusLine'
import AlmanacStrip from './ops/AlmanacStrip'
import ActionSection from './ops/ActionSection'
import SignalMap from './ops/SignalMap'
import CommandDeck from './ops/CommandDeck'
import DecisionFlow from './ops/DecisionFlow'
import PulseLine from './ops/PulseLine'
import type { RejectedDecision } from './ops/OrderMap'
import { ContentShell, SHELL_CSS } from './ops/Shell'
import BrandLoader from '@/components/BrandLoader'

/**
 * ALMANAC Console (v11) — ルート(/) と /today で共有する相場暦コンソール本体。
 * selected / hovered を集約し、ORDERS ⇔ SIGNAL MAP を双方向連動させる。
 */

// 面とモーションの定義は motion.ts (OPS_MOTION_CSS) が唯一の真実。
// ここではコンソール固有の導線（セクション間の区切り）だけを足す。
const GLOBAL_CSS = OPS_MOTION_CSS + ORNAMENT_CSS + `
.console-links a { transition: background .15s ease, border-color .15s ease; }
.console-links a:hover { background: ${OPS.panelAlt}; border-color: ${OPS.gold}77; }
@media (prefers-reduced-motion: reduce) { .console-links a { transition: none; } }
`

function hasDecisionCoordinates(item: RejectedDecision): boolean {
  return item.confidence_pct != null
    && Number.isFinite(item.confidence_pct)
    && item.confidence_pct >= 0
    && item.confidence_pct <= 100
    && item.impact_nav_pct != null
    && Number.isFinite(item.impact_nav_pct)
    && item.impact_nav_pct >= 0
}

export default function AlmanacConsole() {
  const { data, error, isLoading, mutate } = useSWR<TodayOps>('/api/today', fetcher, {
    refreshInterval: 120000,
  })
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [hoveredKey, setHoveredKey] = useState<string | null>(null)
  const selected = data ? data.board.findIndex(row => row.decision_flow_key === selectedKey) : -1
  const hovered = data ? data.board.findIndex(row => row.decision_flow_key === hoveredKey) : -1
  const rejectedDecisions: RejectedDecision[] = data ? [
    ...data.engine.red_team
      .filter(item => item.verdict === 'reject')
      .map(item => ({ ticker: item.ticker, action: item.action ?? item.hypothesis, reason: item.verdict_reason ?? item.reason, source: 'RED TEAM', verdict: item.verdict })),
    ...data.engine.lanes
      .filter(item => item.verdict !== 'adopt')
      .map(item => ({ ticker: item.ticker, action: item.lane, reason: item.verdict_reason, source: 'INFO LANE', verdict: item.verdict })),
    ...(data.execution_plan?.filtered_examples ?? []).map(item => ({
      ticker: item.ticker,
      action: item.type,
      reason: item.reason,
      source: 'PLAN GATE',
      verdict: item.code,
      confidence_pct: item.confidence_pct,
      estimated_notional_jpy: item.estimated_notional_jpy,
      impact_nav_pct: item.estimated_notional_jpy != null && data.portfolio_snapshot.total_jpy
        ? Math.round((item.estimated_notional_jpy / data.portfolio_snapshot.total_jpy) * 10000) / 100
        : undefined,
    })),
  ].filter(hasDecisionCoordinates) : []

  return (
    <MotionConfig reducedMotion="user">
    <div
        style={{
        // ClientLayout の padding を打ち消して full-bleed
        margin: 'calc(-1 * clamp(16px, 3vw, 32px)) calc(-1 * clamp(16px, 3vw, 36px))',
        background: OPS.bg,
        color: OPS.text,
        minHeight: 'calc(100vh - 72px)',
        fontFamily: OPS.sans,
        fontSize: 14,
        paddingBottom: 52,
      }}
    >
      <style dangerouslySetInnerHTML={{ __html: GLOBAL_CSS + SHELL_CSS }} />
      {data && <StatusLine command={data.command} asOf={data.as_of} snapshot={data.snapshot_meta} sessions={data.almanac?.sessions} />}

      <ContentShell widthMode="wide">
        <div
          style={{
            padding: '8px 0 40px',
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
          }}
        >
        {isLoading && <BrandLoader />}
        {error && (
          <div
            style={{
              marginTop: 24,
              padding: '20px',
              border: `1px solid ${OPS.vermilion}66`,
              borderRadius: 8,
              background: OPS.vermilionBg,
              color: OPS.redSoft,
              fontSize: 13,
              lineHeight: 1.7,
            }}
          >
            /api/today の取得に失敗。FastAPI (port 8000) の稼働を確認。
            <div style={{ fontFamily: OPS.mono, fontSize: 11, marginTop: 6, color: OPS.dim }}>{String(error)}</div>
          </div>
        )}

        {data && (
          <>
            <div className="ops-sec" style={{ animationDelay: '0ms' }}>
              <CommandDeck data={data} onRefreshed={() => { void mutate() }} />
            </div>
            <div className="ops-sec" style={{ animationDelay: '70ms' }}>
              <PulseLine command={data.command} pulse={data.pulse} benchmark={data.benchmark} />
            </div>
            <div className="ops-sec" style={{ animationDelay: '140ms' }}>
              <DecisionFlow flow={data.decision_flow} engine={data.engine}
                selectedKey={selectedKey} onSelect={setSelectedKey} />
            </div>
            {/* 旧 WeekSummaryStrip(今週の市場カレンダー)は AlmanacStrip に統合した。
                同じ日付の予定を2箇所で見ることになっていたため。 */}
            <div className="ops-sec" style={{ animationDelay: '180ms' }}>
              <AlmanacStrip almanac={data.almanac} plan={data.execution_plan} />
            </div>
            <div className="ops-sec" style={{ animationDelay: '240ms' }}>
              <ActionSection
                board={data.board}
                reviewBoard={data.review_board ?? []}
                notes={data.board_notes}
                charts={data.charts}
                backlog={data.backlog}
                executionPlan={data.execution_plan}
                selected={selected}
                hovered={hovered >= 0 ? hovered : null}
                onSelect={index => setSelectedKey(data.board[index]?.decision_flow_key ?? null)}
                onHover={index => setHoveredKey(index == null ? null : data.board[index]?.decision_flow_key ?? null)}
                rejectedDecisions={rejectedDecisions}
                pendingPortfolioApplications={data.pending_portfolio_applications ?? []}
                selectedKey={selectedKey}
                onDecisionSelect={setSelectedKey}
              />
            </div>
            <div id="rationale-section" className="ops-sec" style={{ animationDelay: '280ms' }}>
              <SignalMap
                engine={data.engine}
                board={data.board}
                charts={data.charts}
                delta={data.delta}
                benchmark={data.benchmark}
              />
            </div>
            <div className="ops-sec console-links" style={{ animationDelay: '320ms', display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 10 }}>
              <TodayLink href="/portfolio" label="資産の詳細" value={`¥${Math.round((data.portfolio_snapshot.total_jpy ?? 0) / 1_000_000 * 10) / 10}M · 現金 ${data.portfolio_snapshot.total_jpy ? (((data.portfolio_snapshot.cash_total_jpy ?? data.portfolio_snapshot.cash_jpy ?? 0) / data.portfolio_snapshot.total_jpy) * 100).toFixed(1) : '—'}%`} />
              <TodayLink href="/agent" label="AI分析全文" value={`${Object.keys(data.report ?? {}).length} レーン`} />
              <TodayLink href="/executions" label="執行履歴" value="注文・約定・取消" />
              <TodayLink href="/performance" label="検証" value="成績・信頼度" />
            </div>
            <p style={{ fontSize: 11, color: OPS.dim, lineHeight: 1.7, margin: 0 }}>
              本ページは参考情報。最終判断は本人の投資ルールに依る。データ時刻 {data.as_of ?? '—'} · 生成{' '}
              {data.generated_at}
            </p>
          </>
        )}
        </div>
      </ContentShell>

    </div>
    </MotionConfig>
  )
}

function TodayLink({ href, label, value }: { href: string; label: string; value: string }) {
  const icon = href === '/portfolio' ? '◔' : href === '/agent' ? '∿' : href === '/executions' ? '▤' : '◎'
  return <Link href={href} className="ops-elev" style={{ textDecoration: 'none', borderRadius: 9, padding: '11px 13px', display: 'grid', gridTemplateColumns: '34px minmax(0,1fr) auto', alignItems: 'center', gap: 10 }}>
    <span aria-hidden="true" style={{ width: 34, height: 34, display: 'grid', placeItems: 'center', border: `1px solid ${OPS.border}`, borderRadius: 8, color: OPS.gold, fontFamily: OPS.brand, fontSize: 19, background: OPS.panelAlt }}>{icon}</span>
    <span style={{ minWidth: 0 }}>
      <span style={{ display: 'block', color: OPS.text, fontFamily: OPS.display, fontSize: 13, fontWeight: 600 }}>{label}</span>
      <span style={{ display: 'block', color: OPS.dim, fontFamily: OPS.mono, fontSize: 10, marginTop: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{value}</span>
    </span>
    <span style={{ color: OPS.gold }}>›</span>
  </Link>
}

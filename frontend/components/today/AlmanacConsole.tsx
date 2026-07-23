'use client'

import { useState } from 'react'
import Link from 'next/link'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS } from './ops/tokens'
import type { TodayOps } from './ops/types'
import StatusLine from './ops/StatusLine'
import AlmanacStrip from './ops/AlmanacStrip'
import ActionSection from './ops/ActionSection'
import SignalMap from './ops/SignalMap'
import CommandDeck from './ops/CommandDeck'
import type { RejectedDecision } from './ops/OrderMap'
import { ContentShell, SHELL_CSS } from './ops/Shell'

/**
 * ALMANAC Console (v11) — ルート(/) と /today で共有する相場暦コンソール本体。
 * selected / hovered を集約し、ORDERS ⇔ SIGNAL MAP を双方向連動させる。
 */

const GLOBAL_CSS = `
@keyframes opsFadeUp { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
.ops-sec { animation: opsFadeUp .55s ease both; }
.ops-card { transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease; }
.ops-card:hover { transform: translateY(-2px); border-color: rgba(201,167,93,0.55) !important; box-shadow: 0 6px 18px rgba(0,0,0,0.35); }
.ops-row { transition: background .15s ease; }
.ops-row:hover { background: rgba(201,167,93,0.06); }
@keyframes opsLinkPulse { 0%,100% { box-shadow: 0 0 0 0 rgba(201,167,93,0.0); } 50% { box-shadow: 0 0 0 3px rgba(201,167,93,0.18); } }
.ops-linked { animation: opsLinkPulse 1.6s ease-in-out infinite; }
@keyframes opsModalIn { from { opacity: 0; transform: translateY(10px) scale(.98); } to { opacity: 1; transform: none; } }
@keyframes opsBackdropIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes opsBarGrow { from { transform: scaleX(0); } to { transform: scaleX(1); } }
.ops-bar-fill { transform-origin: left; animation: opsBarGrow .7s cubic-bezier(.4,0,.2,1) both; }
@media (prefers-reduced-motion: reduce) {
  .ops-sec, .ops-bar-fill { animation: none; }
  .ops-card, .ops-row { transition: none; }
  .ops-card:hover { transform: none; }
  .ops-linked { animation: none; }
}
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
  const { data, error, isLoading } = useSWR<TodayOps>('/api/today', fetcher, {
    refreshInterval: 120000,
  })
  const [selected, setSelected] = useState(0)
  const [hovered, setHovered] = useState<number | null>(null)
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
    <div
        style={{
        // ClientLayout の padding を打ち消して full-bleed
        margin: 'calc(-1 * clamp(16px, 3vw, 32px)) calc(-1 * clamp(16px, 3vw, 36px))',
        background: OPS.bg,
        color: OPS.text,
        minHeight: 'calc(100vh - 54px)',
        fontFamily: OPS.sans,
        fontSize: 14,
        paddingBottom: 52,
      }}
    >
      <style dangerouslySetInnerHTML={{ __html: GLOBAL_CSS + SHELL_CSS }} />
      {data && <StatusLine command={data.command} asOf={data.as_of} snapshot={data.snapshot_meta} />}

      <ContentShell widthMode="wide">
        <div
          style={{
            padding: '0 24px 48px',
            display: 'flex',
            flexDirection: 'column',
            gap: 36,
          }}
        >
        {isLoading && (
          <div
            style={{
              padding: '80px 0',
              textAlign: 'center',
              color: OPS.dim,
              fontFamily: OPS.dot,
              letterSpacing: '0.24em',
              fontSize: 14,
            }}
          >
            ALMANAC LOADING…
          </div>
        )}
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
              <CommandDeck data={data} />
            </div>
            <div className="ops-sec" style={{ animationDelay: '70ms' }}>
              <AlmanacStrip almanac={data.almanac} plan={data.execution_plan} />
            </div>
            <div className="ops-sec" style={{ animationDelay: '140ms' }}>
              <ActionSection
                board={data.board}
                reviewBoard={data.review_board ?? []}
                notes={data.board_notes}
                charts={data.charts}
                backlog={data.backlog}
                executionPlan={data.execution_plan}
                selected={selected}
                hovered={hovered}
                onSelect={setSelected}
                onHover={setHovered}
                rejectedDecisions={rejectedDecisions}
                pendingPortfolioApplications={data.pending_portfolio_applications ?? []}
              />
            </div>
            <div id="rationale-section" className="ops-sec" style={{ animationDelay: '210ms' }}>
              <SignalMap
                engine={data.engine}
                board={data.board}
                charts={data.charts}
                delta={data.delta}
                benchmark={data.benchmark}
              />
            </div>
            <div className="ops-sec" style={{ animationDelay: '280ms', display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 10 }}>
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
  )
}

function TodayLink({ href, label, value }: { href: string; label: string; value: string }) {
  return <Link href={href} style={{ textDecoration: 'none', border: `1px solid ${OPS.hairline}`, borderRadius: 7, background: OPS.panel, padding: '12px 14px', display: 'block' }}>
    <div style={{ color: OPS.gold, fontSize: 12.5, fontWeight: 600 }}>{label} →</div>
    <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5, marginTop: 5 }}>{value}</div>
  </Link>
}

'use client'

import Link from 'next/link'
import { Fragment, useEffect, useMemo, useState, type CSSProperties } from 'react'
import { OPS, fmtJpy } from './tokens'
import { Seal } from './ornament'
import { SectionHead } from './Shell'
import { ExecutionPlanModal } from './PlanRail'
import { scopeMismatchLine, scopeMismatchView } from './scopeMismatch'
import type { AlmanacData, AlmanacEvent, ExecutionPlan, PastTrade } from './types'

const ALMANAC_CSS = `
.almanac-overview {
  display:grid;
  grid-template-columns:minmax(0,1.9fr) minmax(280px,.72fr);
  gap:10px;
}
.almanac-overview-calendar,
.almanac-overview-plan {
  border:1px solid ${OPS.hairline};
  border-radius:10px;
  background:${OPS.panel};
  overflow:hidden;
}
.almanac-overview-week {
  display:grid;
  grid-template-columns:88px repeat(7,minmax(0,1fr));
  min-height:74px;
}
.almanac-overview-week + .almanac-overview-week { border-top:1px solid ${OPS.hairline}; }
.almanac-overview-label { padding:10px; border-right:1px solid ${OPS.hairline}; background:${OPS.panelAlt}; }
.almanac-overview-day { min-width:0; padding:8px 7px; border-right:1px solid ${OPS.hairline}; }
.almanac-overview-day:last-child { border-right:0; }
.almanac-overview-day.is-today { box-shadow:inset 0 2px 0 ${OPS.gold}; }
.almanac-heat-note { padding:7px 10px; border-top:1px solid ${OPS.hairline}; color:${OPS.dim};
  font-family:${OPS.sans}; font-size:9.5px; line-height:1.5; }
.almanac-heat-note b { color:${OPS.sub}; font-weight:600; }
.almanac-day-items { display:flex; flex-direction:column; gap:2px; margin-top:6px; min-height:26px; }
.almanac-day-item { display:flex; align-items:center; gap:4px; min-width:0; color:${OPS.text};
  font-size:10px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.almanac-day-item i { flex:none; width:4px; height:4px; border-radius:50%; }
.almanac-day-item b { margin-left:auto; flex:none; color:${OPS.dim}; font-family:${OPS.mono}; font-size:8.5px; font-weight:400; }
.almanac-day-empty { color:${OPS.dim}; font-size:10px; }
.almanac-toggle { border:1px solid ${OPS.border}; border-radius:999px; background:transparent; color:${OPS.gold}; padding:4px 10px; font:10.5px ${OPS.mono}; cursor:pointer; }
.almanac-toggle:hover { border-color:${OPS.gold}; background:${OPS.goldBg}; }
.market-clock {
  margin-top: 6px;
  border: 1px solid ${OPS.border};
  border-radius: 10px;
  background: ${OPS.panel};
  padding: 15px 17px 16px;
}
.market-clock-summary {
  display: grid;
  grid-template-columns: minmax(230px, 1.25fr) repeat(2, minmax(170px, .8fr));
  gap: 9px;
  margin-bottom: 15px;
}
.market-clock-card {
  min-width: 0;
  border: 1px solid ${OPS.hairline};
  border-radius: 8px;
  background: ${OPS.panelAlt};
  padding: 11px 13px;
}
.market-clock-lane {
  display: grid;
  grid-template-columns: 104px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  min-height: 27px;
}
.market-clock-track {
  position: relative;
  height: 18px;
  border-radius: 4px;
  background-color: ${OPS.sunken};
  background-image: linear-gradient(to right, ${OPS.hairline} 1px, transparent 1px);
  background-size: 12.5% 100%;
}
.almanac-board-scroll {
  overflow-x: auto;
  overscroll-behavior-inline: contain;
  margin-top: 20px;
  padding-bottom: 4px;
}
.almanac-board { min-width: 1120px; position: relative; }

/* 見出しと本体で同じ2カラム。右カラムは週次カード or 月次スタックが入る */
.almanac-board-head,
.almanac-board-body {
  display: grid;
  grid-template-columns: minmax(700px, 1.8fr) minmax(390px, 1fr);
}
.almanac-board-head { gap: 8px; margin-bottom: 7px; align-items: end; }
.almanac-board-body { gap: 7px 8px; align-items: stretch; }

.month-budget-meter {
  position: relative;
  height: 8px;
  margin-top: 9px;
  border-radius: 8px;
  background: ${OPS.sunken};
  overflow: hidden;
}
.month-budget-meter > i { position: absolute; top: 0; bottom: 0; display: block; }
.calendar-head,
.calendar-week {
  display: grid;
  grid-template-columns: 118px minmax(0, 1fr);
  gap: 4px;
  min-width: 0;
}
.calendar-head { align-items: end; }
.week-intel-head {
  display: grid;
  grid-template-columns: minmax(0, 1.12fr) minmax(0, .88fr);
  border: 1px solid ${OPS.hairline};
  border-radius: 8px 8px 0 0;
  overflow: hidden;
}
.week-intel-head > div {
  padding: 7px 10px;
  color: ${OPS.sub};
  font-family: ${OPS.mono};
  font-size: 10.5px;
  letter-spacing: .12em;
}
.week-intel-head > div + div { border-left: 1px solid ${OPS.hairline}; }
.week-meta {
  position: relative;
  min-width: 0;
  min-height: 102px;
  border: 1px solid ${OPS.hairline};
  border-radius: 8px;
  background: ${OPS.panel};
  padding: 9px 8px 9px 43px;
  overflow: hidden;
}
.week-node {
  position: absolute;
  left: 8px;
  top: 11px;
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border: 1px solid ${OPS.border};
  border-radius: 50%;
  background: ${OPS.panelAlt};
  color: ${OPS.blue};
  font: 10.5px ${OPS.mono};
}
.calendar-week.is-current .week-meta { border-color: ${OPS.gold}; background: ${OPS.goldBg}; }
.calendar-week.is-current .week-node { border-color: ${OPS.gold}; color: ${OPS.gold}; }
.week-intelligence-card {
  position: relative;
  min-width: 0;
  min-height: 102px;
  display: grid;
  grid-template-columns: minmax(0, 1.12fr) minmax(0, .88fr);
  border: 1px solid ${OPS.hairline};
  border-radius: 8px;
  background: ${OPS.panel};
  overflow: hidden;
  transition: border-color .15s ease, background .15s ease;
}
.week-intelligence-card:hover { border-color: ${OPS.border}; background: ${OPS.panelAlt}; }
.week-intelligence-card.is-current { border-color: ${OPS.gold}88; }

/* ── 月次計画スタック ────────────────────────────────
   未来週の右カラムには「まだ無い週次計画」の空枠しか出せず無意味だった。
   その領域をまとめて1ブロックにし、月次の4項目を縦に積む。 */
.monthly-stack {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  border: 1px solid ${OPS.blue}66;
  border-radius: 8px;
  background: ${OPS.panel};
  /* 行高を固定したので、想定外に中身が伸びた場合はここで逃がす */
  overflow: auto;
}
.monthly-stack-head {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  padding: 9px 12px;
  background: ${OPS.panelAlt};
  border-bottom: 1px solid ${OPS.hairline};
}
/* flex:1 + justify-content:center にすると、内容が割当高を超えたときに
   はみ出して次の区画と重なる。内容なりの高さにして、溢れは stack 側で流す。 */
.monthly-sec {
  flex: 0 0 auto;
  min-width: 0;
  padding: 9px 12px;
}
.monthly-sec + .monthly-sec { border-top: 1px solid ${OPS.hairline}; }
.monthly-sec-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; }
.monthly-kpi-grid,
.monthly-guard-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px;
}
.monthly-kpi,
.monthly-guard {
  min-width: 0;
  border: 1px solid ${OPS.hairline};
  border-radius: 6px;
  background: ${OPS.panelAlt};
  padding: 5px 7px;
}
.monthly-mix-bar {
  display: flex;
  height: 8px;
  margin-top: 7px;
  border-radius: 8px;
  background: ${OPS.sunken};
  overflow: hidden;
}
.monthly-priority-list { display: flex; flex-direction: column; }
.monthly-priority {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  gap: 7px;
  align-items: center;
  min-width: 0;
  padding: 3px 0;
}
.week-plan-panel,
.week-result-panel { position: relative; min-width: 0; padding: 9px 10px; }
.week-result-panel { border-left: 1px solid ${OPS.hairline}; }
.week-plan-button {
  display: block;
  width: 100%;
  color: inherit;
  background: none;
  border: 0;
  padding: 0;
  text-align: left;
  cursor: pointer;
}
.plan-item-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 7px;
  align-items: center;
  margin-top: 5px;
}
.plan-item-meter {
  height: 3px;
  margin-top: 4px;
  border-radius: 3px;
  background: ${OPS.sunken};
  overflow: hidden;
}
.plan-item-meter > i { display: block; height: 100%; border-radius: 3px; background: ${OPS.green}; }
.week-result-meter {
  position: relative;
  height: 8px;
  margin-top: 8px;
  border-radius: 8px;
  background: ${OPS.sunken};
  overflow: hidden;
}
.week-result-meter::after {
  content: '';
  position: absolute;
  left: 50%;
  top: 0;
  bottom: 0;
  width: 1px;
  background: ${OPS.dim};
}
.almanac-days {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 4px;
  min-width: 0;
}
.almanac-cell {
  position: relative;
  min-width: 0;
  min-height: 102px;
  border-radius: 8px;
  padding: 7px 8px;
  animation: almanacCellIn .38s cubic-bezier(.22,.8,.3,1) both;
  transition: border-color .15s ease, background .15s ease;
}
.almanac-cell:hover,
.almanac-cell:focus-visible { outline: none; border-color: ${OPS.gold} !important; }
@keyframes almanacCellIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@container ops-content (max-width: 1180px) {
  .market-clock-summary { grid-template-columns: 1fr 1fr; }
  .market-clock-summary .market-clock-card:first-child { grid-column: 1 / -1; }
}
@container ops-content (max-width: 760px) {
  .almanac-overview { grid-template-columns:1fr; }
  .almanac-overview-week { grid-template-columns:64px repeat(7,minmax(55px,1fr)); min-width:520px; }
  .almanac-overview-calendar { overflow-x:auto; }
  .market-clock-summary { grid-template-columns: 1fr; }
  .market-clock-summary .market-clock-card:first-child { grid-column: auto; }
  .market-clock-lane { grid-template-columns: 82px minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  .almanac-cell { animation: none; }
  .week-intelligence-card { transition: none; }
}
`

const KIND_COLOR: Record<string, string> = {
  system: OPS.blue,
  analysis: OPS.gold,
  order: OPS.vermilion,
  earnings: OPS.orchid,
  nisa: OPS.green,
  policy: OPS.amber,
  reminder: OPS.blue,
}
const KIND_LABEL: Record<string, string> = {
  earnings: '決算',
  nisa: 'NISA積立',
  policy: 'ポリシー',
  order: '指値失効',
  system: 'システム',
  analysis: '統合分析',
  reminder: 'リマインド',
}
const SESSION_COLOR: Record<string, string> = {
  JP: OPS.gold,
  US: OPS.blue,
}

function dkey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function localDate(value?: string): Date {
  if (value && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [year, month, day] = value.split('-').map(Number)
    return new Date(year, month - 1, day)
  }
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), now.getDate())
}

function mondayOf(date: Date): Date {
  const result = new Date(date)
  result.setDate(date.getDate() - ((date.getDay() + 6) % 7))
  result.setHours(0, 0, 0, 0)
  return result
}

function addDays(date: Date, days: number): Date {
  const result = new Date(date)
  result.setDate(result.getDate() + days)
  return result
}

function fmtRange(start: Date, end: Date): string {
  return `${start.getMonth() + 1}/${start.getDate()}–${end.getMonth() + 1}/${end.getDate()}`
}

function isoWeekNumber(date: Date): number {
  const utc = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()))
  utc.setUTCDate(utc.getUTCDate() + 4 - (utc.getUTCDay() || 7))
  const yearStart = new Date(Date.UTC(utc.getUTCFullYear(), 0, 1))
  return Math.ceil((((utc.getTime() - yearStart.getTime()) / 86400000) + 1) / 7)
}

function monthKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
}

function isPlanWeek(week: WeekRow, plan?: ExecutionPlan): boolean {
  return Boolean(
    plan?.horizon.week_start
    && plan?.horizon.week_end
    && week.startKey <= plan.horizon.week_end
    && week.endKey >= plan.horizon.week_start
  )
}

function signedJpy(value: number): string {
  return value > 0 ? `+${fmtJpy(value)}` : value < 0 ? `−${fmtJpy(Math.abs(value))}` : fmtJpy(0)
}

/**
 * 日次売買損益のヒートマップ背景。表示期間内の最大変動を基準に濃さを決める。
 * 値が無い日は無色 — ゼロ(引き分け)と未取得を同じ見た目にしない。
 */
export function heatBackground(pnl: number | null | undefined, scale: number): string | undefined {
  if (pnl == null || !Number.isFinite(pnl) || pnl === 0) return undefined
  const intensity = Math.min(1, Math.abs(pnl) / Math.max(1, scale))
  // 下限を置いて、小さな損益の日も「色が付いている」と分かるようにする
  const alpha = 0.07 + intensity * 0.33
  return pnl > 0
    ? `rgba(95, 211, 160, ${alpha.toFixed(3)})`
    : `rgba(240, 101, 90, ${alpha.toFixed(3)})`
}

const PLAN_OBJECTIVE_LABEL: Record<string, string> = {
  wife_nisa_growth_capacity: '妻NISA成長枠',
  add_currency_usd: 'USD不足の補正',
  'add_sector_financial-services': '金融サービス',
  'add_sector_consumer-cyclical': '一般消費財',
  'add_sector_basic-materials': '素材',
}

function planItemLabel(item: ExecutionPlan['items'][number]): string {
  return PLAN_OBJECTIVE_LABEL[item.objective ?? ''] ?? item.label
}

/**
 * ALMANAC 相場暦 — 24h market clock + week-linked plan/calendar/outcome board.
 */
export default function AlmanacStrip({ almanac, plan }: { almanac: AlmanacData; plan?: ExecutionPlan }) {
  const pnlDays = Object.values(almanac.pnl_by_date ?? {})
  const netPnl = pnlDays.reduce((sum, value) => sum + value, 0)
  const [planOpen, setPlanOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)

  return (
    <section>
      <style dangerouslySetInnerHTML={{ __html: ALMANAC_CSS }} />
      <SectionHead
        no="01"
        en="ALMANAC"
        jp="相場暦"
        note={
          <span>
            観測 {pnlDays.length}日 損益{' '}
            <span style={{ color: netPnl >= 0 ? OPS.green : OPS.redSoft }}>
              {netPnl >= 0 ? '+' : '−'}{fmtJpy(Math.abs(netPnl))}
            </span>
            {' · '}執行 {almanac.past.length}件 · 予定 {almanac.upcoming.length}件
          </span>
        }
        right={
          <button type="button" className="almanac-toggle" onClick={() => setExpanded(value => !value)}>
            {expanded ? '要約に戻す' : '複数週を詳しく見る'}
          </button>
        }
      />

      {expanded ? (
        <>
          <MarketClock almanac={almanac} />
          <WeekBoard almanac={almanac} plan={plan} onOpenPlan={() => setPlanOpen(true)} />
        </>
      ) : (
        <AlmanacOverview almanac={almanac} plan={plan} onOpenPlan={() => setPlanOpen(true)} />
      )}
      <ExecutionPlanModal plan={plan} open={planOpen} onClose={() => setPlanOpen(false)} />
    </section>
  )
}

function AlmanacOverview({ almanac, plan, onOpenPlan }: {
  almanac: AlmanacData
  plan?: ExecutionPlan
  onOpenPlan: () => void
}) {
  const today = useMemo(() => localDate(almanac.today_str), [almanac.today_str])
  // 先週から始める。相場暦は「これから」だけでなく「直前に何があったか」を
  // 読むための帳面なので、実績(トレード・日次損益)が乗る過去週を必ず含める。
  const start = useMemo(() => addDays(mondayOf(today), -7), [today])
  const weeks = useMemo(() => [0, 7, 14].map(offset => Array.from({ length: 7 }, (_, index) => addDays(start, offset + index))), [start])
  const eventMap = useMemo(() => {
    const map = new Map<string, AlmanacEvent[]>()
    for (const event of almanac.upcoming) {
      if (!event.date) continue
      map.set(event.date, [...(map.get(event.date) ?? []), event])
    }
    return map
  }, [almanac.upcoming])
  const tradeMap = useMemo(() => {
    const map = new Map<string, PastTrade[]>()
    for (const trade of almanac.past) map.set(trade.date, [...(map.get(trade.date) ?? []), trade])
    return map
  }, [almanac.past])
  const monthlyTotal = plan?.budgets.monthly_total_jpy ?? 0
  const monthlyRemaining = plan?.consumption.monthly_remaining_jpy ?? plan?.budgets.monthly_remaining_jpy ?? monthlyTotal
  const remainingPct = monthlyTotal > 0 ? Math.max(0, Math.min(100, monthlyRemaining / monthlyTotal * 100)) : 0

  // ヒートマップの濃さは「表示中の3週で一番大きく動いた日」を基準にする。
  // 全期間の最大にすると平常週がほぼ無色になり、週内の差が読めなくなる。
  const heatScale = useMemo(() => {
    const shown = weeks.flat().map(d => almanac.pnl_by_date?.[dkey(d)])
      .filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
    return Math.max(1, ...shown.map(Math.abs))
  }, [weeks, almanac.pnl_by_date])

  return (
    <div className="almanac-overview">
      <div className="almanac-overview-calendar" aria-label="先週・今週・来週の相場暦">
        {weeks.map((days, weekIndex) => (
          <div className="almanac-overview-week" key={dkey(days[0])}>
            <div className="almanac-overview-label">
              <div className="ops-latin" style={{ color: weekIndex === 1 ? OPS.gold : weekIndex === 0 ? OPS.dim : OPS.blue, fontSize: 9.5 }}>
                {weekIndex === 0 ? 'LAST WEEK' : weekIndex === 1 ? 'THIS WEEK' : 'NEXT WEEK'}
              </div>
              <strong style={{ display: 'block', color: OPS.text, fontFamily: OPS.mono, fontSize: 12, marginTop: 7 }}>{fmtRange(days[0], days[6])}</strong>
            </div>
            {days.map(day => {
              const key = dkey(day)
              const events = eventMap.get(key) ?? []
              const trades = tradeMap.get(key) ?? []
              const pnl = almanac.pnl_by_date?.[key]
              const isToday = key === dkey(today)
              return (
                <div key={key} className={`almanac-overview-day${isToday ? ' is-today' : ''}`}
                  style={{ background: heatBackground(pnl, heatScale) }}
                  title={pnl == null ? undefined : `${key} 売買損益 ${signedJpy(pnl)}（入出金を除く）`}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 3, color: isToday ? OPS.gold : OPS.sub, fontFamily: OPS.mono, fontSize: 10.5 }}>
                    <span>{day.getMonth() + 1}/{day.getDate()}</span>
                    <span style={{ color: day.getDay() === 0 ? OPS.vermilion : day.getDay() === 6 ? OPS.blue : OPS.dim }}>{['日','月','火','水','木','金','土'][day.getDay()]}</span>
                  </div>
                  {/* 予定は最大2件まで。旧「今週の市場カレンダー」が別枠で出していた
                      情報をここに統合し、同じ日付を2箇所で見ないようにする。 */}
                  <div className="almanac-day-items">
                    {trades.slice(0, 2).map((trade, index) => (
                      <span key={`t${index}`} className="almanac-day-item" title={`${trade.ticker} ${trade.detail ?? ''}`}>
                        <i style={{ background: trade.side === 'buy' ? OPS.green : OPS.vermilion }} />
                        {trade.ticker}
                      </span>
                    ))}
                    {events.slice(0, Math.max(0, 2 - trades.length)).map((event, index) => (
                      <span key={`e${index}`} className="almanac-day-item" title={event.label}>
                        <i style={{ background: KIND_COLOR[event.kind] ?? OPS.sub }} />
                        {event.ticker ?? event.label}
                        {event.t && <b>{event.t}</b>}
                      </span>
                    ))}
                    {trades.length + events.length === 0 && <span className="almanac-day-empty">—</span>}
                  </div>
                  {pnl != null && <div style={{ color: pnl >= 0 ? OPS.green : OPS.redSoft, fontFamily: OPS.mono, fontSize: 9.5, marginTop: 3 }}>{signedJpy(pnl)}</div>}
                </div>
              )
            })}
          </div>
        ))}
        <div className="almanac-heat-note">
          日付の色 = その日の<b>売買損益</b>（濃さ＝表示期間内の相対的な大きさ）。
          積立・入出金は差し引き済みで、相場で動いた分だけを表す。
        </div>
      </div>
      <button type="button" className="almanac-overview-plan" onClick={onOpenPlan} style={{ color: 'inherit', textAlign: 'left', padding: 14, cursor: 'pointer' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 }}>
          <strong style={{ color: OPS.gold, fontFamily: OPS.display, fontSize: 15, letterSpacing: '.08em' }}>今月の計画</strong>
          <span style={{ color: plan?.status === 'active' ? OPS.green : OPS.amber, fontFamily: OPS.mono, fontSize: 9.5 }}>{plan?.status === 'active' ? 'ACTIVE' : '要確認'} →</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 14 }}>
          <div><span style={eyebrow}>残り予算</span><strong style={{ display: 'block', color: OPS.text, fontFamily: OPS.mono, fontSize: 18, marginTop: 4 }}>{fmtJpy(monthlyRemaining)}</strong></div>
          <div><span style={eyebrow}>優先項目</span><strong style={{ display: 'block', color: OPS.text, fontFamily: OPS.mono, fontSize: 18, marginTop: 4 }}>{plan?.summary.active_items ?? 0}</strong></div>
        </div>
        <div style={{ height: 5, borderRadius: 5, background: OPS.sunken, marginTop: 13, overflow: 'hidden' }}><i style={{ display: 'block', width: `${remainingPct}%`, height: '100%', background: OPS.blue }} /></div>
        <div style={{ color: OPS.sub, fontSize: 11, lineHeight: 1.55, marginTop: 10 }}>{plan?.today_decision.reason ?? '月次計画を確認してください。'}</div>
      </button>
    </div>
  )
}

/* ── 24h market clock ─────────────────────────────────────── */

function minutesOf(value: string): number {
  const [hours, minutes] = value.split(':').map(Number)
  return hours * 60 + minutes
}

function inSession(nowMinutes: number, start: string, end: string): boolean {
  const startMinutes = minutesOf(start)
  const endMinutes = minutesOf(end)
  return endMinutes < startMinutes
    ? nowMinutes >= startMinutes || nowMinutes < endMinutes
    : nowMinutes >= startMinutes && nowMinutes < endMinutes
}

function timeUntil(nowMinutes: number, target: string): number {
  const targetMinutes = minutesOf(target)
  const delta = targetMinutes - nowMinutes
  return delta > 0 ? delta : delta + 1440
}

function durationLabel(totalMinutes: number): string {
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return hours > 0 ? `${hours}時間${minutes ? `${minutes}分` : ''}` : `${minutes}分`
}

// ⚠️ Date.getHours()/getMinutes() はブラウザのローカルタイムゾーンを使う。
// この画面は「JST」と明示ラベルしているが、ホストのタイムゾーン設定が
// JST でない環境 (UTC 設定の CI, 海外設定の端末) では表示も
// セッション判定もずれる。日本は DST が無いので UTC+9 固定として扱える。
export function jstMinutesOfDay(date: Date): number {
  const jst = new Date(date.getTime() + 9 * 60 * 60 * 1000)
  return jst.getUTCHours() * 60 + jst.getUTCMinutes()
}

export function jstHHMM(date: Date): string {
  const jst = new Date(date.getTime() + 9 * 60 * 60 * 1000)
  return `${String(jst.getUTCHours()).padStart(2, '0')}:${String(jst.getUTCMinutes()).padStart(2, '0')}`
}

function clockSegments(start: string, end: string): Array<{ start: number; end: number }> {
  const from = minutesOf(start)
  const to = minutesOf(end)
  return to < from ? [{ start: 0, end: to }, { start: from, end: 1440 }] : [{ start: from, end: to }]
}

function MarketClock({ almanac }: { almanac: AlmanacData }) {
  const [now, setNow] = useState<Date | null>(null)
  useEffect(() => {
    const update = () => setNow(new Date())
    update()
    const timer = setInterval(update, 60000)
    return () => clearInterval(timer)
  }, [])

  const nowMinutes = now ? jstMinutesOfDay(now) : 0
  const active = now
    ? [...almanac.sessions]
      .sort((a, b) => Number(b.phase === 'regular') - Number(a.phase === 'regular'))
      .find(session => session.is_open_day !== false && inSession(nowMinutes, session.start, session.end))
    : undefined
  const nextSession = now
    ? [...almanac.sessions]
      .filter(session => session.is_open_day !== false)
      .map(session => ({ session, minutes: timeUntil(nowMinutes, session.start) }))
      .sort((a, b) => a.minutes - b.minutes)[0]
    : undefined
  const nextSystem = now
    ? almanac.today
      .filter(event => event.t && minutesOf(event.t) > nowMinutes)
      .sort((a, b) => String(a.t).localeCompare(String(b.t)))[0]
    : undefined
  const activeColor = active ? SESSION_COLOR[active.market ?? ''] ?? OPS.gold : OPS.dim
  const transitionMinutes = active && now ? timeUntil(nowMinutes, active.end) : nextSession?.minutes

  return (
    <div className="market-clock" aria-label="本日の市場タイムライン">
      <div className="market-clock-summary">
        <div className="market-clock-card" style={{ borderColor: active ? `${activeColor}66` : OPS.hairline }}>
          <div style={eyebrow}>MARKET NOW · JST {now ? jstHHMM(now) : '—'}</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginTop: 6 }}>
            <span aria-hidden style={{ width: 8, height: 8, borderRadius: '50%', background: active ? activeColor : OPS.dim, boxShadow: active ? `0 0 10px ${activeColor}` : undefined }} />
            <strong style={{ color: active ? OPS.text : OPS.sub, fontSize: 17 }}>
              {active ? `${active.label} 取引中` : '主要市場は取引時間外'}
            </strong>
          </div>
          <div style={{ color: OPS.dim, fontSize: 12, marginTop: 5, fontFamily: OPS.mono }}>
            {active ? `${active.start}–${active.end} · 終了まで ${durationLabel(transitionMinutes ?? 0)}` : nextSession ? `${nextSession.session.label} ${nextSession.session.start}開始 · あと${durationLabel(nextSession.minutes)}` : '次の市場時間を確認できません'}
          </div>
        </div>
        <div className="market-clock-card">
          <div style={eyebrow}>NEXT MARKET</div>
          <div style={{ color: OPS.text, fontSize: 14, fontWeight: 700, marginTop: 7 }}>{active ? `${active.label} 終了` : nextSession?.session.label ?? '—'}</div>
          <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 12, marginTop: 5 }}>{active?.end ?? nextSession?.session.start ?? '—'} JST</div>
        </div>
        <div className="market-clock-card">
          <div style={eyebrow}>NEXT SYSTEM PULSE</div>
          <div style={{ color: OPS.text, fontSize: 14, fontWeight: 700, marginTop: 7, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{nextSystem?.label ?? '本日の定期処理は完了'}</div>
          <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 12, marginTop: 5 }}>{nextSystem?.t ?? '—'} JST</div>
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {almanac.sessions.map(session => {
          const isActive = active === session
          const color = SESSION_COLOR[session.market ?? ''] ?? OPS.sub
          return (
            <div className="market-clock-lane" key={session.id ?? session.label}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 5, minWidth: 0 }}>
                <span style={{ color: isActive ? color : OPS.sub, fontSize: 11.5, fontFamily: OPS.mono, whiteSpace: 'nowrap' }}>{session.label}</span>
                {session.is_open_day === false && <span style={{ color: OPS.dim, fontSize: 9 }}>休場</span>}
              </div>
              <div className="market-clock-track" style={{ opacity: session.is_open_day === false ? .35 : 1 }}>
                {clockSegments(session.start, session.end).map((segment, index) => (
                  <div
                    key={index}
                    title={`${session.label} ${session.start}–${session.end}`}
                    style={{
                      position: 'absolute',
                      left: `${segment.start / 1440 * 100}%`,
                      width: `${Math.max(.45, (segment.end - segment.start) / 1440 * 100)}%`,
                      top: 2,
                      bottom: 2,
                      borderRadius: 3,
                      background: `${color}${session.phase === 'regular' ? '42' : '20'}`,
                      border: `1px solid ${color}${isActive ? 'cc' : session.phase === 'regular' ? '77' : '44'}`,
                      boxShadow: isActive ? `0 0 9px ${color}55` : undefined,
                    }}
                  />
                ))}
                {now && <div style={{ position: 'absolute', left: `${nowMinutes / 1440 * 100}%`, top: -2, bottom: -2, width: 1, background: OPS.vermilion, boxShadow: `0 0 5px ${OPS.vermilion}` }} />}
              </div>
            </div>
          )
        })}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '104px minmax(0, 1fr)', gap: 10, marginTop: 4 }}>
        <div />
        <div style={{ position: 'relative', height: 31 }}>
          {[0, 3, 6, 9, 12, 15, 18, 21, 24].map(hour => (
            <span key={hour} style={{ position: 'absolute', left: `${hour / 24 * 100}%`, transform: `translateX(${hour === 0 ? '0' : hour === 24 ? '-100%' : '-50%'})`, color: OPS.dim, fontFamily: OPS.mono, fontSize: 9.5, top: 4 }}>{String(hour).padStart(2, '0')}</span>
          ))}
          {almanac.today.filter(event => event.kind !== 'system' && event.t).map((event, index) => (
            <span key={`${event.t}-${index}`} title={`${event.t} ${event.label}`} style={{ position: 'absolute', left: `${minutesOf(event.t ?? '00:00') / 1440 * 100}%`, top: 18, width: 5, height: 5, borderRadius: '50%', transform: 'translateX(-50%)', background: KIND_COLOR[event.kind] ?? OPS.gold, boxShadow: `0 0 5px ${KIND_COLOR[event.kind] ?? OPS.gold}` }} />
          ))}
        </div>
      </div>
    </div>
  )
}

/* ── week-linked calendar board ───────────────────────────── */

interface WeekRow {
  start: Date
  end: Date
  days: Date[]
  startKey: string
  endKey: string
}

function WeekBoard({ almanac, plan, onOpenPlan }: { almanac: AlmanacData; plan?: ExecutionPlan; onOpenPlan: () => void }) {
  const [hovered, setHovered] = useState<string | null>(null)
  const today = useMemo(() => localDate(almanac.today_str), [almanac.today_str])
  const currentWeekStart = useMemo(() => mondayOf(today), [today])
  const rangeStart = useMemo(() => addDays(currentWeekStart, -14), [currentWeekStart])
  const weeks = useMemo<WeekRow[]>(() => Array.from({ length: 7 }, (_, weekIndex) => {
    const start = addDays(rangeStart, weekIndex * 7)
    const days = Array.from({ length: 7 }, (__, dayIndex) => addDays(start, dayIndex))
    const end = days[6]
    return { start, end, days, startKey: dkey(start), endKey: dkey(end) }
  }), [rangeStart])

  const eventsByDate = useMemo(() => {
    const grouped = new Map<string, AlmanacEvent[]>()
    for (const event of almanac.upcoming) {
      if (!event.date) continue
      const rows = grouped.get(event.date) ?? []
      rows.push(event)
      grouped.set(event.date, rows)
    }
    return grouped
  }, [almanac.upcoming])
  const tradesByDate = useMemo(() => {
    const grouped = new Map<string, PastTrade[]>()
    for (const trade of almanac.past ?? []) {
      const rows = grouped.get(trade.date) ?? []
      rows.push(trade)
      grouped.set(trade.date, rows)
    }
    return grouped
  }, [almanac.past])
  const pnl = almanac.pnl_by_date ?? {}
  const maxAbs = Math.max(1, ...Object.values(pnl).map(Math.abs))
  const weeklyRows = weeks.map(week => {
    const trades = (almanac.past ?? []).filter(trade => trade.date >= week.startKey && trade.date <= week.endKey)
    const pnlRows = Object.entries(pnl).filter(([date]) => date >= week.startKey && date <= week.endKey)
    return { week, trades, pnlRows, net: pnlRows.reduce((sum, [, value]) => sum + value, 0) }
  })
  const maxWeeklyAbs = Math.max(1, ...weeklyRows.map(row => Math.abs(row.net)))
  // 最初の「まだ始まっていない週」。ここから下の右カラムを月次スタックが占める。
  const firstFutureIndex = weeklyRows.findIndex(row => row.week.start > today)

  return (
    <div className="almanac-board-scroll">
      <div className="almanac-board">
        <div className="almanac-board-head">
          <div className="calendar-head">
            <div className="ops-latin" style={{ color: OPS.dim, fontSize: 10.5, padding: '0 8px 4px' }}>WEEK</div>
            <div className="almanac-days">
              {['月', '火', '水', '木', '金', '土', '日'].map((day, i) => (
                <div
                  key={day}
                  style={{
                    fontFamily: OPS.display,
                    color: i === 5 ? OPS.blue : i === 6 ? OPS.vermilion : OPS.sub,
                    fontSize: 14,
                    fontWeight: 600,
                    textAlign: 'center',
                    letterSpacing: '.14em',
                    paddingBottom: 4,
                  }}
                >
                  {day}
                </div>
              ))}
            </div>
          </div>
          <div className="week-intel-head" aria-label="各週の計画と結果">
            <div>WEEKLY PLAN · 今週の目的と予算</div>
            <div>RESULT · 週次の結果</div>
          </div>
        </div>

        {/* 未来週の行は固定高。auto のままだと月次スタックの中身の高さが
            4行に分配され、予定の無いカレンダーのマスまで間延びする。 */}
        <div
          className="almanac-board-body"
          style={{
            gridTemplateRows: weeklyRows
              // 148px × 4行 + gap で月次スタック(約600px)がちょうど収まる。
              // 過去週の行(126〜144px)ともほぼ揃うので暦が間延びしない。
              .map((row, i) => (firstFutureIndex >= 0 && i >= firstFutureIndex ? '148px' : 'auto'))
              .join(' '),
          }}
        >
          {weeklyRows.map(({ week, trades, pnlRows }, weekIndex) => {
            const isCurrent = week.startKey === dkey(currentWeekStart)
            const isFuture = firstFutureIndex >= 0 && weekIndex >= firstFutureIndex
            return (
              <Fragment key={week.startKey}>
                <div
                  className={`calendar-week${isCurrent ? ' is-current' : ''}`}
                  style={{ gridColumn: 1, gridRow: weekIndex + 1 }}
                >
                  <WeekMeta week={week} current={isCurrent} today={today} />
                  <div className="almanac-days">
                    {week.days.map((date, dayIndex) => {
                      const key = dkey(date)
                      return (
                        <DayCell
                          key={key}
                          date={date}
                          dateKey={key}
                          today={today}
                          events={eventsByDate.get(key) ?? []}
                          trades={tradesByDate.get(key) ?? []}
                          pnl={pnl[key]}
                          maxAbsPnl={maxAbs}
                          hovered={hovered === key}
                          onHover={setHovered}
                          animationIndex={weekIndex * 7 + dayIndex}
                        />
                      )
                    })}
                  </div>
                </div>
                {/* 未来週の右カラムは空の計画枠になるだけなので描かない。
                    その領域は下の月次スタックがまとめて占める。 */}
                {!isFuture && (
                  <WeeklyIntelligenceCard
                    week={week}
                    plan={plan}
                    trades={trades}
                    pnlRows={pnlRows}
                    isCurrent={isCurrent}
                    today={today}
                    maxWeeklyAbs={maxWeeklyAbs}
                    onOpenPlan={onOpenPlan}
                    style={{ gridColumn: 2, gridRow: weekIndex + 1 }}
                  />
                )}
              </Fragment>
            )
          })}
          {firstFutureIndex >= 0 && (
            <MonthlyPlanStack
              plan={plan}
              today={today}
              onOpenPlan={onOpenPlan}
              style={{ gridColumn: 2, gridRow: `${firstFutureIndex + 1} / -1` }}
            />
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 15, flexWrap: 'wrap', color: OPS.dim, fontSize: 11.5, fontFamily: OPS.mono, marginTop: 10 }}>
          <span>表示範囲 先々週〜4週先</span>
          <span><span style={{ color: OPS.green }}>▲</span> 買い <span style={{ color: OPS.vermilion }}>▼</span> 売り</span>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>日次損益 <i style={{ width: 13, height: 9, background: 'rgba(224,72,60,.4)', borderRadius: 2 }} /><i style={{ width: 13, height: 9, background: 'rgba(87,190,146,.4)', borderRadius: 2 }} /></span>
          {(['earnings', 'nisa', 'policy', 'order'] as const).map(kind => <span key={kind}><span style={{ color: KIND_COLOR[kind] }}>●</span> {KIND_LABEL[kind]}</span>)}
          <span style={{ marginLeft: 'auto' }}>月次計画 → 今週の実行 → 週次結果</span>
        </div>
      </div>
    </div>
  )
}

/** スタック内の1区画。和名の見出し + 右端に英字コード。 */
function MonthlySection({ jp, code, children }: { jp: string; code: string; children: React.ReactNode }) {
  return (
    <div className="monthly-sec">
      <div className="monthly-sec-head">
        <span style={{ fontFamily: OPS.display, color: OPS.text, fontSize: 14, fontWeight: 600, letterSpacing: '.1em' }}>{jp}</span>
        <span className="ops-latin" style={{ marginLeft: 'auto', fontSize: 9.5, color: OPS.dim }}>{code}</span>
      </div>
      {children}
    </div>
  )
}

/**
 * 月次計画スタック — 「今月の投資余力 / 現在の配分設計 / 優先配分キュー / リスク境界」。
 *
 * 置き場所の経緯: 元は4枚を未来週の行に1枚ずつ差し込んでいた（週に紐づいて見える）。
 * かといって暦の外へ出すと、未来週の右カラムに「まだ策定されていない週次計画」の
 * 空枠だけが残って無意味になる。そこで未来週の右カラムをまとめて1つの領域とし、
 * そこへ月次の4項目を縦に積む。週次カードが尽きた先が月次の話、という読み順になる。
 */
function MonthlyPlanStack({ plan, today, onOpenPlan, style }: {
  plan?: ExecutionPlan
  today: Date
  onOpenPlan: () => void
  style?: CSSProperties
}) {
  const activePlan = plan?.status === 'active'
  const monthLabel = (plan?.horizon.month ?? monthKey(today)).replace('-', '.')

  const monthlyTotal = activePlan ? plan.budgets.monthly_total_jpy ?? 0 : 0
  const monthlyRemaining = activePlan
    ? plan.consumption.monthly_remaining_jpy ?? plan.budgets.monthly_remaining_jpy ?? monthlyTotal
    : 0
  const monthlyConsumed = activePlan
    ? plan.consumption.monthly_consumed_jpy ?? Math.max(0, monthlyTotal - monthlyRemaining)
    : 0
  const consumedPct = monthlyTotal > 0 ? Math.max(0, Math.min(100, monthlyConsumed / monthlyTotal * 100)) : 0
  const weeklyNormal = activePlan ? plan.budgets.weekly_normal_jpy ?? 0 : 0
  const weeklyOpportunity = activePlan ? plan.budgets.weekly_opportunity_reserve_jpy ?? 0 : 0
  const weeklyDefensive = activePlan ? plan.budgets.weekly_defensive_reserve_jpy ?? 0 : 0
  const weeklyTotal = weeklyNormal + weeklyOpportunity + weeklyDefensive
  const unattributedCount = activePlan ? plan.consumption.unattributed_monthly_total_count ?? 0 : 0
  const unattributedNotional = activePlan ? plan.consumption.unattributed_monthly_total_notional_jpy ?? 0 : 0
  const attributionIncomplete = activePlan && (plan.consumption.monthly_attribution_incomplete === true || unattributedCount > 0)
  // スリーブ違いの建玉が計画予算を押さえている件。自動取消しない代わりに必ず見せる。
  const scopeMismatch = activePlan ? scopeMismatchView(plan.consumption) : null

  const statusLabel = activePlan ? 'ACTIVE' : plan?.status === 'disabled' ? 'DISABLED' : 'PENDING'
  const statusColor = activePlan ? OPS.green : plan?.status === 'disabled' ? OPS.amber : OPS.dim
  const allocation = [
    { label: '通常', value: weeklyNormal, color: OPS.blue },
    { label: '機会', value: weeklyOpportunity, color: OPS.vermilion },
    { label: '防御', value: weeklyDefensive, color: OPS.gold },
  ]

  return (
    <section className="monthly-stack" aria-label="今月の計画" style={style}>
      <div className="monthly-stack-head">
        <span style={{ fontFamily: OPS.display, color: OPS.gold, fontSize: 14, fontWeight: 600, letterSpacing: '.14em' }}>月次計画</span>
        <span style={{ color: OPS.sub, fontFamily: OPS.mono, fontSize: 11.5 }}>{monthLabel}</span>
        <span className="ops-latin" style={{ color: statusColor, fontSize: 9.5 }}>{statusLabel}</span>
        {activePlan && (
          <button type="button" className="ops-btn" onClick={onOpenPlan} style={{ marginLeft: 'auto', background: 'transparent', border: `1px solid ${OPS.border}`, color: OPS.gold, fontFamily: OPS.mono, fontSize: 10.5, padding: '3px 8px' }}>
            計画詳細 →
          </button>
        )}
      </div>

      {!activePlan ? (
        <div className="monthly-sec" style={{ flex: 1 }}>
          <div style={{ color: OPS.amber, fontSize: 13, fontWeight: 700 }}>月次計画を参照できません</div>
          <div style={{ color: OPS.sub, fontSize: 12, marginTop: 6 }}>
            {plan?.status === 'disabled' ? '計画レイヤー無効' : '月次計画は未策定'}
          </div>
          <div style={{ color: OPS.dim, fontSize: 11, lineHeight: 1.65, marginTop: 6 }}>
            {plan?.today_decision.reason ?? '有効な計画が生成されるまで、予算は表示専用です。'}
          </div>
        </div>
      ) : (
        <>
          <MonthlySection jp="今月の投資余力" code="CAPACITY">
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <strong style={{ color: OPS.text, fontFamily: OPS.mono, fontSize: 22, fontWeight: 700 }}>{fmtJpy(monthlyTotal)}</strong>
              <span style={{ color: OPS.dim, fontSize: 11 }}>月間枠</span>
              <span style={{ marginLeft: 'auto', color: OPS.gold, fontFamily: OPS.mono, fontSize: 11.5 }}>残 {fmtJpy(monthlyRemaining)}</span>
            </div>
            <div className="month-budget-meter" aria-label={`月次枠 ${fmtJpy(monthlyTotal)}、帰属済み ${fmtJpy(monthlyConsumed)}`}>
              <i className="ops-bar-fill" style={{ left: 0, width: `${consumedPct}%`, background: OPS.green }} />
            </div>
            <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5, marginTop: 6 }}>
              帰属済み {fmtJpy(monthlyConsumed)} / {consumedPct.toFixed(0)}%
            </div>
            {attributionIncomplete && (
              <div style={{ color: OPS.amber, fontSize: 10.5, lineHeight: 1.5, marginTop: 5 }}>
                ▲ 未帰属 {unattributedCount}件 {fmtJpy(unattributedNotional)} は未算入
              </div>
            )}
            {scopeMismatch && (
              <div style={{ color: OPS.amber, fontSize: 10.5, lineHeight: 1.5, marginTop: 5 }}>
                <div>▲ スコープ不一致 {scopeMismatch.count}件 {fmtJpy(scopeMismatch.notionalJpy)} を予約中・要確認</div>
                {scopeMismatch.records.map((record, index) => (
                  <div key={record.id ?? `${record.ticker}-${index}`}
                    style={{ color: OPS.sub, marginTop: 2 }}>
                    {scopeMismatchLine(record)}
                  </div>
                ))}
              </div>
            )}
          </MonthlySection>

          <MonthlySection jp="現在の配分設計" code="BUDGET MIX">
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 5 }}>
              {allocation.map(row => (
                <div key={row.label} className="monthly-kpi">
                  <div style={{ ...panelEyebrow, color: row.color }}>{row.label}</div>
                  <strong style={{ color: OPS.text, fontFamily: OPS.mono, fontSize: 11.5 }}>{fmtJpy(row.value)}</strong>
                </div>
              ))}
            </div>
            <div className="monthly-mix-bar" aria-label={`配分 通常 ${fmtJpy(weeklyNormal)}、機会 ${fmtJpy(weeklyOpportunity)}、防御 ${fmtJpy(weeklyDefensive)}`}>
              {allocation.map(row => (
                <i key={row.label} className="ops-bar-fill" style={{ width: `${weeklyTotal > 0 ? row.value / weeklyTotal * 100 : 0}%`, background: row.color }} />
              ))}
            </div>
            <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10, marginTop: 5 }}>今週へ切り出した予算 · 計 {fmtJpy(weeklyTotal)}</div>
          </MonthlySection>

          <MonthlySection jp="優先配分キュー" code="PRIORITY QUEUE">
            {plan.items.length > 0 ? (
              <div className="monthly-priority-list">
                {plan.items.slice(0, 3).map((item, index) => {
                  const tone = item.status === 'covered' ? OPS.green : OPS.blue
                  return (
                    <div key={item.plan_item_id ?? `${item.priority}-${item.label}`} className="monthly-priority">
                      <span style={{ color: tone, fontFamily: OPS.mono, fontSize: 10.5 }}>{item.priority ?? index + 1}.</span>
                      <span style={{ color: OPS.sub, fontSize: 11.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{planItemLabel(item)}</span>
                      <strong style={{ color: OPS.text, fontFamily: OPS.mono, fontSize: 11 }}>{fmtJpy(item.normal_budget_jpy ?? 0)}</strong>
                    </div>
                  )
                })}
              </div>
            ) : (
              <div style={{ color: OPS.dim, fontSize: 11.5 }}>優先配分はまだ登録されていません。</div>
            )}
          </MonthlySection>

          <MonthlySection jp="リスク境界" code="RISK BOUNDARY">
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 9, marginBottom: 7 }}>
              <span style={{ color: decisionColor(plan), fontSize: 12, fontWeight: 700 }}>{plan.today_decision.label}</span>
              <span style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5 }}>候補 {plan.summary.board_count} · 除外 {plan.summary.plan_filtered_count}</span>
            </div>
            <div className="monthly-guard-grid">
              {[
                { label: '通常1件上限', value: plan.budgets.max_single_normal_action_jpy },
                { label: '機会1件上限', value: plan.budgets.max_single_opportunity_action_jpy },
                { label: 'H2上限', value: plan.budgets.h2_hard_cap_jpy },
                { label: '積立残', value: plan.budgets.scheduled_contributions_remaining_jpy },
              ].map(row => (
                <div key={row.label} className="monthly-guard">
                  <div style={panelEyebrow}>{row.label}</div>
                  <strong style={{ color: row.value != null ? OPS.text : OPS.dim, fontFamily: OPS.mono, fontSize: 11.5 }}>{row.value != null ? fmtJpy(row.value) : '—'}</strong>
                </div>
              ))}
            </div>
            {plan.warnings.length > 0 && (
              // 警告は scheduled_contributions_excluded_… のような長い snake_case が来る。
              // anywhere を付けないと折り返せず枠端で切り落とされる。
              <div style={{ color: OPS.amber, fontSize: 10, lineHeight: 1.5, marginTop: 6, overflowWrap: 'anywhere' }} title={plan.warnings[0]}>▲ {plan.warnings[0]}</div>
            )}
          </MonthlySection>
        </>
      )}
    </section>
  )
}

function WeekMeta({ week, current, today }: { week: WeekRow; current: boolean; today: Date }) {
  const status = week.end < today ? 'CLOSED' : week.start > today ? 'UPCOMING' : 'IN PROGRESS'
  return (
    <div className="week-meta">
      <span className="week-node">W{isoWeekNumber(week.start)}</span>
      <div style={{ color: current ? OPS.gold : OPS.sub, fontFamily: OPS.mono, fontSize: 10.5 }}>{fmtRange(week.start, week.end)}</div>
      {current ? (
        <div style={{ marginTop: 6, fontFamily: OPS.display, fontSize: 13, fontWeight: 600, color: OPS.gold, letterSpacing: '.16em' }}>今週</div>
      ) : (
        <div className="ops-latin" style={{ color: OPS.dim, fontSize: 9, marginTop: 8 }}>{status}</div>
      )}
    </div>
  )
}

function WeeklyIntelligenceCard({ week, plan, trades, pnlRows, isCurrent, today, maxWeeklyAbs, onOpenPlan, style }: {
  week: WeekRow
  plan?: ExecutionPlan
  trades: PastTrade[]
  pnlRows: Array<[string, number]>
  isCurrent: boolean
  today: Date
  maxWeeklyAbs: number
  onOpenPlan: () => void
  style?: CSSProperties
}) {
  return (
    <div
      className={`week-intelligence-card${isCurrent ? ' is-current' : ''}`}
      aria-label={`${fmtRange(week.start, week.end)}の週次計画と結果`}
      style={style}
    >
      <WeeklyPlanPanel week={week} plan={plan} today={today} onOpen={onOpenPlan} />
      <WeeklyResultPanel trades={trades} pnlRows={pnlRows} isCurrent={isCurrent} maxWeeklyAbs={maxWeeklyAbs} />
    </div>
  )
}

function WeeklyPlanPanel({ week, plan, today, onOpen }: { week: WeekRow; plan?: ExecutionPlan; today: Date; onOpen: () => void }) {
  const activePlan = plan?.status === 'active'
  const planWeek = activePlan && isPlanWeek(week, plan)
  const past = week.end < today
  if (!plan || !planWeek) {
    const planUnavailable = Boolean(plan && !activePlan)
    return (
      <div className="week-plan-panel" style={{ opacity: past ? .62 : .78 }}>
        <div style={{ ...panelEyebrow, color: planUnavailable ? OPS.amber : OPS.blue }}>PLAN{planUnavailable ? ' · DISABLED' : ''}</div>
        <div style={{ color: planUnavailable ? OPS.amber : OPS.sub, fontSize: 12, fontWeight: 700, marginTop: 8 }}>{planUnavailable ? '計画レイヤー無効' : past ? '計画履歴なし' : '週次計画は未策定'}</div>
        <div style={{ color: OPS.dim, fontSize: 10.5, lineHeight: 1.55, marginTop: 5 }}>{planUnavailable ? plan?.today_decision.reason : past ? '当時の計画スナップショットは保存されていません。' : '月次残枠と前週の消化後に配分します。'}</div>
        <span style={{ display: 'inline-flex', marginTop: 8, color: planUnavailable ? OPS.amber : OPS.blue, border: `1px solid ${planUnavailable ? OPS.amber : OPS.blue}44`, borderRadius: 9, padding: '2px 6px', fontFamily: OPS.mono, fontSize: 8.5 }}>{planUnavailable ? 'DISABLED' : past ? 'NO SNAPSHOT' : 'PENDING'}</span>
      </div>
    )
  }

  const budget = plan.budgets.weekly_normal_jpy ?? 0
  const remaining = plan.consumption.remaining_normal_jpy ?? budget
  const consumed = plan.consumption.normal_plan_budget_consumed_jpy ?? Math.max(0, budget - remaining)
  const pct = plan.consumption.normal_plan_budget_consumed_pct ?? (budget > 0 ? consumed / budget * 100 : 0)
  const opportunity = plan.consumption.remaining_opportunity_jpy ?? plan.budgets.weekly_opportunity_reserve_jpy ?? 0

  return (
    <div className="week-plan-panel">
      <button type="button" className="week-plan-button" onClick={onOpen} aria-label={`${fmtRange(week.start, week.end)}の計画詳細`}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 7 }}>
          <span style={panelEyebrow}>PLAN · ACTIVE</span>
          <span style={{ color: decisionColor(plan), fontFamily: OPS.mono, fontSize: 9 }}>{plan.today_decision.label} →</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8, marginTop: 7 }}>
          <strong style={{ color: OPS.text, fontFamily: OPS.mono, fontSize: 13 }}>通常 {fmtJpy(budget)}</strong>
          <span style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 10.5 }}>残 {fmtJpy(remaining)}</span>
        </div>
        <div style={{ height: 4, borderRadius: 8, background: OPS.hairline, overflow: 'hidden', marginTop: 5 }}>
          <div style={{ width: `${Math.max(1, Math.min(100, pct))}%`, height: '100%', background: pct >= 100 ? OPS.amber : OPS.green, borderRadius: 8, opacity: pct > 0 ? 1 : .35 }} />
        </div>
        <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 9, marginTop: 3 }}>消化 {pct.toFixed(1)}%</div>

        <div style={{ marginTop: 7 }}>
          {plan.items.slice(0, 5).map(item => {
            const itemBudget = item.normal_budget_jpy ?? 0
            const itemConsumed = item.consumed_jpy ?? 0
            const itemPct = itemBudget > 0 ? Math.max(2, Math.min(100, itemConsumed / itemBudget * 100)) : 2
            return (
              <div key={item.plan_item_id ?? `${item.priority}-${item.label}`} className="plan-item-row">
                <div style={{ minWidth: 0 }}>
                  <div style={{ color: item.status === 'covered' ? OPS.green : OPS.sub, fontSize: 9.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{item.priority ? `${item.priority}. ` : ''}{planItemLabel(item)}</div>
                  <div className="plan-item-meter"><i style={{ width: `${itemPct}%`, opacity: itemConsumed > 0 ? .95 : .38 }} /></div>
                </div>
                <span style={{ color: OPS.text, fontFamily: OPS.mono, fontSize: 9 }}>{fmtJpy(itemBudget)}</span>
              </div>
            )
          })}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 7, marginTop: 8, paddingTop: 6, borderTop: `1px solid ${OPS.hairline}` }}>
          <span style={{ color: OPS.vermilion, fontFamily: OPS.mono, fontSize: 10 }}>機会枠 {fmtJpy(opportunity)}</span>
          <span style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 9 }}>候補 {plan.summary.board_count}</span>
        </div>
      </button>
    </div>
  )
}

function DayCell({ date, dateKey, today, events, trades, pnl, maxAbsPnl, hovered, onHover, animationIndex }: {
  date: Date
  dateKey: string
  today: Date
  events: AlmanacEvent[]
  trades: PastTrade[]
  pnl?: number
  maxAbsPnl: number
  hovered: boolean
  onHover: (key: string | null) => void
  animationIndex: number
}) {
  const isToday = date.getTime() === today.getTime()
  const isPast = date < today
  const firstOfMonth = date.getDate() === 1
  const hasContent = events.length > 0 || trades.length > 0 || pnl != null
  let background: string = OPS.panel
  if (isToday) background = OPS.goldBg
  else if (pnl != null) {
    const alpha = Math.min(.48, Math.abs(pnl) / maxAbsPnl * .48)
    background = pnl >= 0 ? `rgba(87,190,146,${alpha})` : `rgba(224,72,60,${alpha})`
  } else if (date.getDay() === 0 || date.getDay() === 6) background = 'rgba(255,255,255,.012)'

  return (
    <div
      className={`almanac-cell${isToday ? ' almanac-today' : ''}`}
      tabIndex={hasContent ? 0 : -1}
      onMouseEnter={() => onHover(dateKey)}
      onMouseLeave={() => onHover(null)}
      onFocus={() => onHover(dateKey)}
      onBlur={() => onHover(null)}
      style={{
        background,
        border: `1px solid ${isToday ? `${OPS.gold}cc` : hovered ? `${OPS.gold}77` : OPS.hairline}`,
        opacity: isPast && !hasContent ? .42 : 1,
        animationDelay: `${Math.min(animationIndex, 34) * 13}ms`,
        zIndex: hovered ? 5 : undefined,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 3 }}>
        <span style={{ color: isToday ? OPS.gold : firstOfMonth ? OPS.text : OPS.sub, fontFamily: OPS.mono, fontSize: isToday ? 15 : 11.5, fontWeight: isToday ? 700 : 500 }}>
          {firstOfMonth ? `${date.getMonth() + 1}/1` : date.getDate()}
        </span>
        {pnl != null && <span style={{ color: pnl >= 0 ? OPS.green : OPS.redSoft, fontFamily: OPS.mono, fontSize: 9.5 }}>{pnl >= 0 ? '+' : '−'}{Math.abs(Math.round(pnl / 10000))}</span>}
      </div>
      {/* 「今日」は朱印で捺す。和文書の現在地の示し方 */}
      {isToday && (
        <span style={{ position: 'absolute', right: 6, bottom: 6 }}>
          <Seal label="今日" size={30} />
        </span>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1, marginTop: 3 }}>
        {trades.slice(0, 2).map((trade, index) => <div key={`trade-${index}`} style={cellLine(trade.side === 'buy' ? OPS.green : OPS.vermilion)}>{trade.side === 'buy' ? '▲' : '▼'}{trade.ticker}</div>)}
        {events.slice(0, trades.length ? 1 : 2).map((event, index) => <div key={`event-${index}`} style={cellLine(KIND_COLOR[event.kind] ?? OPS.sub)}>●{event.ticker ?? KIND_LABEL[event.kind] ?? event.kind}</div>)}
        {trades.length + events.length > 2 && <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 9.5 }}>+{trades.length + events.length - 2}</div>}
      </div>
      {hovered && hasContent && <DayPopover dateKey={dateKey} pnl={pnl} trades={trades} events={events} />}
    </div>
  )
}

function DayPopover({ dateKey, pnl, trades, events }: { dateKey: string; pnl?: number; trades: PastTrade[]; events: AlmanacEvent[] }) {
  return (
    <div style={{ position: 'absolute', top: '100%', left: '50%', transform: 'translateX(-50%)', marginTop: 4, minWidth: 245, background: OPS.panelAlt, border: `1px solid ${OPS.border}`, borderRadius: 8, padding: '10px 12px', zIndex: 30, pointerEvents: 'none', boxShadow: OPS.shadowOverlay }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, fontFamily: OPS.mono, fontSize: 11.5, marginBottom: 6 }}>
        <span style={{ color: OPS.gold }}>{dateKey.slice(5).replace('-', '/')}</span>
        {pnl != null && <span style={{ color: pnl >= 0 ? OPS.green : OPS.redSoft }}>日次 {pnl >= 0 ? '+' : '−'}{fmtJpy(Math.abs(pnl))}</span>}
      </div>
      {trades.map((trade, index) => <div key={`t-${index}`} style={popoverLine}><span style={{ color: trade.side === 'buy' ? OPS.green : OPS.vermilion }}>{trade.side === 'buy' ? '▲買' : '▼売'}</span> <span style={{ color: OPS.text, fontFamily: OPS.mono }}>{trade.ticker}</span> <span style={{ color: OPS.dim }}>{(trade.detail ?? '').slice(0, 30)}</span></div>)}
      {events.map((event, index) => <div key={`e-${index}`} style={popoverLine}><span style={{ color: KIND_COLOR[event.kind] ?? OPS.sub }}>●</span> {event.label}</div>)}
    </div>
  )
}

function WeeklyResultPanel({ trades, pnlRows, isCurrent, maxWeeklyAbs }: {
  trades: PastTrade[]
  pnlRows: Array<[string, number]>
  isCurrent: boolean
  maxWeeklyAbs: number
}) {
  const net = pnlRows.reduce((sum, [, value]) => sum + value, 0)
  const buyCount = trades.filter(trade => trade.side === 'buy').length
  const sellCount = trades.filter(trade => trade.side !== 'buy').length
  const tickers = Array.from(new Set(trades.map(trade => trade.ticker).filter(Boolean)))
  const wins = pnlRows.filter(([, value]) => value > 0).length
  const losses = pnlRows.filter(([, value]) => value < 0).length
  const hasPnl = pnlRows.length > 0
  const hasTrades = trades.length > 0
  const meterWidth = Math.min(48, Math.abs(net) / maxWeeklyAbs * 48)
  const pnlColor = net > 0 ? OPS.green : net < 0 ? OPS.redSoft : OPS.sub

  return (
    <div className="week-result-panel">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 7 }}>
        <span style={panelEyebrow}>RESULT</span>
        <span style={{ color: isCurrent ? OPS.gold : OPS.blue, fontFamily: OPS.mono, fontSize: 8.5 }}>{isCurrent ? 'LIVE' : 'CLOSED'}</span>
      </div>
      {hasPnl ? (
        <>
          <div style={{ color: pnlColor, fontFamily: OPS.mono, fontWeight: 700, fontSize: 15, marginTop: 8 }}>{signedJpy(net)}</div>
          <div className="week-result-meter" aria-label={`週次損益 ${signedJpy(net)}`}>
            {net !== 0 && <i style={{ position: 'absolute', top: 0, bottom: 0, left: net > 0 ? '50%' : undefined, right: net < 0 ? '50%' : undefined, width: `${meterWidth}%`, background: net > 0 ? OPS.green : OPS.vermilion, opacity: .88 }} />}
          </div>
          <div style={{ display: 'flex', gap: 7, flexWrap: 'wrap', marginTop: 7, color: OPS.dim, fontSize: 9.5, fontFamily: OPS.mono }}>
            <span>勝 {wins}</span><span>負 {losses}</span><span style={{ color: OPS.green }}>買 {buyCount}</span><span style={{ color: OPS.vermilion }}>売 {sellCount}</span>
          </div>
          {tickers.length > 0 && <div style={{ color: OPS.sub, fontFamily: OPS.mono, fontSize: 9.5, marginTop: 5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{tickers.slice(0, 3).join(' · ')}{tickers.length > 3 ? ` +${tickers.length - 3}` : ''}</div>}
        </>
      ) : hasTrades ? (
        <>
          <div style={{ color: OPS.amber, fontSize: 12, fontWeight: 700, marginTop: 9 }}>損益未集計</div>
          <div style={{ color: OPS.dim, fontSize: 10, lineHeight: 1.5, marginTop: 5 }}>売買 {trades.length}件を記録済み。日次損益の計測待ちです。</div>
          <div style={{ display: 'flex', gap: 7, flexWrap: 'wrap', marginTop: 7, color: OPS.dim, fontSize: 9.5, fontFamily: OPS.mono }}>
            <span style={{ color: OPS.green }}>買 {buyCount}</span><span style={{ color: OPS.vermilion }}>売 {sellCount}</span>
          </div>
          {tickers.length > 0 && <div style={{ color: OPS.sub, fontFamily: OPS.mono, fontSize: 9.5, marginTop: 5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{tickers.slice(0, 3).join(' · ')}{tickers.length > 3 ? ` +${tickers.length - 3}` : ''}</div>}
        </>
      ) : (
        <>
          <div style={{ color: OPS.sub, fontSize: 12, fontWeight: 700, marginTop: 9 }}>記録なし</div>
          <div style={{ color: OPS.dim, fontSize: 10, lineHeight: 1.5, marginTop: 5 }}>この週の損益・売買記録はありません。</div>
        </>
      )}
      {trades.length > 0 && <Link href="/executions" style={{ display: 'inline-flex', marginTop: 7, color: OPS.gold, textDecoration: 'none', fontFamily: OPS.mono, fontSize: 9 }}>台帳 →</Link>}
    </div>
  )
}

function decisionColor(plan: ExecutionPlan): string {
  if (plan.today_decision.code === 'actions_available') return OPS.green
  if (plan.today_decision.code === 'disabled' || plan.today_decision.code === 'warning') return OPS.amber
  return OPS.gold
}

function cellLine(color: string): CSSProperties {
  return { color, fontFamily: OPS.mono, fontSize: 10.5, lineHeight: 1.4, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }
}

const eyebrow: CSSProperties = { color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5, letterSpacing: '.1em' }
const panelEyebrow: CSSProperties = { color: OPS.blue, fontFamily: OPS.mono, fontSize: 10.5, letterSpacing: '.12em' }
const popoverLine: CSSProperties = { color: OPS.sub, fontSize: 12, lineHeight: 1.75, whiteSpace: 'nowrap' }

'use client'

import Link from 'next/link'
import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import { OPS, fmtJpy } from './tokens'
import { SectionHead } from './Shell'
import { ExecutionPlanModal } from './PlanRail'
import type { AlmanacData, AlmanacEvent, ExecutionPlan, PastTrade } from './types'

const ALMANAC_CSS = `
.market-clock {
  margin-top: 6px;
  border: 1px solid ${OPS.border};
  border-radius: 12px;
  background: linear-gradient(145deg, rgba(18,21,28,.98), rgba(11,13,18,.98));
  padding: 14px 16px 15px;
  overflow: hidden;
}
.market-clock-summary {
  display: grid;
  grid-template-columns: minmax(230px, 1.25fr) repeat(2, minmax(170px, .8fr));
  gap: 8px;
  margin-bottom: 14px;
}
.market-clock-card {
  min-width: 0;
  border: 1px solid ${OPS.hairline};
  border-radius: 8px;
  background: rgba(255,255,255,.018);
  padding: 10px 12px;
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
  height: 17px;
  border-left: 1px solid ${OPS.hairline};
  border-right: 1px solid ${OPS.hairline};
  background-image: linear-gradient(to right, ${OPS.hairline} 1px, transparent 1px);
  background-size: 12.5% 100%;
}
.almanac-board-scroll {
  overflow-x: auto;
  overscroll-behavior-inline: contain;
  margin-top: 20px;
  padding-bottom: 4px;
}
.almanac-board {
  min-width: 1120px;
  position: relative;
  border-radius: 13px;
  background-image:
    linear-gradient(rgba(110,140,195,.022) 1px, transparent 1px),
    linear-gradient(90deg, rgba(110,140,195,.022) 1px, transparent 1px);
  background-size: 28px 28px;
}
.almanac-month-row,
.almanac-board-head,
.almanac-week-row {
  display: grid;
  grid-template-columns: minmax(700px, 1.8fr) minmax(390px, 1fr);
  gap: 8px;
}
.almanac-month-row { margin-bottom: 8px; }
.almanac-board-head { margin-bottom: 7px; align-items: end; }
.almanac-week-row {
  position: relative;
  align-items: stretch;
  margin-bottom: 7px;
  border-radius: 10px;
}
.almanac-week-row.is-current::before {
  content: '';
  position: absolute;
  inset: -2px;
  border: 1px solid ${OPS.gold}77;
  border-radius: 12px;
  pointer-events: none;
  box-shadow: 0 0 24px rgba(201,167,93,.08), inset 0 0 18px rgba(201,167,93,.025);
  z-index: 3;
}
.month-plan-lane,
.month-intel-intro {
  position: relative;
  min-width: 0;
  border: 1px solid ${OPS.border};
  border-radius: 10px;
  background: linear-gradient(145deg, rgba(18,21,28,.98), rgba(11,14,19,.98));
  overflow: hidden;
}
.month-plan-lane {
  padding: 13px 14px 12px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.025);
}
.month-plan-lane::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, ${OPS.blue}77, ${OPS.gold}66, transparent);
}
.month-plan-top {
  display: grid;
  grid-template-columns: minmax(220px, .72fr) minmax(0, 1.55fr);
  gap: 16px;
  align-items: center;
}
.month-budget-meter {
  position: relative;
  height: 9px;
  margin-top: 9px;
  border-radius: 20px;
  background: ${OPS.hairline};
  overflow: hidden;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.025);
}
.month-budget-meter > i {
  position: absolute;
  top: 0;
  bottom: 0;
  display: block;
}
.month-week-grid {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 4px;
}
.month-week-segment {
  position: relative;
  min-width: 0;
  border: 1px solid ${OPS.hairline};
  border-radius: 7px;
  background: rgba(255,255,255,.015);
  padding: 7px 8px 8px;
  overflow: hidden;
}
.month-week-segment.is-plan-week {
  border-color: ${OPS.gold}88;
  background: linear-gradient(145deg, ${OPS.goldBg}, rgba(18,21,28,.98));
  box-shadow: 0 0 16px rgba(201,167,93,.07);
}
.month-week-segment.is-plan-week::after {
  content: '';
  position: absolute;
  left: 8px;
  right: 8px;
  bottom: 4px;
  height: 2px;
  border-radius: 2px;
  background: linear-gradient(90deg, ${OPS.green} 0 74%, ${OPS.vermilion} 74% 100%);
  opacity: .8;
}
.month-intel-intro {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: center;
  gap: 14px;
  padding: 13px 15px;
  background:
    radial-gradient(circle at 90% 10%, rgba(110,140,195,.09), transparent 34%),
    linear-gradient(145deg, rgba(18,21,28,.98), rgba(11,14,19,.98));
}
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
  font-size: 9.5px;
  letter-spacing: .12em;
}
.week-intel-head > div + div { border-left: 1px dashed ${OPS.blue}55; }
.week-meta {
  position: relative;
  min-width: 0;
  min-height: 102px;
  border: 1px solid ${OPS.hairline};
  border-radius: 8px;
  background: linear-gradient(145deg, rgba(18,21,28,.98), rgba(13,16,22,.98));
  padding: 9px 8px 9px 43px;
  overflow: hidden;
}
.week-meta::before {
  content: '';
  position: absolute;
  left: 22px;
  top: -12px;
  bottom: -12px;
  width: 1px;
  background: linear-gradient(${OPS.blue}22, ${OPS.blue}aa 35%, ${OPS.blue}55 72%, ${OPS.blue}11);
  box-shadow: 0 0 8px rgba(110,140,195,.16);
}
.week-node {
  position: absolute;
  left: 7px;
  top: 11px;
  width: 31px;
  height: 31px;
  display: grid;
  place-items: center;
  border: 1px solid ${OPS.blue}77;
  border-radius: 50%;
  background: ${OPS.inset};
  color: ${OPS.blue};
  font: 9.5px ${OPS.mono};
  box-shadow: 0 0 11px rgba(110,140,195,.10);
}
.is-current .week-meta {
  border-color: ${OPS.gold}55;
  background: linear-gradient(145deg, ${OPS.goldBg}, rgba(13,16,22,.98));
}
.is-current .week-node {
  border-color: ${OPS.gold};
  color: ${OPS.gold};
  box-shadow: 0 0 15px rgba(201,167,93,.23);
}
.week-intelligence-card {
  position: relative;
  min-width: 0;
  min-height: 102px;
  display: grid;
  grid-template-columns: minmax(0, 1.12fr) minmax(0, .88fr);
  border: 1px solid ${OPS.hairline};
  border-radius: 8px;
  background:
    radial-gradient(circle at 100% 0, rgba(110,140,195,.055), transparent 38%),
    linear-gradient(145deg, rgba(18,21,28,.985), rgba(12,15,20,.985));
  overflow: hidden;
  transition: border-color .18s ease, transform .18s ease, box-shadow .18s ease;
}
.week-intelligence-card:hover {
  border-color: ${OPS.blue}66;
  transform: translateY(-1px);
  box-shadow: 0 7px 20px rgba(0,0,0,.22);
}
.is-current .week-intelligence-card { border-color: ${OPS.gold}66; }
.week-plan-panel,
.week-result-panel {
  position: relative;
  min-width: 0;
  padding: 9px 10px;
}
.week-result-panel {
  border-left: 1px dashed ${OPS.blue}55;
  background: rgba(255,255,255,.008);
}
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
  height: 2px;
  margin-top: 3px;
  border-radius: 2px;
  background: ${OPS.hairline};
  overflow: hidden;
}
.plan-item-meter > i {
  display: block;
  height: 100%;
  border-radius: 2px;
  background: ${OPS.green};
}
.week-result-meter {
  position: relative;
  height: 7px;
  margin-top: 8px;
  border-radius: 10px;
  background: ${OPS.hairline};
  overflow: hidden;
}
.week-result-meter::after {
  content: '';
  position: absolute;
  left: 50%;
  top: -2px;
  bottom: -2px;
  width: 1px;
  background: rgba(233,231,223,.72);
  box-shadow: 0 0 5px rgba(233,231,223,.18);
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
  border-radius: 7px;
  padding: 6px 7px;
  animation: almanacCellIn .38s ease both;
  transition: border-color .18s ease, transform .18s ease, background .18s ease;
}
.almanac-cell:hover,
.almanac-cell:focus-visible { transform: translateY(-2px); outline: none; }
.almanac-today { animation: almanacCellIn .38s ease both, almanacTodayGlow 2.8s ease-in-out infinite; }
@keyframes almanacCellIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes almanacTodayGlow {
  0%, 100% { box-shadow: 0 0 0 1px ${OPS.gold}55, 0 0 0 rgba(201,167,93,0); }
  50% { box-shadow: 0 0 0 1px ${OPS.gold}99, 0 0 18px rgba(201,167,93,.14); }
}
@container ops-content (max-width: 1180px) {
  .market-clock-summary { grid-template-columns: 1fr 1fr; }
  .market-clock-summary .market-clock-card:first-child { grid-column: 1 / -1; }
}
@container ops-content (max-width: 760px) {
  .market-clock-summary { grid-template-columns: 1fr; }
  .market-clock-summary .market-clock-card:first-child { grid-column: auto; }
  .market-clock-lane { grid-template-columns: 82px minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  .almanac-cell, .almanac-today { animation: none; }
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
      />

      <MarketClock almanac={almanac} />
      <WeekBoard almanac={almanac} plan={plan} onOpenPlan={() => setPlanOpen(true)} />
      <ExecutionPlanModal plan={plan} open={planOpen} onClose={() => setPlanOpen(false)} />
    </section>
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

  const nowMinutes = now ? now.getHours() * 60 + now.getMinutes() : 0
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
          <div style={eyebrow}>MARKET NOW · JST {now ? `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}` : '—'}</div>
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
  const resultWeekCount = weeklyRows.filter(row => row.trades.length > 0 || row.pnlRows.length > 0).length
  const activePlanWeekCount = plan?.status === 'active'
    ? weeks.filter(week => isPlanWeek(week, plan)).length
    : 0

  return (
    <div className="almanac-board-scroll">
      <div className="almanac-board">
        <div className="almanac-month-row">
          <MonthPlanLane weeks={weeks} plan={plan} today={today} />
          <div className="month-intel-intro">
            <div>
              <div style={{ color: OPS.blue, fontFamily: OPS.mono, fontSize: 10.5, letterSpacing: '.14em' }}>WEEK INTELLIGENCE</div>
              <div style={{ color: OPS.text, fontSize: 13, fontWeight: 700, marginTop: 6 }}>各週の計画と結果を同じ行で比較</div>
              <div style={{ color: OPS.dim, fontSize: 10.5, marginTop: 4 }}>結果バーは全週共通スケール · 日次の濃淡はカレンダーに集約</div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, auto)', gap: '5px 13px', color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5 }}>
              <span>計画あり</span><strong style={{ color: OPS.gold }}>{activePlanWeekCount}週</strong>
              <span>実績あり</span><strong style={{ color: OPS.green }}>{resultWeekCount}週</strong>
            </div>
          </div>
        </div>

        <div className="almanac-board-head">
          <div className="calendar-head">
            <div style={{ color: OPS.blue, fontFamily: OPS.mono, fontSize: 9.5, letterSpacing: '.12em', padding: '0 8px 4px' }}>WEEK</div>
            <div className="almanac-days">
              {['月', '火', '水', '木', '金', '土', '日'].map(day => (
                <div key={day} style={{ color: OPS.sub, fontSize: 12, fontWeight: 700, textAlign: 'center', letterSpacing: '.12em', paddingBottom: 3 }}>{day}</div>
              ))}
            </div>
          </div>
          <div className="week-intel-head" aria-label="各週の計画と結果">
            <div>PLAN · 週の目的と予算</div>
            <div>RESULT · 週合計</div>
          </div>
        </div>

        {weeklyRows.map(({ week, trades, pnlRows }, weekIndex) => {
          const isCurrent = week.startKey === dkey(currentWeekStart)
          return (
            <div key={week.startKey} className={`almanac-week-row${isCurrent ? ' is-current' : ''}`}>
              <div className="calendar-week">
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
              <WeeklyIntelligenceCard
                week={week}
                plan={plan}
                trades={trades}
                pnlRows={pnlRows}
                isCurrent={isCurrent}
                today={today}
                maxWeeklyAbs={maxWeeklyAbs}
                onOpenPlan={onOpenPlan}
              />
            </div>
          )
        })}

        <div style={{ display: 'flex', alignItems: 'center', gap: 15, flexWrap: 'wrap', color: OPS.dim, fontSize: 11.5, fontFamily: OPS.mono, marginTop: 10 }}>
          <span>表示範囲 先々週〜4週先</span>
          <span><span style={{ color: OPS.green }}>▲</span> 買い <span style={{ color: OPS.vermilion }}>▼</span> 売り</span>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>日次損益 <i style={{ width: 13, height: 9, background: 'rgba(224,72,60,.4)', borderRadius: 2 }} /><i style={{ width: 13, height: 9, background: 'rgba(87,190,146,.4)', borderRadius: 2 }} /></span>
          {(['earnings', 'nisa', 'policy', 'order'] as const).map(kind => <span key={kind}><span style={{ color: KIND_COLOR[kind] }}>●</span> {KIND_LABEL[kind]}</span>)}
          <span style={{ marginLeft: 'auto' }}>月次枠 → 週次計画 → 日次イベント</span>
        </div>
      </div>
    </div>
  )
}

function MonthPlanLane({ weeks, plan, today }: { weeks: WeekRow[]; plan?: ExecutionPlan; today: Date }) {
  const activePlan = plan?.status === 'active'
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
  const weeklyPct = monthlyTotal > 0 ? Math.max(0, Math.min(100 - consumedPct, weeklyTotal / monthlyTotal * 100)) : 0
  const normalShare = weeklyTotal > 0 ? weeklyNormal / weeklyTotal * 100 : 0
  const month = plan?.horizon.month ?? monthKey(today)
  const monthLabel = month.replace('-', '.')
  const unattributedCount = activePlan ? plan.consumption.unattributed_monthly_total_count ?? 0 : 0
  const unattributedNotional = activePlan ? plan.consumption.unattributed_monthly_total_notional_jpy ?? 0 : 0
  const attributionIncomplete = activePlan && (plan.consumption.monthly_attribution_incomplete === true || unattributedCount > 0)
  const unavailableLabel = plan?.status === 'disabled' ? '計画レイヤー無効' : '月次計画は未策定'
  const unavailableReason = plan?.today_decision.reason ?? '有効な計画が生成されるまで、予算は表示専用です。'

  return (
    <div className="month-plan-lane" data-testid="monthly-plan-lane">
      <div className="month-plan-top">
        <div>
          <div style={{ color: activePlan ? OPS.gold : OPS.amber, fontFamily: OPS.mono, fontSize: 10.5, letterSpacing: '.13em' }}>MONTHLY PLAN · {monthLabel}{activePlan ? '' : ' · DISABLED'}</div>
          {activePlan ? (
            <>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginTop: 8 }}>
                <strong style={{ color: OPS.text, fontFamily: OPS.mono, fontSize: 20 }}>{fmtJpy(monthlyTotal)}</strong>
                <span style={{ color: OPS.dim, fontSize: 10.5 }}>月間枠</span>
              </div>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 5, color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5 }}>
                <span>帰属済み <b style={{ color: OPS.green }}>{fmtJpy(monthlyConsumed)}</b></span>
                <span>残 <b style={{ color: attributionIncomplete ? OPS.sub : OPS.gold }}>{fmtJpy(monthlyRemaining)}</b></span>
              </div>
              <div className="month-budget-meter" aria-label={`月次枠 ${fmtJpy(monthlyTotal)}、帰属済み ${fmtJpy(monthlyConsumed)}、今週配分 ${fmtJpy(weeklyTotal)}`}>
                <i style={{ left: 0, width: `${consumedPct}%`, background: OPS.green }} />
                <i style={{ left: `${consumedPct}%`, width: `${weeklyPct * normalShare / 100}%`, background: OPS.blue, opacity: .78 }} />
                <i style={{ left: `${consumedPct + weeklyPct * normalShare / 100}%`, width: `${weeklyPct * (100 - normalShare) / 100}%`, background: OPS.vermilion, opacity: .76 }} />
              </div>
            </>
          ) : (
            <div style={{ marginTop: 9 }}>
              <strong style={{ color: OPS.amber, fontSize: 14 }}>{unavailableLabel}</strong>
              <div style={{ color: OPS.dim, fontSize: 10.5, lineHeight: 1.55, marginTop: 5 }}>{unavailableReason}</div>
            </div>
          )}
        </div>

        <div>
          <div className="month-week-grid" aria-label="月間計画の週別状態">
            {weeks.map(week => {
              const planWeek = activePlan && isPlanWeek(week, plan)
              const past = week.end < today
              const outsideMonth = monthKey(week.start) > month
              const status = planWeek ? `今週 ${fmtJpy(weeklyTotal)}` : !activePlan ? '無効' : past ? '履歴なし' : outsideMonth ? '次月' : '未策定'
              return (
                <div key={week.startKey} className={`month-week-segment${planWeek ? ' is-plan-week' : ''}`}>
                  <div style={{ color: planWeek ? OPS.gold : OPS.blue, fontFamily: OPS.mono, fontSize: 9.5 }}>W{isoWeekNumber(week.start)}</div>
                  <div style={{ color: planWeek ? OPS.text : OPS.dim, fontSize: 9.5, marginTop: 3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{status}</div>
                </div>
              )
            })}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, minHeight: 18, marginTop: 8, color: attributionIncomplete ? OPS.amber : OPS.dim, fontSize: 10.5 }}>
            <span aria-hidden>{attributionIncomplete ? '▲' : '●'}</span>
            <span>{!activePlan ? '有効な計画が生成されるまで、月次予算は利用しません。' : attributionIncomplete ? `月次帰属確認中 · 未帰属${unattributedCount}件 ${fmtJpy(unattributedNotional)} は月次枠に未算入` : `今週配分 通常 ${fmtJpy(weeklyNormal)} · 機会 ${fmtJpy(weeklyOpportunity)}`}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function WeekMeta({ week, current, today }: { week: WeekRow; current: boolean; today: Date }) {
  const status = week.end < today ? 'CLOSED' : week.start > today ? 'UPCOMING' : 'IN PROGRESS'
  return (
    <div className="week-meta">
      <span className="week-node">W{isoWeekNumber(week.start)}</span>
      <div style={{ color: current ? OPS.gold : OPS.sub, fontFamily: OPS.mono, fontSize: 10.5 }}>{fmtRange(week.start, week.end)}</div>
      {current ? (
        <div style={{ display: 'inline-flex', marginTop: 7, color: OPS.gold, border: `1px solid ${OPS.gold}55`, borderRadius: 9, padding: '2px 6px', fontFamily: OPS.mono, fontSize: 8.5 }}>THIS WEEK</div>
      ) : (
        <div style={{ color: OPS.blue, fontFamily: OPS.mono, fontSize: 8.5, marginTop: 8 }}>{status}</div>
      )}
    </div>
  )
}

function WeeklyIntelligenceCard({ week, plan, trades, pnlRows, isCurrent, today, maxWeeklyAbs, onOpenPlan }: {
  week: WeekRow
  plan?: ExecutionPlan
  trades: PastTrade[]
  pnlRows: Array<[string, number]>
  isCurrent: boolean
  today: Date
  maxWeeklyAbs: number
  onOpenPlan: () => void
}) {
  return (
    <div className="week-intelligence-card" aria-label={`${fmtRange(week.start, week.end)}の週次計画と結果`}>
      <WeeklyPlanPanel week={week} plan={plan} today={today} onOpen={onOpenPlan} />
      <WeeklyResultPanel week={week} trades={trades} pnlRows={pnlRows} isCurrent={isCurrent} today={today} maxWeeklyAbs={maxWeeklyAbs} />
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
        <span style={{ color: isToday ? OPS.gold : firstOfMonth ? OPS.text : OPS.sub, fontFamily: OPS.mono, fontSize: 11.5, fontWeight: isToday ? 700 : 500 }}>
          {firstOfMonth ? `${date.getMonth() + 1}/1` : date.getDate()}{isToday && <span style={{ marginLeft: 3, fontSize: 9 }}>今日</span>}
        </span>
        {pnl != null && <span style={{ color: pnl >= 0 ? OPS.green : OPS.redSoft, fontFamily: OPS.mono, fontSize: 9.5 }}>{pnl >= 0 ? '+' : '−'}{Math.abs(Math.round(pnl / 10000))}</span>}
      </div>
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
    <div style={{ position: 'absolute', top: '100%', left: '50%', transform: 'translateX(-50%)', marginTop: 4, minWidth: 245, background: 'rgba(14,17,23,.985)', border: `1px solid ${OPS.gold}66`, borderRadius: 8, padding: '10px 12px', zIndex: 30, pointerEvents: 'none', boxShadow: '0 12px 30px rgba(0,0,0,.58)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, fontFamily: OPS.mono, fontSize: 11.5, marginBottom: 6 }}>
        <span style={{ color: OPS.gold }}>{dateKey.slice(5).replace('-', '/')}</span>
        {pnl != null && <span style={{ color: pnl >= 0 ? OPS.green : OPS.redSoft }}>日次 {pnl >= 0 ? '+' : '−'}{fmtJpy(Math.abs(pnl))}</span>}
      </div>
      {trades.map((trade, index) => <div key={`t-${index}`} style={popoverLine}><span style={{ color: trade.side === 'buy' ? OPS.green : OPS.vermilion }}>{trade.side === 'buy' ? '▲買' : '▼売'}</span> <span style={{ color: OPS.text, fontFamily: OPS.mono }}>{trade.ticker}</span> <span style={{ color: OPS.dim }}>{(trade.detail ?? '').slice(0, 30)}</span></div>)}
      {events.map((event, index) => <div key={`e-${index}`} style={popoverLine}><span style={{ color: KIND_COLOR[event.kind] ?? OPS.sub }}>●</span> {event.label}</div>)}
    </div>
  )
}

function WeeklyResultPanel({ week, trades, pnlRows, isCurrent, today, maxWeeklyAbs }: {
  week: WeekRow
  trades: PastTrade[]
  pnlRows: Array<[string, number]>
  isCurrent: boolean
  today: Date
  maxWeeklyAbs: number
}) {
  const isFuture = week.start > today
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
    <div className="week-result-panel" style={{ opacity: isFuture ? .68 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 7 }}>
        <span style={panelEyebrow}>RESULT</span>
        <span style={{ color: isCurrent ? OPS.gold : OPS.blue, fontFamily: OPS.mono, fontSize: 8.5 }}>{isCurrent ? 'LIVE' : isFuture ? 'WAITING' : 'CLOSED'}</span>
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
          <div style={{ color: OPS.sub, fontSize: 12, fontWeight: 700, marginTop: 9 }}>{isFuture ? '未集計' : '記録なし'}</div>
          <div style={{ color: OPS.dim, fontSize: 10, lineHeight: 1.5, marginTop: 5 }}>{isFuture ? '週の進行後に週合計を表示します。' : 'この週の損益・売買記録はありません。'}</div>
        </>
      )}
      {!isFuture && trades.length > 0 && <Link href="/executions" style={{ display: 'inline-flex', marginTop: 7, color: OPS.gold, textDecoration: 'none', fontFamily: OPS.mono, fontSize: 9 }}>台帳 →</Link>}
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

const eyebrow: CSSProperties = { color: OPS.dim, fontFamily: OPS.mono, fontSize: 9.5, letterSpacing: '.1em' }
const panelEyebrow: CSSProperties = { color: OPS.blue, fontFamily: OPS.mono, fontSize: 8.5, letterSpacing: '.12em' }
const popoverLine: CSSProperties = { color: OPS.sub, fontSize: 11.5, lineHeight: 1.7, whiteSpace: 'nowrap' }

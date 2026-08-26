'use client'
import { useEffect, useState } from 'react'
import { OPS, fmtAge } from './tokens'
import type { AlmanacSession, Command, TodayOps } from './types'
import FreshnessDots from './FreshnessDots'
import { jstHHMM, jstMinutesOfDay, jstMonthDay } from './jstTime'

/**
 * MARKET RAIL — 画面最上部の市場ステータス帯。
 *
 * モックは各市場の指数水準（日経 41,860 等）と USD/JPY を並べているが、
 * /api/today にはどちらも入っていない。数字を作ると観測所として嘘になるので、
 * 「取引所のセッション状態」＋「実在する指標」で同じ構造を組む。
 * 追加できるのは指数水準を配信し始めてからで良い。
 */

function minutesOf(value: string): number {
  const [h, m] = value.split(':').map(Number)
  return h * 60 + m
}

/** 日を跨ぐセッション（米国通常 22:30–05:00）も判定する */
function inSession(nowMin: number, start: string, end: string): boolean {
  const s = minutesOf(start)
  const e = minutesOf(end)
  return e < s ? nowMin >= s || nowMin < e : nowMin >= s && nowMin < e
}

type MarketState = { label: string; status: string; tone: string; note?: string }

export function marketState(sessions: AlmanacSession[], market: string, jp: string, now: Date | null): MarketState {
  const mine = sessions.filter(s => s.market === market)
  if (mine.length === 0) return { label: jp, status: '—', tone: OPS.dim }

  if (now) {
    const nowMin = jstMinutesOfDay(now)
    const live = mine
      .filter(s => s.is_open_day !== false && inSession(nowMin, s.start, s.end))
      .sort((a, b) => Number(b.phase === 'regular') - Number(a.phase === 'regular'))[0]
    if (live) {
      return {
        label: jp,
        status: live.phase === 'regular' ? 'OPEN' : live.phase === 'pre' ? 'PRE-MKT' : 'AFTER',
        tone: live.phase === 'regular' ? OPS.green : OPS.blue,
        note: live.label,
      }
    }
  }

  const holiday = mine.every(s => s.is_open_day === false)
  const nextIso = mine.map(s => s.next_market_open).filter(Boolean).sort()[0]
  let note: string | undefined
  if (nextIso) {
    const d = new Date(nextIso)
    if (!Number.isNaN(d.getTime())) {
      const { month, date } = jstMonthDay(d)
      note = `次 ${month}/${date} ${jstHHMM(d)}`
    }
  }
  return { label: jp, status: holiday ? 'CLOSED' : 'OFF-HOURS', tone: OPS.dim, note }
}

function Cell({ code, label, value, tone, note }: {
  code: string; label: string; value: string; tone?: string; note?: string
}) {
  return (
    <div className="rail-cell">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 7 }}>
        <span className="ops-latin" style={{ fontSize: 9.5, color: OPS.dim }}>{code}</span>
        <span style={{ fontFamily: OPS.display, fontSize: 11, color: OPS.sub, letterSpacing: '.08em' }}>{label}</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 3 }}>
        <span style={{ fontFamily: OPS.mono, fontSize: 15, fontWeight: 500, color: tone ?? OPS.text, letterSpacing: '.01em' }}>{value}</span>
        {note && <span style={{ fontFamily: OPS.mono, fontSize: 10, color: OPS.dim, whiteSpace: 'nowrap' }}>{note}</span>}
      </div>
    </div>
  )
}

export default function StatusLine({
  command, asOf, snapshot, sessions = [],
}: {
  command: Command
  asOf?: string
  snapshot: TodayOps['snapshot_meta']
  sessions?: AlmanacSession[]
}) {
  // now はクライアントでのみ確定させる（SSR と一致させないと hydration がずれる）
  const [now, setNow] = useState<Date | null>(null)
  useEffect(() => {
    const tick = () => setNow(new Date())
    tick()
    const timer = setInterval(tick, 30000)
    return () => clearInterval(timer)
  }, [])

  const stale = (command.data_age_hours ?? 0) > 24
  const jpMarket = marketState(sessions, 'JP', '東証', now)
  const usMarket = marketState(sessions, 'US', '米国株', now)
  const g = command.guard
  const guardOk = g.new_entry_allowed !== false && g.trading_allowed !== false && g.alerts.length === 0
  const vixTone = command.vix == null ? OPS.dim
    : command.vix >= 28 ? OPS.vermilion : command.vix >= 20 ? OPS.amber : OPS.green

  return (
    <div className="market-rail">
      <style dangerouslySetInnerHTML={{ __html: `
        .market-rail {
          position:sticky; top:72px; z-index:40;
          display:flex; align-items:stretch; gap:0;
          padding:7px max(2vw,20px) 8px;
          background:rgba(4,18,33,.96);
          backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
          border-top:1px solid ${OPS.hairline}; border-bottom:1px solid ${OPS.border};
          overflow-x:auto; scrollbar-width:none;
        }
        .market-rail::-webkit-scrollbar { display:none; }
        .rail-cell { padding:0 18px; min-width:0; white-space:nowrap; flex:0 0 auto; }
        .rail-cell + .rail-cell { border-left:1px solid ${OPS.hairline}; }
        .rail-cell:first-child { padding-left:0; }
        .rail-tail { margin-left:auto; display:flex; align-items:center; gap:12px; padding-left:18px; flex:0 0 auto; }
        @container ops-content (max-width:760px) { .rail-cell { padding:0 12px; } }
      ` }} />

      <Cell code="TOKYO" label={jpMarket.label} value={jpMarket.status} tone={jpMarket.tone} note={jpMarket.note} />
      <Cell code="US" label={usMarket.label} value={usMarket.status} tone={usMarket.tone} note={usMarket.note} />
      <Cell
        code="VOLATILITY" label="VIX"
        value={command.vix != null ? command.vix.toFixed(1) : '—'}
        tone={vixTone}
        note={command.vix_status ?? undefined}
      />
      <Cell code="RATES" label="米10年" value={command.yield_10y != null ? `${command.yield_10y.toFixed(2)}%` : '—'} />
      <Cell
        code="FX" label="USD比率"
        value={command.usd_ratio_pct != null ? `${command.usd_ratio_pct.toFixed(1)}%` : '—'}
        note={command.usd_target_pct != null ? `目標 ${command.usd_target_pct}%` : undefined}
      />
      <Cell
        code="REGIME" label="相場" value={command.scenario ?? '—'} tone={OPS.green}
        note={guardOk ? 'ガード 通常' : `ガード 警告 ${g.alerts.length}`}
      />

      <div className="rail-tail">
        <span style={{ textAlign: 'right' }}>
          <div className="ops-latin" style={{ fontSize: 9.5, color: OPS.dim }}>{stale ? 'DATA STALE' : 'ANALYSIS'}</div>
          <div style={{ fontFamily: OPS.mono, fontSize: 12, color: stale ? OPS.amber : OPS.sub, marginTop: 3 }}>
            {fmtAge(command.data_age_hours)}
            <span className="hidden md:inline" style={{ color: OPS.dim }}> · {asOf ?? '—'}</span>
          </div>
        </span>
        <FreshnessDots health={snapshot.data_health} />
      </div>
    </div>
  )
}

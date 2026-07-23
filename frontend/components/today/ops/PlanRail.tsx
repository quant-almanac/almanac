'use client'

import Link from 'next/link'
import { useId, useRef, useState, type CSSProperties, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import { useSWRConfig } from 'swr'
import { apiFetch, apiErrorMessage } from '@/lib/api'
import { Bar, Chip, Modal } from './PageKit'
import { OPS, fmtAge, fmtJpy } from './tokens'
import type { AlmanacData, ExecutionPlan, ExecutionPlanRationale } from './types'

type RailTab = 'plan' | 'schedule' | 'record'

const TABS: Array<{ key: RailTab; label: string }> = [
  { key: 'plan', label: '計画' },
  { key: 'schedule', label: '予定' },
  { key: 'record', label: '記録' },
]

const EVENT_FILTERS = ['earnings', 'nisa', 'policy', 'order', 'reminder'] as const
const KIND_LABEL: Record<string, string> = {
  earnings: '決算', nisa: 'NISA', policy: 'ポリシー', order: '失効', reminder: 'リマインド',
}
const KIND_COLOR: Record<string, string> = {
  earnings: OPS.orchid, nisa: OPS.green, policy: OPS.amber, order: OPS.vermilion, reminder: OPS.blue,
}

function rationaleText(reason: string | ExecutionPlanRationale): string {
  return typeof reason === 'string' ? reason : reason.message ?? reason.reason_code ?? ''
}

function decisionColor(plan: ExecutionPlan): string {
  if (plan.today_decision.code === 'actions_available') return OPS.green
  if (plan.today_decision.code === 'disabled' || plan.today_decision.code === 'warning') return OPS.amber
  return OPS.gold
}

function reasonCodeLabel(code: string): string {
  const map: Record<string, string> = {
    plan_consumed_by_open_order: '既存注文で消費',
    plan_wait_for_better_candidate: 'より良い候補待ち',
    plan_over_budget: '計画枠超過',
    execution_plan_existing_guard: '既存ガード優先',
    plan_unmatched_no_override: '計画外',
    already_executed: '実行済み',
    plan_new_order: '計画内',
    opportunistic_override: '機会枠',
  }
  return map[code] ?? code.replaceAll('_', ' ')
}

export default function PlanRail({
  plan,
  almanac,
  onTabChange,
}: {
  plan?: ExecutionPlan
  almanac: AlmanacData
  onTabChange?: (tab: RailTab) => void
}) {
  const [active, setActive] = useState<RailTab>('plan')
  const [modalOpen, setModalOpen] = useState(false)
  const [filters, setFilters] = useState<Set<string>>(() => new Set(EVENT_FILTERS))
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const id = useId()

  const selectTab = (tab: RailTab) => {
    setActive(tab)
    onTabChange?.(tab)
  }
  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + TABS.length) % TABS.length
    selectTab(TABS[next].key)
    tabRefs.current[next]?.focus()
  }
  const toggleFilter = (kind: string) => {
    setFilters(current => {
      const next = new Set(current)
      if (next.has(kind)) next.delete(kind)
      else next.add(kind)
      return next
    })
  }

  const upcoming = almanac.upcoming.filter(event => !EVENT_FILTERS.includes(event.kind as typeof EVENT_FILTERS[number]) || filters.has(event.kind))
  const recentTrades = [...(almanac.past ?? [])].reverse().slice(0, 8)

  return (
    <aside className="almanac-plan-rail" style={{ minWidth: 0 }}>
      <div role="tablist" aria-label="相場暦のサイド情報" style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6, marginBottom: 12 }}>
        {TABS.map((tab, index) => {
          const selected = active === tab.key
          return (
            <button
              key={tab.key}
              ref={element => { tabRefs.current[index] = element }}
              id={`${id}-${tab.key}-tab`}
              role="tab"
              aria-selected={selected}
              aria-controls={`${id}-${tab.key}-panel`}
              tabIndex={selected ? 0 : -1}
              onClick={() => selectTab(tab.key)}
              onKeyDown={event => onTabKeyDown(event, index)}
              style={{
                background: selected ? OPS.goldBg : 'transparent',
                border: `1px solid ${selected ? `${OPS.gold}66` : OPS.hairline}`,
                borderRadius: 6,
                color: selected ? OPS.gold : OPS.sub,
                cursor: 'pointer',
                fontFamily: OPS.mono,
                fontSize: 12.5,
                letterSpacing: '0.08em',
                padding: '9px 6px',
              }}
            >
              {tab.label}
            </button>
          )
        })}
      </div>

      <div id={`${id}-${active}-panel`} role="tabpanel" aria-labelledby={`${id}-${active}-tab`}>
        {active === 'plan' && <PlanTab plan={plan} onOpenModal={() => setModalOpen(true)} />}
        {active === 'schedule' && (
          <ScheduleTab upcoming={upcoming} todayStr={almanac.today_str} filters={filters} onToggle={toggleFilter} />
        )}
        {active === 'record' && <RecordTab trades={recentTrades} notes={almanac.notes} />}
      </div>

      <ExecutionPlanModal plan={plan} open={modalOpen} onClose={() => setModalOpen(false)} />
    </aside>
  )
}

function PlanTab({ plan, onOpenModal }: { plan?: ExecutionPlan; onOpenModal: () => void }) {
  if (!plan) {
    return <p style={{ color: OPS.dim, fontSize: 13.5, lineHeight: 1.7, margin: '12px 0' }}>実行計画データはまだありません。</p>
  }

  const b = plan.budgets ?? {}
  const c = plan.consumption ?? {}
  const pct = c.normal_plan_budget_consumed_pct
  const remaining = b.normal_pool_available_jpy ?? c.remaining_normal_jpy
  const monthlyTotal = b.monthly_total_jpy ?? 0
  const monthlyRemaining = c.monthly_remaining_jpy ?? b.monthly_remaining_jpy
  const monthlyRemainingPct = monthlyTotal > 0 && monthlyRemaining != null
    ? Math.max(0, Math.min(100, monthlyRemaining / monthlyTotal * 100))
    : 0
  const attributionIncomplete = c.monthly_attribution_incomplete === true
    || (c.unattributed_monthly_total_count ?? 0) > 0
  const ageColor = (plan.age_hours ?? 0) > 48 ? OPS.amber : OPS.dim
  const activeItems = [...plan.items.filter(item => item.status === 'active'), ...plan.items.filter(item => item.status === 'covered')].slice(0, 5)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <section style={railPanel}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 12 }}>
          <span style={railTitle}>今週の計画</span>
          <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, fontSize: 11.5, color: ageColor }}>
            {plan.horizon.week_start && plan.horizon.week_end ? `${plan.horizon.week_start.slice(5)}–${plan.horizon.week_end.slice(5)}` : '—'} · {fmtAge(plan.age_hours)}
          </span>
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 10, marginBottom: 7 }}>
          <span style={{ fontSize: 13.5, color: OPS.sub }}>通常の共通プール {pct != null ? `参考消化 ${pct.toFixed(1)}%` : ''}</span>
          <span style={{ fontFamily: OPS.mono, color: OPS.gold, fontSize: 13 }}>残 {fmtJpy(remaining)}</span>
        </div>
        <Bar pct={pct ?? 0} color={(pct ?? 0) >= 100 ? OPS.amber : OPS.gold} height={7} />

        <p style={{ margin: '10px 0 0', color: OPS.sub, fontSize: 13, lineHeight: 1.65 }}>
          対応した実額 <span style={{ color: OPS.text, fontFamily: OPS.mono }}>{fmtJpy(c.normal_matched_notional_jpy)}</span>
          {' '}（注文中 {fmtJpy(c.normal_open_order_matched_notional_jpy)} ／ 約定 {fmtJpy(c.normal_filled_matched_notional_jpy)}）
          <span style={{ color: OPS.dim, fontSize: 12 }}> ※計画枠とは別尺度</span>
        </p>

        <p style={{ margin: '7px 0 0', color: OPS.dim, fontSize: 12.5, lineHeight: 1.6 }}>
          通常裁量枠 {fmtJpy(b.monthly_discretionary_budget_jpy)} · 承認済み追加資金 {fmtJpy(plan.contributions?.available_jpy)}
        </p>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 11 }}>
          <Chip color={OPS.blue} bg={OPS.blueBg} mono>機会枠 残 {fmtJpy(c.remaining_opportunity_jpy)}</Chip>
          {(c.opportunity_matched_notional_jpy ?? 0) > 0 && <span style={{ color: OPS.sub, fontFamily: OPS.mono, fontSize: 12 }}>対応 {fmtJpy(c.opportunity_matched_notional_jpy)}</span>}
        </div>

        {activeItems.length > 0 && (
          <div style={{ marginTop: 13, display: 'flex', flexDirection: 'column', gap: 7 }}>
            {activeItems.map(item => (
              <div key={item.plan_item_id ?? item.label} style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) auto', gap: 10, alignItems: 'baseline', color: item.status === 'covered' ? OPS.dim : OPS.sub, fontSize: 13.5 }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ color: item.status === 'covered' ? OPS.dim : OPS.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.label}</div>
                  {item.preferred_tickers.length > 0 && <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 11.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginTop: 2 }}>{item.preferred_tickers.join(' · ')}</div>}
                </div>
                <span style={{ color: item.status === 'covered' ? OPS.dim : OPS.gold, fontFamily: OPS.mono, fontSize: 12.5 }}>{fmtJpy(item.remaining_jpy)}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <ContributionPanel plan={plan} />

      <div style={{ borderLeft: `2px solid ${decisionColor(plan)}`, padding: '2px 0 2px 11px' }}>
        <div style={{ color: decisionColor(plan), fontSize: 15, fontWeight: 700, marginBottom: 4 }}>{plan.today_decision.label}</div>
        <p style={{ color: OPS.sub, fontSize: 13, lineHeight: 1.65, margin: 0 }}>{plan.today_decision.reason}</p>
      </div>

      <section style={railPanel}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 10 }}>
          <span style={railTitle}>今月の計画 {plan.horizon.month ?? ''}</span>
          {attributionIncomplete && <Chip color={OPS.amber} bg={OPS.amberBg} mono>帰属確認中</Chip>}
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, marginBottom: 7, fontSize: 13.5 }}>
          <span style={{ color: OPS.sub }}>月次残り</span>
          <span style={{ color: attributionIncomplete ? OPS.sub : OPS.gold, fontFamily: OPS.mono }}>{fmtJpy(monthlyRemaining)} / {fmtJpy(monthlyTotal)}</span>
        </div>
        <Bar pct={monthlyRemainingPct} color={attributionIncomplete ? OPS.sub : OPS.gold} height={7} />
        {attributionIncomplete && (
          <p style={{ color: OPS.amber, fontSize: 12.5, lineHeight: 1.65, margin: '9px 0 0' }}>
            未帰属の注文・約定 {c.unattributed_monthly_total_count ?? 0}件 {fmtJpy(c.unattributed_monthly_total_notional_jpy)} 未算入
          </p>
        )}
        <p style={{ color: OPS.sub, fontSize: 12.5, margin: '8px 0 0' }}>積立予定残 {fmtJpy(b.scheduled_contributions_remaining_jpy)}</p>
        <button onClick={onOpenModal} style={linkButton}>計画の全文 →</button>
      </section>
    </div>
  )
}

function ContributionPanel({ plan }: { plan: ExecutionPlan }) {
  const { mutate } = useSWRConfig()
  const [source, setSource] = useState<'salary' | 'bonus' | 'other'>('salary')
  const [amount, setAmount] = useState('')
  const [bucket, setBucket] = useState<'normal' | 'opportunity'>('normal')
  const [owner, setOwner] = useState<'husband' | 'wife'>('husband')
  const [broker, setBroker] = useState<'rakuten' | 'sbi'>('rakuten')
  const [releaseMonths, setReleaseMonths] = useState('1')
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null)
  const approvalIdempotencyKey = useRef(newApprovalIdempotencyKey())
  const sources = plan.contributions?.sources ?? []

  const onSource = (next: 'salary' | 'bonus' | 'other') => {
    setSource(next)
    if (next === 'bonus') setReleaseMonths('4')
    else if (releaseMonths === '4') setReleaseMonths('1')
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const amountJpy = Math.round(Number(amount.replaceAll(',', '')))
    if (!Number.isFinite(amountJpy) || amountJpy <= 0) {
      setResult({ ok: false, text: '投資へ回す金額を入力してください。' })
      return
    }
    setSaving(true)
    setResult(null)
    try {
      const response = await apiFetch('/api/contributions/approve', {
        method: 'POST',
        body: JSON.stringify({
          source,
          amount_jpy: amountJpy,
          bucket,
          owner,
          broker,
          release_months: Math.max(1, Math.min(24, Math.round(Number(releaseMonths) || 1))),
          idempotency_key: approvalIdempotencyKey.current,
        }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok || body?.ok === false) {
        setResult({ ok: false, text: apiErrorMessage(body, `保存失敗: HTTP ${response.status}`) })
        return
      }
      setAmount('')
      approvalIdempotencyKey.current = newApprovalIdempotencyKey()
      setResult({ ok: true, text: body.warning ?? '承認済み資金として共通プールへ反映しました。' })
      mutate('/api/today')
    } catch (error) {
      setResult({ ok: false, text: `保存失敗: ${String(error)}` })
    } finally {
      setSaving(false)
    }
  }

  return (
    <section style={railPanel}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6 }}>
        <span style={railTitle}>追加投資資金</span>
        <span style={{ marginLeft: 'auto', color: OPS.gold, fontFamily: OPS.mono, fontSize: 12 }}>{fmtJpy(plan.contributions?.available_jpy)}</span>
      </div>
      <p style={{ margin: '0 0 10px', color: OPS.sub, fontSize: 12.5, lineHeight: 1.65 }}>
        証券口座へ実入金済みで、投資に回すと決めた給料・ボーナスだけを登録します。既存現金、売却代金、振替は登録しません。
      </p>
      {sources.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5, marginBottom: 11 }}>
          {sources.slice(0, 3).map(item => (
            <div key={item.id} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, color: OPS.sub, fontSize: 12 }}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.source === 'bonus' ? '賞与' : item.source === 'salary' ? '給与' : 'その他'} · {item.bucket === 'opportunity' ? '機会枠' : '通常枠'} · {item.owner === 'wife' ? '妻' : '夫'}</span>
              <span style={{ color: OPS.green, fontFamily: OPS.mono, flexShrink: 0 }}>残 {fmtJpy(item.available_jpy)}</span>
            </div>
          ))}
        </div>
      )}
      <form onSubmit={submit} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 7 }}>
        <select aria-label="追加資金の種類" value={source} onChange={event => onSource(event.target.value as 'salary' | 'bonus' | 'other')} style={contributionInput}>
          <option value="salary">給与</option>
          <option value="bonus">ボーナス</option>
          <option value="other">その他の承認資金</option>
        </select>
        <input aria-label="追加資金の金額" value={amount} onChange={event => setAmount(event.target.value)} inputMode="numeric" placeholder="金額（円）" style={contributionInput} />
        <select aria-label="追加資金の枠" value={bucket} onChange={event => setBucket(event.target.value as 'normal' | 'opportunity')} style={contributionInput}>
          <option value="normal">通常プール</option>
          <option value="opportunity">機会プール</option>
        </select>
        <input aria-label="追加資金の分割月数" type="number" min="1" max="24" value={releaseMonths} onChange={event => setReleaseMonths(event.target.value)} style={contributionInput} />
        <select aria-label="追加資金の名義" value={owner} onChange={event => setOwner(event.target.value as 'husband' | 'wife')} style={contributionInput}>
          <option value="husband">夫</option>
          <option value="wife">妻</option>
        </select>
        <select aria-label="追加資金の証券会社" value={broker} onChange={event => setBroker(event.target.value as 'rakuten' | 'sbi')} style={contributionInput}>
          <option value="rakuten">楽天証券</option>
          <option value="sbi">SBI証券</option>
        </select>
        <button type="submit" disabled={saving} style={{ ...linkButton, gridColumn: '1 / -1', textAlign: 'left', opacity: saving ? .6 : 1 }}>
          {saving ? '保存中…' : '実入金を承認してプールへ追加 →'}
        </button>
      </form>
      {result && <p style={{ margin: '9px 0 0', color: result.ok ? OPS.green : OPS.vermilion, fontSize: 12, lineHeight: 1.55 }}>{result.text}</p>}
    </section>
  )
}

function newApprovalIdempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `contribution-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function ScheduleTab({ upcoming, todayStr, filters, onToggle }: { upcoming: AlmanacData['upcoming']; todayStr?: string; filters: Set<string>; onToggle: (kind: string) => void }) {
  return (
    <section style={railPanel}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginBottom: 12 }}>
        {EVENT_FILTERS.map(kind => {
          const active = filters.has(kind)
          return <button key={kind} onClick={() => onToggle(kind)} aria-pressed={active} style={{ ...filterButton, color: active ? KIND_COLOR[kind] : OPS.dim, borderColor: active ? `${KIND_COLOR[kind]}66` : OPS.hairline, background: active ? `${KIND_COLOR[kind]}18` : 'transparent' }}>{KIND_LABEL[kind]}</button>
        })}
      </div>
      <div style={{ maxHeight: 420, overflowY: 'auto' }}>
        {upcoming.map((event, index) => {
          const days = event.date && todayStr ? Math.round((new Date(event.date).getTime() - new Date(todayStr).getTime()) / 86400000) : null
          return (
            <div key={`${event.date}-${event.label}-${index}`} className="ops-row" style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '8px 2px', borderTop: index > 0 ? `1px solid ${OPS.hairline}` : 'none', fontSize: 13.5 }}>
              <span style={{ color: OPS.dim, fontFamily: OPS.mono, minWidth: 44 }}>{event.date?.slice(5).replace('-', '/')}</span>
              <span style={{ color: KIND_COLOR[event.kind] ?? OPS.sub }}>●</span>
              <span style={{ color: OPS.sub, minWidth: 0, flex: 1 }}>{event.label}</span>
              {days != null && (
                <span style={{ fontFamily: OPS.mono, fontSize: 12, color: days <= 7 ? OPS.amber : OPS.dim, flexShrink: 0 }}>
                  {days <= 0 ? '今日' : `${days}d`}
                </span>
              )}
            </div>
          )
        })}
        {upcoming.length === 0 && <p style={{ color: OPS.dim, fontSize: 13.5, margin: 0 }}>該当する予定はありません。</p>}
      </div>
    </section>
  )
}

function RecordTab({ trades, notes }: { trades: AlmanacData['past']; notes: string[] }) {
  return (
    <section style={railPanel}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
        {trades.map((trade, index) => (
          <div key={`${trade.date}-${trade.ticker}-${index}`} className="ops-row" style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '7px 2px', borderTop: index > 0 ? `1px solid ${OPS.hairline}` : 'none', fontSize: 13.5 }}>
            <span style={{ color: OPS.dim, fontFamily: OPS.mono, minWidth: 44 }}>{trade.date.slice(5).replace('-', '/')}</span>
            <span style={{ color: trade.side === 'buy' ? OPS.green : OPS.vermilion, fontFamily: OPS.mono }}>{trade.side === 'buy' ? '▲' : '▼'}</span>
            <span style={{ color: OPS.text, fontFamily: OPS.mono, minWidth: 48 }}>{trade.ticker}</span>
            <span style={{ color: OPS.dim, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{trade.detail}</span>
          </div>
        ))}
        {trades.length === 0 && <p style={{ color: OPS.dim, fontSize: 13.5, margin: 0 }}>最近の記録はありません。</p>}
      </div>
      <Link href="/executions" style={{ ...linkButton, display: 'inline-block', textDecoration: 'none', marginTop: 14 }}>執行台帳 →</Link>
      {notes.map((note, index) => <p key={index} style={{ color: OPS.dim, fontSize: 12, lineHeight: 1.65, margin: '10px 0 0' }}>※ {note}</p>)}
    </section>
  )
}

export function ExecutionPlanModal({ plan, open, onClose }: { plan?: ExecutionPlan; open: boolean; onClose: () => void }) {
  if (!plan || !open) return null
  const b = plan.budgets ?? {}
  const c = plan.consumption ?? {}
  const pct = c.normal_plan_budget_consumed_pct ?? 0
  const reasonRows = [
    ...(plan.no_action_rationale ?? []).map(rationaleText),
    ...plan.filtered_examples.map(item => `${item.ticker ?? '—'}: ${reasonCodeLabel(item.code)}${item.reason ? ` · ${item.reason}` : ''}`),
  ].filter(Boolean)
  const orderIntent = plan.order_intent_review ?? { count: 0, summary: {}, items: [] }

  return (
    <Modal open={open} onClose={onClose} width={940} fitViewport>
      <div style={{ overflowY: 'auto', paddingRight: 4 }}>
        <div style={{ fontFamily: OPS.mono, color: OPS.gold, fontSize: 12, letterSpacing: '0.12em', marginBottom: 8 }}>EXECUTION PLAN</div>
        <h3 style={{ color: decisionColor(plan), fontSize: 20, margin: '0 0 4px' }}>{plan.today_decision.label}</h3>
        <p style={{ color: OPS.sub, fontSize: 13, lineHeight: 1.7, margin: '0 0 16px' }}>{plan.today_decision.reason}</p>
        <p style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 11, margin: '-10px 0 16px' }}>{plan.status}{plan.age_hours != null ? ` · ${fmtAge(plan.age_hours)}` : ''}{plan.horizon.week_start && plan.horizon.week_end ? ` · ${plan.horizon.week_start.slice(5)}–${plan.horizon.week_end.slice(5)}` : ''}</p>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(145px, 1fr))', gap: 10 }}>
          <ModalStat label="月次枠" value={fmtJpy(b.monthly_total_jpy)} sub={b.budget_source} />
          <ModalStat label="月次残り" value={fmtJpy(c.monthly_remaining_jpy ?? b.monthly_remaining_jpy)} />
          <ModalStat label="週次通常枠" value={fmtJpy(b.weekly_normal_jpy)} />
          <ModalStat label="計画枠（通常）" value={`${pct.toFixed(1)}%`} sub={`${fmtJpy(c.normal_plan_budget_consumed_jpy)} · 残 ${fmtJpy(c.remaining_normal_jpy)}`} color={pct >= 100 ? OPS.amber : OPS.text} />
          <ModalStat label="通常の対応実額" value={fmtJpy(c.normal_matched_notional_jpy)} />
          <ModalStat label="通常の注文中" value={fmtJpy(c.normal_open_order_matched_notional_jpy)} color={OPS.amber} />
          <ModalStat label="通常の約定" value={fmtJpy(c.normal_filled_matched_notional_jpy)} color={OPS.green} />
          <ModalStat label="機会枠残り" value={fmtJpy(c.remaining_opportunity_jpy)} sub={(c.opportunity_matched_notional_jpy ?? 0) > 0 ? `対応 ${fmtJpy(c.opportunity_matched_notional_jpy)}` : undefined} color={OPS.blue} />
        </div>
        <div style={{ margin: '14px 0 18px' }}><Bar pct={pct} color={pct >= 100 ? OPS.amber : OPS.gold} height={7} /></div>

        {reasonRows.length > 0 && <ModalBlock title="判断・除外理由">{reasonRows.slice(0, 8).map((reason, index) => <p key={index} style={detailText}><span style={{ color: OPS.gold }}>•</span> {reason}</p>)}</ModalBlock>}
        {plan.warnings.length > 0 && <ModalBlock title="警告">{plan.warnings.map((warning, index) => <p key={index} style={{ ...detailText, color: OPS.amber }}>{warning}</p>)}</ModalBlock>}
        {(c.unattributed_monthly_total_count ?? 0) > 0 && <ModalBlock title="月次帰属"> <p style={{ ...detailText, color: OPS.amber }}>未帰属の注文・約定 {c.unattributed_monthly_total_count}件（{fmtJpy(c.unattributed_monthly_total_notional_jpy)}）は月次枠に未算入です。帰属確認が完了するまで計画ゲートは enforce へ昇格しません。</p></ModalBlock>}
        {Object.keys(plan.filtered_summary).length > 0 && <ModalBlock title="計画ゲート除外"><div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>{Object.entries(plan.filtered_summary).map(([code, count]) => <Chip key={code} color={OPS.dim} mono>{reasonCodeLabel(code)} {count}</Chip>)}</div></ModalBlock>}
        {orderIntent.count > 0 && <ModalBlock title={`REVIEW ONLY · 既存注文の確認 ${orderIntent.count}件`}><p style={detailText}>この欄は既存注文の確認専用で、発注・約定の操作はありません。</p>{Object.keys(orderIntent.summary).length > 0 && <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', margin: '7px 0' }}>{Object.entries(orderIntent.summary).map(([key, count]) => <Chip key={key} color={OPS.dim} mono>{key} {count}</Chip>)}</div>}{orderIntent.items.slice(0, 5).map((item, index) => <p key={`${item.existing_order_id ?? item.ticker}-${index}`} style={detailText}><span style={{ color: OPS.text, fontFamily: OPS.mono }}>{item.ticker ?? '—'}</span> · {item.label}{item.existing_order_status ? ` · ${item.existing_order_status}` : ''}{item.incremental_notional_jpy != null ? ` · 差分 ${fmtJpy(item.incremental_notional_jpy)}` : ''}{item.reason ? ` · ${item.reason}` : ''}</p>)}</ModalBlock>}
        {plan.gate_observation && <ModalBlock title={plan.gate_observation.mode === 'observe' ? 'OBSERVE' : '計画ゲート'}><p style={detailText}>計画ゲートは{plan.gate_observation.mode === 'observe' ? '記録のみ' : plan.gate_observation.mode ?? '—'}{plan.gate_observation.would_filter_count != null ? ` · enforce時の除外見込み ${plan.gate_observation.would_filter_count}件` : ''}{plan.gate_observation.batch_allocation?.over_budget_count ? ` · 最終枠超過 ${plan.gate_observation.batch_allocation.over_budget_count}件` : ''}</p>{plan.gate_observation.warning && <p style={{ ...detailText, color: OPS.amber }}>{plan.gate_observation.warning}</p>}{plan.gate_observation.batch_allocation && <p style={detailText}>バッチ配分 {plan.gate_observation.batch_allocation.applied ? '適用' : '未適用'} · 受入 {plan.gate_observation.batch_allocation.accepted_count ?? 0}件{plan.gate_observation.batch_allocation.error ? ` · ${plan.gate_observation.batch_allocation.error}` : ''}</p>}{plan.gate_observation.readiness && <p style={detailText}>昇格準備 {plan.gate_observation.readiness.ready_for_enforce ? '完了' : '観測中'} · {plan.gate_observation.readiness.trading_day_count ?? 0}日 / {plan.gate_observation.readiness.classification_count ?? 0}分類</p>}{plan.gate_observation.readiness?.blockers?.map(blocker => <p key={blocker} style={detailText}>• {blocker}</p>)}{Object.keys(plan.gate_observation.observed_decisions ?? {}).length > 0 && <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 7 }}>{Object.entries(plan.gate_observation.observed_decisions ?? {}).map(([key, count]) => <Chip key={key} color={OPS.dim} mono>{key} {count}</Chip>)}</div>}</ModalBlock>}
        {plan.items.length > 0 && <ModalBlock title="計画項目">{plan.items.map(item => <div key={item.plan_item_id ?? item.label} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, borderTop: `1px solid ${OPS.hairline}`, padding: '8px 0', color: item.status === 'covered' ? OPS.dim : OPS.sub, fontSize: 12.5 }}><span>{item.label}</span><span style={{ color: item.status === 'covered' ? OPS.dim : OPS.gold, fontFamily: OPS.mono }}>{fmtJpy(item.remaining_jpy)}</span></div>)}</ModalBlock>}
      </div>
    </Modal>
  )
}

function ModalBlock({ title, children }: { title: string; children: ReactNode }) {
  return <section style={{ borderTop: `1px solid ${OPS.hairline}`, paddingTop: 13, marginTop: 15 }}><div style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 11, letterSpacing: '0.08em', marginBottom: 7 }}>{title}</div>{children}</section>
}

function ModalStat({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return <div style={{ background: OPS.inset, border: `1px solid ${OPS.hairline}`, borderRadius: 8, padding: '10px 12px' }}><div style={{ color: OPS.dim, fontSize: 11, marginBottom: 5 }}>{label}</div><div style={{ color: color ?? OPS.text, fontFamily: OPS.mono, fontSize: 16, fontWeight: 700 }}>{value}</div>{sub && <div style={{ color: OPS.dim, fontSize: 10.5, marginTop: 4 }}>{sub}</div>}</div>
}

const railPanel: CSSProperties = { background: OPS.panel, border: `1px solid ${OPS.border}`, borderRadius: 9, padding: '13px 14px' }
const railTitle: CSSProperties = { color: OPS.gold, fontFamily: OPS.mono, fontSize: 12.5, letterSpacing: '0.1em', fontWeight: 600 }
const filterButton: CSSProperties = { border: '1px solid', borderRadius: 5, cursor: 'pointer', fontFamily: OPS.mono, fontSize: 11.5, padding: '5px 7px' }
const linkButton: CSSProperties = { background: 'none', border: 'none', color: OPS.gold, cursor: 'pointer', fontFamily: OPS.mono, fontSize: 12.5, padding: 0, marginTop: 12 }
const contributionInput: CSSProperties = { width: '100%', minWidth: 0, boxSizing: 'border-box', background: OPS.inset, border: `1px solid ${OPS.hairline}`, borderRadius: 5, color: OPS.text, fontFamily: OPS.mono, fontSize: 11.5, padding: '7px 8px' }
const detailText: CSSProperties = { color: OPS.sub, fontSize: 13, lineHeight: 1.65, margin: '4px 0' }

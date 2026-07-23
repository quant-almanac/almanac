'use client'
import { useState } from 'react'
import { useSWRConfig } from 'swr'
import { apiFetch } from '@/lib/api'
import { OPS, TYPE_META, fmtJpy, remainingLabel, rankGlyph, quadrantLabels, QUADRANT_COLOR } from './tokens'
import { SectionHead } from './Shell'
import { Modal } from './PageKit'
import { ExecutionPlanModal } from './PlanRail'
import OrderStrategyRefresh from './OrderStrategyRefresh'
import Sparkline from './Sparkline'
import ExecutionForm from './ExecutionForm'
import OrderMap, { type RejectedDecision } from './OrderMap'
import type { BoardRow, ChartsData, ExecutionPlan, ExecutionPlanRationale } from './types'

type FundingOption = NonNullable<NonNullable<ExecutionPlan['contributions']>['sources']>[number]

type PendingApplication = {
  id?: string
  ticker?: string
  account?: string
  investment_type?: string
  execution_owner?: string
  execution_broker?: string
  reasons: Array<{ code?: string; message?: string }>
  candidate_position_keys: string[]
}

const ACTION_SECTION_CSS = `
.orders-layout { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(300px,1fr); gap:20px; align-items:start; }
@container ops-content (max-width:900px) { .orders-layout { grid-template-columns:minmax(0,1fr); } }
`

export function formatExecutionPlanRationale(reason: string | ExecutionPlanRationale): string {
  if (typeof reason === 'string') return reason
  return reason.message ?? reason.reason_code ?? ''
}

/** lifecycle.status → 対応状態（要対応 か 対応済み か） */
function actedState(status: string): { label: string; color: string; acted: boolean } {
  switch (status) {
    case 'placed': return { label: '指値中', color: OPS.amber, acted: true }
    case 'filled': return { label: '約定済', color: OPS.green, acted: true }
    case 'cancelled': return { label: '見送り', color: OPS.dim, acted: true }
    case 'expired': return { label: '期限切れ', color: OPS.dim, acted: true }
    case 'reprice_required': return { label: '再評価待ち', color: OPS.amber, acted: true }
    default: return { label: '要対応', color: OPS.vermilion, acted: false }
  }
}

/** 「何を・どれだけ」を1行に: 売り 2株 @¥1,219 */
function whatHowMuch(row: BoardRow): { verb: string; verbColor: string; qty: string; price: string } {
  const t = row.type ? TYPE_META[row.type] : null
  const m = /([\d,]+(?:\.\d+)?)\s*(株|口)/.exec(row.amount_hint ?? '')
  const qty = m ? `${m[1]}${m[2]}` : (row.amount_hint ?? '')
  const sym = row.ticker?.endsWith('.T') ? '¥' : '$'
  const price = row.limit_price != null ? `@${sym}${row.limit_price}` : '成行'
  return { verb: t?.label ?? row.type ?? '—', verbColor: t?.color ?? OPS.text, qty, price }
}

export default function ActionSection({
  board,
  reviewBoard = [],
  notes,
  charts,
  backlog,
  executionPlan,
  selected,
  hovered,
  onSelect,
  onHover,
  rejectedDecisions = [],
  pendingPortfolioApplications = [],
}: {
  board: BoardRow[]
  reviewBoard?: BoardRow[]
  notes: { label: string; text: string }[]
  charts?: ChartsData
  backlog?: BoardRow[]
  executionPlan?: ExecutionPlan
  selected: number
  hovered?: number | null
  onSelect: (i: number) => void
  onHover?: (i: number | null) => void
  rejectedDecisions?: RejectedDecision[]
  pendingPortfolioApplications?: PendingApplication[]
}) {
  const [openIdx, setOpenIdx] = useState<number | null>(null)
  const [planModalOpen, setPlanModalOpen] = useState(false)
  const quads = quadrantLabels(board)
  const fundingOptions = executionPlan?.contributions?.sources?.filter(source => (source.available_jpy ?? 0) > 0) ?? []

  const indexed = board.map((row, i) => ({ row, i }))
  const todo = indexed.filter(x => !actedState(x.row.lifecycle.status).acted)
  const done = indexed.filter(x => actedState(x.row.lifecycle.status).acted)

  return (
    <section id="orders-section">
      <style dangerouslySetInnerHTML={{ __html: ACTION_SECTION_CSS }} />
      <SectionHead
        no="02"
        en="ORDERS"
        jp="今日の発注"
        right={<OrderStrategyRefresh />}
        note={
          <span>
            <span style={{ color: OPS.vermilion }}>要対応 {todo.length}</span>
            {done.length > 0 && <span style={{ color: OPS.dim }}> · 対応済み {done.length}</span>}
            {reviewBoard.length > 0 && <span style={{ color: OPS.amber }}> · 要確認 {reviewBoard.length}</span>}
          </span>
        }
      />
      {pendingPortfolioApplications.length > 0 && (
        <PendingApplicationsPanel items={pendingPortfolioApplications} />
      )}

      {executionPlan && <ExecutionPlanSummary plan={executionPlan} onOpen={() => setPlanModalOpen(true)} />}

      {board.length === 0 ? (
        <div className={rejectedDecisions.length > 0 ? 'orders-layout' : undefined} style={rejectedDecisions.length > 0 ? undefined : { display: 'grid', gridTemplateColumns: '1fr' }}>
          <p style={{ fontSize: 15, color: OPS.sub, lineHeight: 1.8, marginTop: executionPlan ? 14 : 0 }}>
            新規の発注はありません。{executionPlan?.today_decision?.reason ?? '観察を続けます。'}
          </p>
          {onHover && rejectedDecisions.length > 0 && (
            <OrderMap
              board={board}
              selected={selected}
              hovered={hovered ?? null}
              onSelect={onSelect}
              onHover={onHover}
              onOpen={i => setOpenIdx(i)}
              rejected={rejectedDecisions}
            />
          )}
        </div>
      ) : (
        <div className="orders-layout">
          {/* ── 発注リスト ── */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {todo.map(({ row, i }) => (
              <OrderRow
                key={`${row.ticker}-${row.rank ?? i}`}
                row={row} index={i} quadrant={quads[i]}
                hovered={hovered === i} selected={selected === i}
                onOpen={() => { onSelect(i); setOpenIdx(i) }}
                onHover={onHover}
              />
            ))}

            {done.length > 0 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '6px 0 2px' }}>
                <span style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.dim, letterSpacing: '0.08em' }}>対応済み {done.length}</span>
                <span style={{ flex: 1, height: 1, background: OPS.hairline }} />
              </div>
            )}
            {done.map(({ row, i }) => (
              <OrderRow
                key={`${row.ticker}-${row.rank ?? i}`}
                row={row} index={i} quadrant={quads[i]} dim
                hovered={hovered === i} selected={selected === i}
                onOpen={() => { onSelect(i); setOpenIdx(i) }}
                onHover={onHover}
              />
            ))}

            {notes.length > 0 && (
              <div style={{ marginTop: 8 }}>
                {notes.map(n => (
                  <p key={n.label} style={{ fontSize: 12.5, color: OPS.dim, lineHeight: 1.7, margin: '4px 0' }}>
                    <span style={{ color: OPS.sub }}>不実施 — {n.label}</span>: {n.text}
                  </p>
                ))}
              </div>
            )}
          </div>

          {/* ── 判断地図（発注と直結） ── */}
          {onHover && (
            <OrderMap
              board={board}
              selected={selected}
              hovered={hovered ?? null}
              onSelect={onSelect}
              onHover={onHover}
              onOpen={i => setOpenIdx(i)}
              rejected={rejectedDecisions}
            />
          )}
        </div>
      )}

      {reviewBoard.length > 0 && <ReviewPanel items={reviewBoard} />}

      {backlog && backlog.length > 0 && <BacklogPanel items={backlog} onOpen={setOpenIdx} board={board} />}

      {/* 詳細＋売買記録ポップアップ（2カラム・画面内に収める） */}
      <Modal open={openIdx != null} onClose={() => setOpenIdx(null)} width={940} fitViewport>
        {openIdx != null && board[openIdx] && (
          <OrderDetail
            key={`${board[openIdx].ticker}-${openIdx}`}
            row={board[openIdx]}
            index={openIdx}
            quadrant={quads[openIdx]}
            series={board[openIdx].ticker ? charts?.tickers?.[board[openIdx].ticker!] : undefined}
            onClose={() => setOpenIdx(null)}
            fundingOptions={fundingOptions}
          />
        )}
      </Modal>
      <ExecutionPlanModal plan={executionPlan} open={planModalOpen} onClose={() => setPlanModalOpen(false)} />
    </section>
  )
}

function PendingApplicationsPanel({ items }: { items: PendingApplication[] }) {
  return (
    <div style={{ marginBottom: 14, padding: '10px 12px', border: `1px solid ${OPS.amber}66`, borderRadius: 7, background: OPS.amberBg, color: OPS.amber, fontSize: 12.5, lineHeight: 1.6 }}>
      <div style={{ fontWeight: 700, marginBottom: 6 }}>約定事実は保存済み・台帳適用待ち {items.length}件</div>
      {items.map(item => <PendingApplicationRow key={item.id} item={item} />)}
    </div>
  )
}

function PendingApplicationRow({ item }: { item: PendingApplication }) {
  const { mutate } = useSWRConfig()
  const [owner, setOwner] = useState(item.execution_owner ?? '')
  const [broker, setBroker] = useState(item.execution_broker ?? '')
  const [positionKey, setPositionKey] = useState(item.candidate_position_keys[0] ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function resolve(resolution: 'apply' | 'externally_reconciled') {
    if (!item.id) return
    const source = resolution === 'externally_reconciled'
      ? window.prompt('外部照合元（楽天CSV、SBI CSVなど）を入力してください')
      : null
    if (resolution === 'externally_reconciled' && !source) return
    setBusy(true)
    setError('')
    try {
      const res = await apiFetch(`/api/actions/executions/${item.id}/resolve-portfolio`, {
        method: 'POST',
        body: JSON.stringify({
          resolution,
          execution_owner: owner || null,
          execution_broker: broker || null,
          account: item.account ?? null,
          investment_type: item.investment_type ?? null,
          execution_position_key: positionKey || null,
          external_reconcile_source: source,
        }),
      })
      const payload = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(payload.detail ?? `HTTP ${res.status}`)
      await mutate('/api/today')
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ borderTop: `1px solid ${OPS.amber}33`, padding: '8px 0', color: OPS.sub }}>
      <div>{item.ticker ?? '—'} — {item.reasons[0]?.message ?? '適用先の確認が必要です'}</div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
        <select aria-label={`${item.ticker} owner`} value={owner} onChange={e => setOwner(e.target.value)} style={pendingSelectStyle}>
          <option value="">名義を選択</option><option value="husband">本人</option><option value="wife">妻</option>
        </select>
        <select aria-label={`${item.ticker} broker`} value={broker} onChange={e => setBroker(e.target.value)} style={pendingSelectStyle}>
          <option value="">証券会社</option><option value="rakuten">楽天</option><option value="sbi">SBI</option>
        </select>
        {item.candidate_position_keys.length > 0 && (
          <select aria-label={`${item.ticker} position`} value={positionKey} onChange={e => setPositionKey(e.target.value)} style={pendingSelectStyle}>
            <option value="">保有を選択</option>
            {item.candidate_position_keys.map(key => <option key={key} value={key}>{key}</option>)}
          </select>
        )}
        <button disabled={busy || !owner || !broker} onClick={() => resolve('apply')} style={pendingButtonStyle}>台帳へ適用</button>
        <button disabled={busy || !owner || !broker} onClick={() => resolve('externally_reconciled')} style={pendingButtonStyle}>外部CSVで照合済み</button>
      </div>
      {error && <div style={{ color: OPS.redSoft, marginTop: 4 }}>{error}</div>}
    </div>
  )
}

const pendingSelectStyle: React.CSSProperties = {
  border: `1px solid ${OPS.border}`, background: OPS.inset, color: OPS.text,
  borderRadius: 5, padding: '4px 6px', fontSize: 12,
}
const pendingButtonStyle: React.CSSProperties = {
  border: `1px solid ${OPS.amber}66`, background: OPS.panel, color: OPS.amber,
  borderRadius: 5, padding: '4px 8px', fontSize: 12, cursor: 'pointer',
}

function ExecutionPlanSummary({ plan, onOpen }: { plan: ExecutionPlan; onOpen: () => void }) {
  const color = plan.today_decision.code === 'actions_available'
    ? OPS.green
    : plan.today_decision.code === 'disabled' || plan.today_decision.code === 'warning'
      ? OPS.amber
      : OPS.gold

  return (
    <div style={{ marginBottom: 12, display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap', borderBottom: `1px solid ${OPS.hairline}`, padding: '0 2px 9px' }}>
      <span style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 11.5, fontWeight: 600, letterSpacing: '0.1em' }}>EXECUTION PLAN</span>
      <span style={{ color, fontSize: 14, fontWeight: 700 }}>{plan.today_decision.label}</span>
      <span style={{ color: OPS.sub, fontSize: 12.5, minWidth: 0, flex: '1 1 280px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{plan.today_decision.reason}</span>
      <button onClick={onOpen} style={{ background: 'none', border: 'none', padding: 0, color: OPS.gold, fontFamily: OPS.mono, fontSize: 11.5, cursor: 'pointer' }}>計画の全文 →</button>
    </div>
  )
}

/* ── 発注リストの1行（大きな「何を・どれだけ」）── */
function OrderRow({
  row, index, quadrant, hovered, selected, dim, onOpen, onHover,
}: {
  row: BoardRow; index: number; quadrant: string | null
  hovered: boolean; selected: boolean; dim?: boolean
  onOpen: () => void; onHover?: (i: number | null) => void
}) {
  const state = actedState(row.lifecycle.status)
  const w = whatHowMuch(row)
  const remaining = remainingLabel(row.lifecycle.expiry_at)
  const highlight = hovered || selected

  return (
    <button
      onClick={onOpen}
      onMouseEnter={() => onHover?.(index)}
      onMouseLeave={() => onHover?.(null)}
      className={`ops-card${selected ? ' ops-linked' : ''}`}
      style={{
        textAlign: 'left',
        background: highlight ? OPS.panelAlt : OPS.panel,
        border: `1px solid ${highlight ? OPS.gold + '99' : state.acted ? OPS.hairline : OPS.border}`,
        borderLeft: `3px solid ${state.color}`,
        borderRadius: 10,
        padding: '13px 16px',
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        fontFamily: OPS.sans,
        color: OPS.text,
        opacity: dim ? 0.62 : 1,
      }}
    >
      {/* 連番 + 状態ランプ */}
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3, minWidth: 34, flexShrink: 0 }}>
        <span style={{ fontFamily: OPS.mono, fontSize: 18, color: w.verbColor, lineHeight: 1 }}>{rankGlyph(index)}</span>
        <span style={{ fontFamily: OPS.mono, fontSize: 10, color: state.color, whiteSpace: 'nowrap' }}>● {state.label}</span>
      </div>

      {/* 銘柄 + 何を・どれだけ */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontFamily: OPS.mono, fontSize: 20, fontWeight: 700, letterSpacing: '-0.01em' }}>{row.ticker}</span>
          <span style={{ fontSize: 17, fontWeight: 700, color: w.verbColor }}>{w.verb}</span>
          {w.qty && <span style={{ fontFamily: OPS.mono, fontSize: 17, fontWeight: 600, color: OPS.text }}>{w.qty}</span>}
          <span style={{ fontFamily: OPS.mono, fontSize: 15, color: OPS.gold }}>{w.price}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 5, fontFamily: OPS.mono, fontSize: 12, color: OPS.dim, flexWrap: 'wrap' }}>
          {row.confidence_pct != null && <span>確信 <span style={{ color: OPS.sub }}>{row.confidence_pct}%</span></span>}
          {row.estimated_notional_jpy != null && <span>想定 <span style={{ color: OPS.sub }}>{fmtJpy(row.estimated_notional_jpy)}</span></span>}
          {quadrant && <span style={{ color: QUADRANT_COLOR[quadrant] ?? OPS.dim }}>◎ {quadrant}</span>}
          {row.decision_price != null && row.limit_price != null && (
            <span>現値 {row.decision_price}（{row.limit_price >= row.decision_price ? '+' : ''}{(((row.limit_price - row.decision_price) / row.decision_price) * 100).toFixed(1)}%）</span>
          )}
        </div>
      </div>

      {/* 期限 + 開くヒント */}
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4, flexShrink: 0 }}>
        {row.lifecycle.expiry_deferred_until_reprice && row.lifecycle.market_reprice_after && (
          <span style={{ fontFamily: OPS.mono, fontSize: 11.5, color: OPS.amber }}>次回朝分析で再評価</span>
        )}
        {row.market_quote_confirmation_required && !state.acted && (
          <span style={{ fontFamily: OPS.mono, fontSize: 11.5, color: OPS.amber }}>発注時に現在値確認</span>
        )}
        {remaining && !state.acted && !row.lifecycle.expiry_deferred_until_reprice && (
          <span style={{ fontFamily: OPS.mono, fontSize: 12.5, color: remaining.over ? OPS.dim : OPS.amber }}>{remaining.label}</span>
        )}
        <span style={{ fontSize: 11.5, color: OPS.gold }}>詳細・記録 →</span>
      </div>
    </button>
  )
}

/* ── ポップアップ本文（詳細 + 売買記録）── */
function OrderDetail({
  row, index, quadrant, series, onClose, fundingOptions,
}: {
  row: BoardRow; index: number; quadrant: string | null
  series?: { d: string; c: number }[]; onClose: () => void
  fundingOptions: FundingOption[]
}) {
  const { mutate } = useSWRConfig()
  const state = actedState(row.lifecycle.status)
  const w = whatHowMuch(row)
  // An approved contribution is bound to a real owner/broker route.  Hiding
  // nonmatching sources is friendlier than letting the form submit a request
  // that the server must correctly reject.
  const eligibleFundingOptions = row.execution_owner && row.execution_broker
    ? fundingOptions.filter(option => option.owner === row.execution_owner && option.broker === row.execution_broker)
    : []
  const [quickBusy, setQuickBusy] = useState<'cancelled' | null>(null)
  const canDismiss = Boolean(row.lifecycle.id) && row.lifecycle.status === 'pending'

  async function dismiss() {
    if (!row.lifecycle.id) return
    setQuickBusy('cancelled')
    try {
      await apiFetch(`/api/actions/status/${row.lifecycle.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'cancelled', note: 'コンソールで見送り' }),
      })
      mutate('/api/today')
    } finally {
      setQuickBusy(null)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, gap: 14 }}>
      {/* ヘッダー（全幅・固定） */}
      <div style={{ flexShrink: 0, paddingRight: 24 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap', marginBottom: 8 }}>
          <span style={{ fontFamily: OPS.mono, fontSize: 15, color: w.verbColor }}>{rankGlyph(index)}</span>
          <span style={{ fontFamily: OPS.mono, fontSize: 24, fontWeight: 700 }}>{row.ticker}</span>
          <span style={{ fontFamily: OPS.mono, fontSize: 12, color: state.color, border: `1px solid ${state.color}44`, borderRadius: 5, padding: '2px 9px' }}>● {state.label}</span>
          <span style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginLeft: 'auto' }}>
            <span style={{ fontSize: 22, fontWeight: 700, color: w.verbColor }}>{w.verb}</span>
            {w.qty && <span style={{ fontFamily: OPS.mono, fontSize: 22, fontWeight: 700, color: OPS.text }}>{w.qty}</span>}
            <span style={{ fontFamily: OPS.mono, fontSize: 20, color: OPS.gold }}>{w.price}</span>
          </span>
        </div>
        <div style={{ display: 'flex', gap: 16, fontFamily: OPS.mono, fontSize: 12.5, color: OPS.sub, flexWrap: 'wrap' }}>
          {row.confidence_pct != null && <span>確信度 {row.confidence_pct}%</span>}
          {row.estimated_notional_jpy != null && <span>想定 {fmtJpy(row.estimated_notional_jpy)}</span>}
          {quadrant && <span style={{ color: QUADRANT_COLOR[quadrant] ?? OPS.dim }}>◎ {quadrant}</span>}
          {row.order_type && <span>{row.order_type}</span>}
          {(row.target_5d_pct != null || row.target_20d_pct != null) && (
            <span>目標 5d {fmtPct(row.target_5d_pct)} / 20d {fmtPct(row.target_20d_pct)}</span>
          )}
        </div>
      </div>

      {/* 2カラム本文（各カラムが必要時のみ内部スクロール） */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 22, minHeight: 0, flex: 1, overflow: 'hidden' }}>
        {/* 左: 判断の中身 */}
        <div style={{ overflowY: 'auto', minHeight: 0, paddingRight: 6, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {series && series.length > 1 && (
            <div style={{ marginBottom: 4 }}><Sparkline series={series} limit={row.limit_price} height={48} /></div>
          )}
          <DetailRow label="根拠">{row.reason ?? '—'}</DetailRow>
          {row.execution_reason && <DetailRow label="執行">{row.execution_reason}</DetailRow>}
          {row.execution_note && <DetailRow label="注記" color={OPS.amber}>{row.execution_note}</DetailRow>}
          {(row.execution_advisories ?? []).map((advisory, advisoryIndex) => (
            <DetailRow key={`${advisory.code ?? 'advisory'}-${advisoryIndex}`} label="発注" color={OPS.amber}>
              {advisory.message ?? '発注時に現在値・スプレッドを確認してください'}
            </DetailRow>
          ))}
          {row.cooldown_warning && <DetailRow label="重複" color={OPS.amber}>{row.cooldown_warning}</DetailRow>}
          {row.execution_plan_decision && (
            <DetailRow label="計画" color={row.execution_plan_override ? OPS.gold : OPS.sub}>
              {planDecisionLabel(row.execution_plan_decision)}
              {row.plan_item_id && <span> · {row.plan_item_id}</span>}
              {row.plan_remaining_before_jpy != null && row.plan_remaining_after_jpy != null && (
                <span> · 残 {fmtJpy(row.plan_remaining_before_jpy)} → {fmtJpy(row.plan_remaining_after_jpy)}</span>
              )}
              {row.override_reason && <span> · {row.override_reason}</span>}
            </DetailRow>
          )}
        </div>

        {/* 右: 対応・売買記録 */}
        <div style={{ overflowY: 'auto', minHeight: 0, paddingRight: 4, borderLeft: `1px solid ${OPS.hairline}`, paddingLeft: 22 }}>
          {canDismiss && (
            <div style={{ display: 'flex', gap: 8, marginBottom: 4, flexWrap: 'wrap' }}>
              <button onClick={dismiss} disabled={quickBusy != null} style={quickBtn}>
                {quickBusy === 'cancelled' ? '…' : '見送る'}
              </button>
            </div>
          )}
          <ExecutionForm row={row} onClose={onClose} fundingOptions={eligibleFundingOptions} />
        </div>
      </div>
    </div>
  )
}

function ReviewPanel({ items }: { items: BoardRow[] }) {
  return (
    <div style={{ marginTop: 20, borderTop: `1px solid ${OPS.hairline}`, paddingTop: 14 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }}>
        <span style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.amber, letterSpacing: '0.08em' }}>要確認 {items.length}件</span>
        <span style={{ color: OPS.dim, fontSize: 12.5 }}>発注操作は無効です。理由を解消して再分析してください。</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {items.map((row, index) => {
          const reasons = row.execution_block_reasons ?? []
          return (
            <div key={`${row.ticker}-${row.type}-${index}`} className="ops-card" style={{ background: OPS.inset, border: `1px solid ${OPS.amber}44`, borderLeft: `3px solid ${OPS.amber}`, borderRadius: 8, padding: '11px 14px' }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
                <span style={{ fontFamily: OPS.mono, fontSize: 16, fontWeight: 700, color: OPS.text }}>{row.ticker}</span>
                <span style={{ color: OPS.amber, fontSize: 12, fontFamily: OPS.mono }}>{row.execution_readiness === 'blocked' ? 'BLOCKED' : 'REVIEW'}</span>
                {row.execution_plan_would_filter && <span style={{ color: OPS.dim, fontSize: 12 }}>計画observeでは除外判定</span>}
              </div>
              {row.action && <div style={{ color: OPS.sub, fontSize: 13.5, marginTop: 5 }}>{row.action}</div>}
              <div style={{ color: OPS.dim, fontSize: 12.5, lineHeight: 1.65, marginTop: 5 }}>
                {reasons.length > 0
                  ? reasons.map(reason => reason.message ?? reason.code ?? '要確認').join(' / ')
                  : '実行前の確認が必要です'}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function DetailRow({ label, children, color }: { label: string; children: React.ReactNode; color?: string }) {
  return (
    <div style={{ display: 'flex', gap: 14, fontSize: 14, lineHeight: 1.85, marginBottom: 8 }}>
      <span style={{ flexShrink: 0, width: 40, color: OPS.gold, fontFamily: OPS.mono, fontWeight: 600, fontSize: 13, paddingTop: 1 }}>{label}</span>
      <span style={{ color: color ?? OPS.sub }}>{children}</span>
    </div>
  )
}

function fmtPct(v?: number): string {
  if (v == null) return '—'
  return `${v > 0 ? '+' : ''}${v}%`
}

/* ── 積み残し ── */
function BacklogPanel({ items, onOpen, board }: { items: BoardRow[]; onOpen: (i: number) => void; board: BoardRow[] }) {
  const [open, setOpen] = useState(false)
  const oldest = items[0]
  // backlog は board に無いので個別モーダルは開かない（表示のみ）。将来的に board 統合。
  void onOpen; void board
  const [openRow, setOpenRow] = useState<string | null>(null)

  return (
    <div style={{ marginTop: 20, borderTop: `1px solid ${OPS.hairline}`, paddingTop: 14 }}>
      <button onClick={() => setOpen(!open)} style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: 13.5, color: OPS.amber, fontFamily: OPS.mono, letterSpacing: '0.04em' }}>
        {open ? '▾' : '▸'} 積み残し {items.length} 件
        <span style={{ color: OPS.dim, fontFamily: OPS.sans, letterSpacing: 0, marginLeft: 10 }}>
          — 最古 {oldest.ticker}（{oldest.days_pending}日前の提案が未処理）
        </span>
      </button>
      {open && (
        <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column' }}>
          {items.map((row, i) => {
            const t = row.type ? TYPE_META[row.type] : null
            const key = `${row.ticker}-${row.lifecycle.id ?? i}`
            const rowOpen = openRow === key
            return (
              <div key={key} style={{ borderTop: i > 0 ? `1px solid ${OPS.hairline}` : 'none' }}>
                <div role="button" tabIndex={0} aria-expanded={rowOpen} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setOpenRow(rowOpen ? null : key) } }} onClick={() => setOpenRow(rowOpen ? null : key)} className="ops-row" style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '9px 4px', cursor: 'pointer', fontSize: 13.5 }}>
                  <span style={{ color: OPS.dim, fontSize: 11 }}>{rowOpen ? '▾' : '▸'}</span>
                  <span style={{ fontFamily: OPS.mono, fontWeight: 600, color: OPS.text, minWidth: 64 }}>{row.ticker}</span>
                  {t && <span style={{ color: t.color, minWidth: 60 }}>{t.label}</span>}
                  <span style={{ color: OPS.sub, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.action}</span>
                  <span style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.amber, flexShrink: 0 }}>{row.days_pending}日前</span>
                </div>
                {rowOpen && (
                  <div style={{ background: OPS.inset, border: `1px solid ${OPS.border}`, borderRadius: 8, padding: '14px 18px', margin: '0 0 8px' }}>
                    <DetailRow label="根拠">{row.reason ?? '—'}</DetailRow>
                    <ExecutionForm row={row} onClose={() => setOpenRow(null)} historical />
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function planDecisionLabel(code: string): string {
  const map: Record<string, string> = {
    plan_new_order: '計画内の新規注文',
    opportunistic_override: '機会枠 override',
    defensive_or_exit_outside_plan: '防御/出口 override',
    plan_consumed_by_open_order: '既存注文で消費済み',
    plan_wait_for_better_candidate: 'より良い候補待ち',
    plan_over_budget: '計画枠超過',
    plan_unmatched_no_override: '計画外',
  }
  return map[code] ?? code.replaceAll('_', ' ')
}

const quickBtn: React.CSSProperties = {
  background: 'none', border: `1px solid ${OPS.hairline}`, borderRadius: 6, color: OPS.sub,
  fontSize: 13, padding: '7px 14px', cursor: 'pointer', fontFamily: OPS.sans,
}

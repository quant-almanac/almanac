'use client'

import { useState } from 'react'
import { Chip } from './PageKit'
import { OPS, fmtJpy } from './tokens'
import { fundingRouteLabel, fundingRoutes, needsFx } from './fundingRoutes'
import type { TodayOps } from './types'

type Dict = Record<string, unknown>

function asRecord(value: unknown): Dict {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {}
}

function nativeAmount(value: unknown, currency: unknown): string {
  const amount = Number(value)
  if (!Number.isFinite(amount)) return '—'
  if (currency === 'JPY') return fmtJpy(amount)
  return new Intl.NumberFormat('en-US', {
    style: 'currency', currency: String(currency || 'USD'), maximumFractionDigits: 0,
  }).format(amount)
}

function statusColor(status: unknown): string {
  return status === 'explainable' || status === 'healthy' || status === 'confirmed' ? OPS.green : OPS.amber
}

export default function AllocatorAuditPanel({ data }: { data: TodayOps }) {
  const [reviewed, setReviewed] = useState<string | null>(null)
  const [reviewError, setReviewError] = useState<string | null>(null)
  const allocator = asRecord(data.capital_allocator)
  const comparison = asRecord(data.capital_allocator_comparison)
  const optimizer = asRecord(data.optimizer_health)
  const wallets = data.cash_status ?? []
  const funding = fundingRoutes(data.funding_alternatives)
  const runId = typeof comparison.run_id === 'string' ? comparison.run_id : null
  const explanation = String(comparison.explanation_status || 'not_recorded')
  const reasons = Array.isArray(comparison.explanation_reasons) ? comparison.explanation_reasons : []

  async function review(decision: 'approved' | 'rejected') {
    if (!runId) return
    setReviewError(null)
    try {
      const response = await fetch(`/api/allocator-comparisons/${encodeURIComponent(runId)}/review`, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ decision }),
      })
      if (!response.ok) throw new Error('記録できませんでした')
      setReviewed(decision)
    } catch (error) {
      setReviewError(error instanceof Error ? error.message : '記録できませんでした')
    }
  }

  if (!Object.keys(allocator).length && !wallets.length && !Object.keys(optimizer).length) return null

  return (
    <section aria-label="資本配分の監査" style={{ marginTop: 16, border: `1px solid ${OPS.hairline}`, borderRadius: 10, background: OPS.panel, overflow: 'hidden' }}>
      <header style={{ display: 'flex', gap: 9, alignItems: 'center', padding: '12px 15px', borderBottom: `1px solid ${OPS.hairline}` }}>
        <span className="ops-latin" style={{ fontSize: 10, color: OPS.dim }}>CAPITAL ALLOCATION</span>
        <span style={{ color: OPS.text, fontWeight: 600 }}>資本配分・現金監査</span>
        <span style={{ marginLeft: 'auto' }}><Chip color={statusColor(explanation)} bg={explanation === 'explainable' ? OPS.greenBg : OPS.amberBg} mono>{explanation === 'explainable' ? '差分説明可能' : '差分確認要'}</Chip></span>
      </header>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(240px, .8fr) minmax(0, 1.2fr)', gap: 0 }}>
        <div style={{ padding: 15, borderRight: `1px solid ${OPS.hairline}` }}>
          <div className="ops-latin" style={{ fontSize: 9, color: OPS.dim }}>ALLOCATOR</div>
          <div style={{ marginTop: 6, color: OPS.text, fontWeight: 600 }}>{String(allocator.mode || 'legacy').toUpperCase()} / 通常買付 {String(allocator.selected_count ?? 0)}件</div>
          <div style={{ marginTop: 7, color: OPS.sub, fontSize: 12, lineHeight: 1.55 }}>
            上限 {nativeAmount(allocator.normal_action_cap_jpy, 'JPY')} ・選定 {String(allocator.selected_ticker || 'なし')}
          </div>
          {reasons.length > 0 && <div style={{ marginTop: 8, color: OPS.amber, fontSize: 12 }}>差分理由: {reasons.map(String).join(' / ')}</div>}
          {runId && !reviewed && <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <button type="button" onClick={() => void review('approved')} style={buttonStyle(OPS.green)}>差分を確認済み</button>
            <button type="button" onClick={() => void review('rejected')} style={buttonStyle(OPS.amber)}>再確認にする</button>
          </div>}
          {reviewed && <div style={{ marginTop: 10, color: OPS.green, fontSize: 12 }}>レビューを{reviewed === 'approved' ? '承認' : '差戻し'}として記録しました。</div>}
          {reviewError && <div style={{ marginTop: 10, color: OPS.redSoft, fontSize: 12 }}>{reviewError}</div>}
        </div>

        <div style={{ padding: 15 }}>
          <div className="ops-latin" style={{ fontSize: 9, color: OPS.dim }}>WALLETS / ORDERABLE CASH</div>
          {wallets.length === 0 ? <div style={{ color: OPS.sub, fontSize: 12, marginTop: 8 }}>wallet情報は次回分析で生成されます。</div> : <div style={{ display: 'grid', gap: 7, marginTop: 8 }}>
            {wallets.map(wallet => <div key={wallet.wallet_key || wallet.key} style={{ display: 'grid', gridTemplateColumns: 'minmax(150px,1fr) auto auto', gap: 12, alignItems: 'center', fontSize: 12, borderTop: `1px solid ${OPS.hairline}`, paddingTop: 7 }}>
              <span style={{ color: OPS.text }}>{wallet.owner}/{wallet.broker} · {wallet.currency}</span>
              <span style={{ color: OPS.sub }}>発注余力 {nativeAmount(wallet.available_for_new_buy, wallet.currency)}</span>
              <span style={{ color: wallet.projected_balance != null ? OPS.text : OPS.dim }}>投影 {nativeAmount(wallet.projected_balance, wallet.currency)}</span>
            </div>)}
          </div>}
          {Object.keys(optimizer).length > 0 && <div style={{ marginTop: 12, paddingTop: 10, borderTop: `1px solid ${OPS.hairline}`, color: OPS.sub, fontSize: 12 }}>
            Optimizer: <span style={{ color: statusColor(optimizer.health) }}>{String(optimizer.health || 'unknown')}</span>
            {optimizer.recommended != null && <> ・推薦 {String(optimizer.recommended)}</>}
          </div>}
        </div>
      </div>
      {funding.length > 0 && <div style={{ padding: '12px 15px 15px', borderTop: `1px solid ${OPS.hairline}` }}>
        <div className="ops-latin" style={{ fontSize: 9, color: OPS.dim }}>FUND → REPRICE → BUY</div>
        <div style={{ marginTop: 5, color: OPS.sub, fontSize: 12, lineHeight: 1.55 }}>
          資金移動は自動発注ではありません。実績確認後に価格・NISA枠を再評価してから買付をpreflightします。
        </div>
        <div style={{ display: 'grid', gap: 8, marginTop: 10 }}>
          {funding.map(({ ticker, workflow, requirement }) => {
            const isFx = needsFx(workflow)
            const quantity = requirement.target_quantity == null ? '—' : String(requirement.target_quantity)
            const needed = isFx
              ? `${nativeAmount(workflow.minimum_fx_native, 'USD')}（約${nativeAmount(workflow.minimum_transfer_native, 'JPY')}）`
              : nativeAmount(workflow.minimum_transfer_native, 'JPY')
            const label = requirement.kind === 'minimum_executable' ? '最小実行数量' : '元の提案数量'
            return <div key={`${ticker}-${String(requirement.kind)}-${String(workflow.source_wallet_key)}`} style={{ borderTop: `1px solid ${OPS.hairline}`, paddingTop: 8, display: 'grid', gridTemplateColumns: 'minmax(130px, .75fr) minmax(0, 1.25fr) auto', gap: 10, alignItems: 'center', fontSize: 12 }}>
              <span style={{ color: OPS.text }}>{ticker} · {label} {quantity}</span>
              <span style={{ color: OPS.sub }}>{fundingRouteLabel(workflow)}。最低 {needed}</span>
              <Chip color={OPS.amber} bg={OPS.amberBg} mono>再評価後に買付</Chip>
            </div>
          })}
        </div>
      </div>}
    </section>
  )
}

function buttonStyle(color: string) {
  return {
    border: `1px solid ${color}66`, background: 'transparent', color, borderRadius: 5,
    padding: '5px 8px', fontSize: 11, cursor: 'pointer',
  }
}

'use client'
import { useRef, useState } from 'react'
import { useSWRConfig } from 'swr'
import { apiFetch, apiErrorMessage } from '@/lib/api'
import { OPS } from './tokens'
import type { BoardRow } from './types'

type FundingOption = {
  id: string
  source?: string
  bucket?: string
  owner?: string
  broker?: string
  available_jpy?: number
}

const DIRECTIONS = [
  { value: 'buy', label: '買い' },
  { value: 'sell', label: '売り' },
  { value: 'hold', label: '保持' },
  { value: 'margin_buy', label: '信用買い' },
  { value: 'short', label: '空売り' },
  { value: 'cover', label: '買戻し' },
]
const ACCOUNTS = ['特定', '一般', 'NISA成長投資枠', 'NISAつみたて投資枠', '信用', '持株会']
const INV_TYPES = [
  { value: 'long', label: 'Long' },
  { value: 'medium', label: 'Medium' },
  { value: 'swing', label: 'Swing' },
]
const STATUSES = [
  { value: 'executed', label: '約定済み' },
  { value: 'partial', label: '一部約定' },
  { value: 'ordered', label: '発注のみ（未約定）' },
  { value: 'cancelled', label: '見送り・取消' },
]

function inferDirection(row: BoardRow): string {
  switch (row.type) {
    case 'buy':
    case 'add':
      return 'buy'
    case 'trim':
    case 'sell':
    case 'hedge':
      return 'sell'
    default:
      return 'hold'
  }
}
function inferAccount(row: BoardRow): string {
  if (row.execution_account && ACCOUNTS.includes(row.execution_account)) return row.execution_account
  const text = `${row.action ?? ''} ${row.amount_hint ?? ''}`
  if (text.includes('NISA成長')) return 'NISA成長投資枠'
  if (text.includes('つみたて')) return 'NISAつみたて投資枠'
  if (text.includes('持株会')) return '持株会'
  if (text.includes('信用')) return '信用'
  if (text.includes('一般')) return '一般'
  return '特定'
}
function inferInvType(row: BoardRow): string {
  if (row.execution_investment_type && INV_TYPES.some(x => x.value === row.execution_investment_type)) {
    return row.execution_investment_type
  }
  const t = (row.tier ?? '').toLowerCase()
  return INV_TYPES.some(x => x.value === t) ? t : 'medium'
}
function inferQuantity(row: BoardRow): string {
  const m = /([\d,]+(?:\.\d+)?)\s*(?:株|口)/.exec(row.amount_hint ?? '')
  return m ? m[1].replace(/,/g, '') : ''
}
function inferSellAll(row: BoardRow): boolean {
  const text = `${row.action ?? ''} ${row.amount_hint ?? ''}`
  return /全(株|部|量)|全て売却/.test(text)
}

/**
 * 売買記録フォーム — POST /api/actions/execute で記録し、記録後は
 * PATCH /api/actions/executions/{id} で価格/数量/状態/備考を修正できる。
 */
export default function ExecutionForm({ row, onClose, historical = false, fundingOptions = [] }: { row: BoardRow; onClose: () => void; historical?: boolean; fundingOptions?: FundingOption[] }) {
  const { mutate } = useSWRConfig()
  const [direction, setDirection] = useState(inferDirection(row))
  const [status, setStatus] = useState<'executed' | 'partial' | 'ordered' | 'cancelled'>(historical ? 'cancelled' : 'executed')
  const [price, setPrice] = useState(() => String(row.limit_price ?? row.decision_price ?? ''))
  const [quantity, setQuantity] = useState(inferQuantity(row))
  const [sellAll, setSellAll] = useState(inferSellAll(row))
  const [account, setAccount] = useState(inferAccount(row))
  const [investmentType, setInvestmentType] = useState(inferInvType(row))
  const [currency, setCurrency] = useState<'JPY' | 'USD'>(row.ticker?.endsWith('.T') ? 'JPY' : 'USD')
  const [note, setNote] = useState('')
  const [contributionId, setContributionId] = useState('')
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [recordedId, setRecordedId] = useState<string | null>(null) // 記録済み → 修正対象
  const idempotencyKey = useRef(
    globalThis.crypto?.randomUUID?.() ?? `execution-${Date.now()}-${Math.random().toString(16).slice(2)}`,
  )

  const isMargin = direction === 'margin_buy' || direction === 'short' || direction === 'cover'
  const isRiskIncreasing = direction === 'buy' || direction === 'margin_buy'
  const quantityUnit = row.ticker === '1489.T' || row.ticker === '1306.T' ? '口' : '株'
  const needsPriceQty = status !== 'cancelled'
  const editMode = recordedId != null

  async function handleSave() {
    setSaving(true)
    setResult(null)
    try {
      const res = await apiFetch('/api/actions/execute', {
        method: 'POST',
        body: JSON.stringify({
          ticker: row.ticker, direction, action: row.action ?? '', status,
          price: needsPriceQty && price ? parseFloat(price) : null,
          quantity: needsPriceQty && !sellAll && quantity ? parseFloat(quantity) : null,
          sell_all: needsPriceQty ? sellAll : false,
          note, account, investment_type: investmentType, currency,
          order_type: row.order_type ?? null, limit_price: row.limit_price ?? null,
          decision_price: row.decision_price ?? null,
          ai_recommended_order_type: row.order_type ?? null, ai_recommended_limit: row.limit_price ?? null,
          analysis_id: row.analysis_id ?? null,
          action_state_id: row.action_state_id ?? row.lifecycle.id ?? null,
          execution_owner: row.execution_owner ?? null,
          execution_broker: row.execution_broker ?? null,
          execution_position_keys: row.execution_position_keys ?? null,
          contribution_id: contributionId || null,
          idempotency_key: idempotencyKey.current,
        }),
      })
      const json = await res.json().catch(() => ({}))
      if (!res.ok || json?.ok === false) {
        setResult({ ok: false, message: apiErrorMessage(json, `保存失敗: HTTP ${res.status}`) })
        return
      }
      setRecordedId(json.id ?? null)
      setResult({ ok: true, message: json.portfolio?.message ?? '記録しました。内容は下で修正できます。' })
      mutate('/api/today')
    } catch (err) {
      setResult({ ok: false, message: `保存失敗: ${String(err)}` })
    } finally {
      setSaving(false)
    }
  }

  async function handlePatch() {
    if (!recordedId) return
    setSaving(true)
    setResult(null)
    try {
      const res = await apiFetch(`/api/actions/executions/${recordedId}`, {
        method: 'PATCH',
        body: JSON.stringify({
          price: needsPriceQty && price ? parseFloat(price) : null,
          quantity: needsPriceQty && !sellAll && quantity ? parseFloat(quantity) : null,
          status, note, currency,
        }),
      })
      const json = await res.json().catch(() => ({}))
      if (!res.ok || json?.ok === false) {
        setResult({ ok: false, message: apiErrorMessage(json, `修正失敗: HTTP ${res.status}`) })
        return
      }
      setResult({ ok: true, message: '修正を保存しました。' })
      mutate('/api/today')
    } catch (err) {
      setResult({ ok: false, message: `修正失敗: ${String(err)}` })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{ marginTop: 14, paddingTop: 14, borderTop: `1px dashed ${OPS.hairline}` }}>
      <div style={{ fontFamily: OPS.mono, fontSize: 13, color: OPS.gold, letterSpacing: '0.1em', fontWeight: 600, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 10 }}>
        {editMode ? '記録を修正' : '売買を記録'}
        {editMode && (
          <span style={{ fontFamily: OPS.sans, fontSize: 11, color: OPS.green, letterSpacing: 0, background: OPS.greenBg, border: `1px solid ${OPS.green}44`, borderRadius: 5, padding: '2px 8px' }}>
            ✓ 記録済み — このまま数値を直して再保存できます
          </span>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 12 }}>
        <Field label="方向">
          <select value={direction} onChange={e => setDirection(e.target.value)} style={selSt} disabled={editMode}>
            {DIRECTIONS.map(d => <option key={d.value} value={d.value}>{d.label}</option>)}
          </select>
        </Field>
        <Field label="状態">
          <select value={status} onChange={e => setStatus(e.target.value as typeof status)} style={selSt}>
            {STATUSES.filter(s => !(historical && s.value === 'ordered')).map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </Field>
        <Field label="口座">
          <select value={account} onChange={e => setAccount(e.target.value)} style={selSt} disabled={editMode}>
            {ACCOUNTS.map(a => <option key={a} value={a}>{a}</option>)}
          </select>
        </Field>
        <Field label="投資区分">
          <select value={investmentType} onChange={e => setInvestmentType(e.target.value)} style={selSt} disabled={editMode}>
            {INV_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </Field>
      </div>

      {needsPriceQty && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 12 }}>
          <Field label={isMargin ? '建値' : '約定価格'}>
            <input value={price} onChange={e => setPrice(e.target.value)} placeholder="例: 1213.9" style={inputSt} />
          </Field>
          <Field label={`数量（${quantityUnit}）`}>
            <input value={sellAll ? '' : quantity} onChange={e => setQuantity(e.target.value)} disabled={sellAll} placeholder={`${quantityUnit}数`} style={{ ...inputSt, opacity: sellAll ? 0.4 : 1 }} />
          </Field>
          <Field label="通貨">
            <select value={currency} onChange={e => setCurrency(e.target.value as 'JPY' | 'USD')} style={selSt}>
              <option value="JPY">JPY</option>
              <option value="USD">USD</option>
            </select>
          </Field>
          {(direction === 'sell' || direction === 'cover') && (
            <Field label="全株">
              <label style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 13, color: OPS.sub, height: 34 }}>
                <input type="checkbox" checked={sellAll} onChange={e => setSellAll(e.target.checked)} />
                全株売却
              </label>
            </Field>
          )}
        </div>
      )}

      {isRiskIncreasing && !historical && fundingOptions.length > 0 && (
        <Field label="承認済み追加資金（任意）">
          <select aria-label="承認済み追加資金（任意）" value={contributionId} onChange={e => setContributionId(e.target.value)} style={selSt} disabled={editMode}>
            <option value="">選択しない（約定事実のみ記録）</option>
            {fundingOptions.map(option => (
              <option key={option.id} value={option.id}>
                {option.source === 'bonus' ? 'ボーナス' : option.source === 'salary' ? '給与' : '承認資金'} · {option.bucket === 'opportunity' ? '機会' : '通常'} · 残¥{Math.round(option.available_jpy ?? 0).toLocaleString()}
              </option>
            ))}
          </select>
          <span style={{ color: OPS.dim, fontSize: 11.5, lineHeight: 1.5 }}>選択すると、この約定だけが承認済み資金の消化として記録されます。実績の後追い登録は未選択のまま保存できます。</span>
        </Field>
      )}

      <Field label="備考（任意）">
        <input value={note} onChange={e => setNote(e.target.value)} placeholder="楽天証券での約定メモなど" style={inputSt} />
      </Field>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
        <button
          onClick={editMode ? handlePatch : handleSave}
          disabled={saving}
          style={{ background: OPS.goldBg, border: `1px solid ${OPS.gold}88`, borderRadius: 6, color: OPS.gold, fontSize: 14, fontWeight: 600, padding: '9px 20px', cursor: saving ? 'default' : 'pointer', opacity: saving ? 0.6 : 1 }}
        >
          {saving ? '保存中…' : editMode ? '修正を保存' : '記録する'}
        </button>
        <button onClick={onClose} style={{ background: 'none', border: `1px solid ${OPS.hairline}`, borderRadius: 6, color: OPS.dim, fontSize: 14, padding: '9px 16px', cursor: 'pointer' }}>
          閉じる
        </button>
        {result && <span style={{ fontSize: 13, color: result.ok ? OPS.green : OPS.redSoft }}>{result.message}</span>}
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <span style={{ fontSize: 12, color: OPS.dim, fontFamily: OPS.mono }}>{label}</span>
      {children}
    </label>
  )
}

const selSt: React.CSSProperties = {
  background: OPS.panel, border: `1px solid ${OPS.border}`, borderRadius: 5, color: OPS.text,
  fontSize: 14, padding: '8px 10px', fontFamily: OPS.sans, outline: 'none',
}
const inputSt: React.CSSProperties = {
  background: OPS.panel, border: `1px solid ${OPS.border}`, borderRadius: 5, color: OPS.text,
  fontSize: 14, padding: '8px 10px', fontFamily: OPS.mono, outline: 'none', width: '100%', boxSizing: 'border-box',
}

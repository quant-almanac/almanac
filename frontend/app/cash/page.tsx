'use client'

import { useState } from 'react'
import useSWR, { useSWRConfig } from 'swr'
import { fetcher, apiFetch, apiErrorMessage } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, PanelTitle, Stat, Chip, Modal, Loading, Grid } from '@/components/today/ops/PageKit'

interface Balances {
  balance_jpy_rakuten: number
  balance_jpy_sbi: number
  balance_usd: number
  fx_rate_usdjpy: number
  total_cash_jpy: number
  last_updated: string
}
interface Tx {
  id: string
  timestamp: string
  type: string
  amount: number
  currency: string
  broker?: string
  description?: string
  source?: string
  provisional_date?: boolean
}

const BROKER_LABEL: Record<string, string> = { rakuten: '楽天証券', sbi: 'SBI証券' }

function yen(v: number): string {
  return `¥${Math.round(v).toLocaleString()}`
}

export default function CashPage() {
  const { mutate } = useSWRConfig()
  const { data: bal, isLoading } = useSWR<Balances>('/api/cash/balances', fetcher, { refreshInterval: 120000 })
  const { data: txData } = useSWR<{ transactions: Tx[] }>('/api/cash/transactions?limit=50', fetcher, { refreshInterval: 120000 })
  const [form, setForm] = useState<null | 'deposit' | 'withdraw'>(null)

  const txs = txData?.transactions ?? []

  return (
    <OpsPage
      en="CASH"
      title="入出金・現金残高"
      subtitle="口座別の現金残高と入出金履歴。定期積立はスケジュール由来の暫定表示。"
      right={
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={() => setForm('deposit')} style={btnPrimary}>＋ 入金</button>
          <button onClick={() => setForm('withdraw')} style={btnGhost}>－ 出金</button>
        </div>
      }
    >
      {isLoading && <Loading />}
      {bal && (
        <>
          <Grid cols={4} gap={12}>
            <Stat label="現金合計（円換算）" value={yen(bal.total_cash_jpy)} color={OPS.gold} sub={`USD/JPY ${bal.fx_rate_usdjpy.toFixed(1)}`} />
            <Stat label="楽天証券（円）" value={yen(bal.balance_jpy_rakuten)} />
            <Stat label="SBI証券（円）" value={yen(bal.balance_jpy_sbi)} />
            <Stat label="USD 残高" value={`$${Math.round(bal.balance_usd).toLocaleString()}`} sub={yen(bal.balance_usd * bal.fx_rate_usdjpy)} />
          </Grid>
          <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.dim, margin: '10px 2px 0' }}>
            最終更新 {bal.last_updated}
          </div>

          <div style={{ marginTop: 26 }}>
            <Panel pad="16px 18px">
              <PanelTitle right={`直近 ${txs.length} 件`}>入出金履歴</PanelTitle>
              <div>
                {txs.map(t => {
                  const isIn = t.type === 'deposit'
                  return (
                    <div
                      key={t.id}
                      className="ops-row"
                      style={{
                        display: 'flex',
                        alignItems: 'baseline',
                        gap: 12,
                        padding: '8px 6px',
                        borderTop: `1px solid ${OPS.hairline}`,
                        fontSize: 12.5,
                      }}
                    >
                      <span style={{ fontFamily: OPS.mono, color: OPS.dim, minWidth: 82 }}>
                        {t.timestamp}
                        {t.provisional_date && <span style={{ color: OPS.amber }}> *</span>}
                      </span>
                      <Chip color={isIn ? OPS.green : OPS.vermilion} bg={isIn ? OPS.greenBg : OPS.vermilionBg} mono>
                        {isIn ? '入金' : '出金'}
                      </Chip>
                      {t.broker && <span style={{ color: OPS.sub, minWidth: 70 }}>{BROKER_LABEL[t.broker] ?? t.broker}</span>}
                      <span
                        style={{
                          fontFamily: OPS.mono,
                          color: isIn ? OPS.green : OPS.redSoft,
                          fontWeight: 500,
                          minWidth: 110,
                        }}
                      >
                        {isIn ? '+' : '−'}
                        {t.currency === 'USD' ? '$' : '¥'}
                        {Math.round(t.amount).toLocaleString()}
                      </span>
                      <span style={{ color: OPS.dim, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {t.description}
                      </span>
                    </div>
                  )
                })}
                {txs.length === 0 && <p style={{ fontSize: 12, color: OPS.dim }}>履歴なし</p>}
              </div>
              <div style={{ fontSize: 10.5, color: OPS.dim, marginTop: 10 }}>* = スケジュール由来の暫定日付</div>
            </Panel>
          </div>
        </>
      )}

      <CashForm type={form} onClose={() => setForm(null)} onDone={() => { mutate('/api/cash/balances'); mutate('/api/cash/transactions?limit=50') }} />
    </OpsPage>
  )
}

function CashForm({ type, onClose, onDone }: { type: 'deposit' | 'withdraw' | null; onClose: () => void; onDone: () => void }) {
  const [amount, setAmount] = useState('')
  const [broker, setBroker] = useState('rakuten')
  const [currency, setCurrency] = useState('JPY')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null)

  async function submit() {
    if (!type) return
    setBusy(true)
    setResult(null)
    try {
      const res = await apiFetch(`/api/cash/${type}`, {
        method: 'POST',
        body: JSON.stringify({ amount: parseFloat(amount), broker, currency, description: note }),
      })
      const json = await res.json().catch(() => ({}))
      if (!res.ok || json?.ok === false) {
        setResult({ ok: false, msg: apiErrorMessage(json, `失敗: HTTP ${res.status}`) })
        return
      }
      setResult({ ok: true, msg: '記録しました' })
      onDone()
      setTimeout(onClose, 1000)
    } catch (e) {
      setResult({ ok: false, msg: String(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={!!type} onClose={onClose} width={440}>
      <h2 style={{ fontSize: 18, fontWeight: 700, color: OPS.text, margin: '0 0 16px' }}>
        {type === 'deposit' ? '入金を記録' : '出金を記録'}
      </h2>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <label style={fieldWrap}>
          <span style={fieldLabel}>金額</span>
          <input value={amount} onChange={e => setAmount(e.target.value)} placeholder="100000" style={inputSt} />
        </label>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <label style={fieldWrap}>
            <span style={fieldLabel}>証券会社</span>
            <select value={broker} onChange={e => setBroker(e.target.value)} style={inputSt}>
              <option value="rakuten">楽天証券</option>
              <option value="sbi">SBI証券</option>
            </select>
          </label>
          <label style={fieldWrap}>
            <span style={fieldLabel}>通貨</span>
            <select value={currency} onChange={e => setCurrency(e.target.value)} style={inputSt}>
              <option value="JPY">JPY</option>
              <option value="USD">USD</option>
            </select>
          </label>
        </div>
        <label style={fieldWrap}>
          <span style={fieldLabel}>備考</span>
          <input value={note} onChange={e => setNote(e.target.value)} placeholder="任意" style={inputSt} />
        </label>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 6 }}>
          <button onClick={submit} disabled={busy || !amount} style={{ ...btnPrimary, opacity: busy || !amount ? 0.5 : 1 }}>
            {busy ? '保存中…' : '記録する'}
          </button>
          {result && <span style={{ fontSize: 12.5, color: result.ok ? OPS.green : OPS.redSoft }}>{result.msg}</span>}
        </div>
      </div>
    </Modal>
  )
}

const btnPrimary: React.CSSProperties = {
  background: OPS.goldBg, border: `1px solid ${OPS.gold}66`, borderRadius: 6, color: OPS.gold,
  fontSize: 13, fontWeight: 600, padding: '7px 16px', cursor: 'pointer', fontFamily: OPS.sans,
}
const btnGhost: React.CSSProperties = {
  background: 'none', border: `1px solid ${OPS.hairline}`, borderRadius: 6, color: OPS.sub,
  fontSize: 13, padding: '7px 16px', cursor: 'pointer', fontFamily: OPS.sans,
}
const fieldWrap: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 5 }
const fieldLabel: React.CSSProperties = { fontSize: 11, color: OPS.dim, fontFamily: OPS.mono }
const inputSt: React.CSSProperties = {
  background: OPS.panelAlt, border: `1px solid ${OPS.border}`, borderRadius: 6, color: OPS.text,
  fontSize: 13, padding: '8px 10px', fontFamily: OPS.mono, outline: 'none', width: '100%', boxSizing: 'border-box',
}

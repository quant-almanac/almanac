'use client'

import useSWR from 'swr'
import { Suspense, useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { fetcher, apiErrorMessage, apiFetch } from '@/lib/api'
import { OpsPage, Panel, Modal } from '@/components/today/ops/PageKit'
import { OPS } from '@/components/today/ops/tokens'

// Next.js 16: useSearchParams を呼ぶ component は Suspense で包む必要がある（CSR bailout 回避）
export default function ExecutionsPageWrapper() {
  return (
    <Suspense fallback={<div style={{ padding: 40, color: OPS.sub }}>読み込み中…</div>}>
      <ExecutionsPage />
    </Suspense>
  )
}

interface Execution {
  id: string
  ticker: string
  direction: string
  action: string
  status: string
  price?: number
  quantity?: number
  note?: string
  currency?: string
  saved_at: string
  portfolio_updated?: boolean
  portfolio_message?: string
}

const STATUS_CFG: Record<string, { color: string; label: string }> = {
  executed:  { color: OPS.green, label: '約定済み' },
  partial:   { color: OPS.amber, label: '部分約定' },
  ordered:   { color: OPS.blue, label: '注文中' },
  cancelled: { color: OPS.sub, label: 'キャンセル' },
  skip:      { color: OPS.sub, label: 'スキップ' },
}

const DIR_CFG: Record<string, { color: string; label: string }> = {
  buy:  { color: OPS.green, label: 'BUY' },
  sell: { color: OPS.vermilion, label: 'SELL' },
  hold: { color: OPS.gold, label: 'HOLD' },
}

function ExecutionsPage() {
  const { data, mutate } = useSWR<{ executions: Execution[] }>('/api/actions/executions', fetcher, { refreshInterval: 30000 })
  const [filter, setFilter] = useState<string>('all')
  const [deletingId, setDeletingId] = useState<string | null>(null)

  // Fix 1C (2026-04-25): 編集モーダル状態
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editPrice, setEditPrice] = useState('')
  const [editQty, setEditQty] = useState('')
  const [editNote, setEditNote] = useState('')
  const [editStatus, setEditStatus] = useState<string>('executed')
  const [editCurrency, setEditCurrency] = useState<string>('USD')
  const [editSaving, setEditSaving] = useState(false)

  const executions = [...(data?.executions ?? [])].reverse()
  const filtered = filter === 'all' ? executions : executions.filter(e => e.status === filter)

  // ?focus=<id> deep-link で該当行の編集を自動オープン（page.tsx の "内容を修正" リンクから飛んできた時用）
  const sp = useSearchParams()
  const focusId = sp?.get('focus') ?? null
  useEffect(() => {
    if (!focusId || editingId) return
    const target = (data?.executions ?? []).find(e => e.id === focusId)
    if (target) openEdit(target)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusId, data])

  function openEdit(e: Execution) {
    setEditingId(e.id)
    setEditPrice(e.price != null ? String(e.price) : '')
    setEditQty(e.quantity != null ? String(e.quantity) : '')
    setEditNote(e.note ?? '')
    setEditStatus(e.status || 'executed')
    setEditCurrency(e.currency || (e.ticker?.endsWith('.T') ? 'JPY' : 'USD'))
  }

  function closeEdit() {
    setEditingId(null)
    setEditSaving(false)
  }

  async function submitEdit(id: string) {
    setEditSaving(true)
    try {
      const body: Record<string, unknown> = { status: editStatus, currency: editCurrency }
      if (editPrice.trim() !== '') body.price = parseFloat(editPrice)
      if (editQty.trim() !== '')   body.quantity = parseFloat(editQty)
      if (editNote.trim() !== '')  body.note = editNote
      const res = await apiFetch(`/api/actions/executions/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const json = await res.json().catch(() => ({}))
      if (!res.ok || json?.ok === false) {
        alert(apiErrorMessage(json, `保存失敗: HTTP ${res.status}`))
      } else {
        mutate()
        closeEdit()
      }
    } catch (err) {
      alert('保存失敗: ' + String(err))
    } finally {
      setEditSaving(false)
    }
  }

  async function handleDelete(id: string) {
    if (!confirm('この記録を削除しますか？')) return
    setDeletingId(id)
    try {
      const res = await apiFetch(`/api/actions/executions/${id}`, { method: 'DELETE' })
      const json = await res.json().catch(() => ({}))
      if (!res.ok || json?.ok === false) {
        alert(apiErrorMessage(json, `削除失敗: HTTP ${res.status}`))
        return
      }
      mutate()
    } catch (err) {
      alert(`削除失敗: ${String(err)}`)
    } finally {
      setDeletingId(null)
    }
  }

  const tabStyle = (val: string) => ({
    padding: '5px 14px', borderRadius: 20, fontSize: 14, cursor: 'pointer', border: 'none',
    background: filter === val ? OPS.goldBg : 'transparent',
    color: filter === val ? OPS.gold : OPS.sub,
    fontWeight: filter === val ? 600 : 400,
    outline: filter === val ? `1px solid ${OPS.gold}66` : '1px solid transparent',
  } as React.CSSProperties)

  return (
    <OpsPage en="EXECUTIONS" title="執行台帳" subtitle="優先アクションの実行・注文記録（最新順）。編集・削除は記録だけを更新する。" widthMode="wide">

      {/* フィルタタブ */}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 20 }}>
        {['all', 'executed', 'ordered', 'partial', 'cancelled', 'skip'].map(s => (
          <button key={s} onClick={() => setFilter(s)} style={tabStyle(s)}>
            {s === 'all' ? `すべて (${executions.length})` : `${STATUS_CFG[s]?.label ?? s} (${executions.filter(e => e.status === s).length})`}
          </button>
        ))}
      </div>

      {/* テーブル */}
      {filtered.length === 0 ? (
        <p style={{ color: OPS.sub, fontSize: 13, textAlign: 'center', padding: 40 }}>記録がありません</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {filtered.map(e => {
            const st = STATUS_CFG[e.status] ?? { color: OPS.sub, label: e.status }
            const dir = DIR_CFG[e.direction] ?? { color: OPS.gold, label: e.direction }
            const date = e.saved_at ? new Date(e.saved_at).toLocaleString('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'
            return (
              <Panel key={e.id} pad="12px 16px" style={{ display: 'flex', alignItems: 'flex-start', gap: 12, flexWrap: 'wrap' }}>
                {/* ステータス + 方向 */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, flexShrink: 0, minWidth: 70 }}>
                  <span style={{
                    fontSize: 14, padding: '2px 8px', borderRadius: 4, textAlign: 'center',
                    background: `${st.color}18`, color: st.color, border: `1px solid ${st.color}30`, fontWeight: 700,
                  }}>{st.label}</span>
                  <span style={{
                    fontSize: 14, padding: '2px 8px', borderRadius: 4, textAlign: 'center',
                    background: `${dir.color}18`, color: dir.color, border: `1px solid ${dir.color}30`, fontWeight: 700,
                  }}>{dir.label}</span>
                </div>

                {/* メイン情報 */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, flexWrap: 'wrap' }}>
                    <span style={{ color: OPS.text, fontSize: 14, fontWeight: 700, fontFamily: OPS.mono }}>
                      {e.ticker ?? '—'}
                    </span>
                    {e.price != null && (
                      <span style={{ color: OPS.amber, fontSize: 14, fontWeight: 600 }}>@ {e.price.toLocaleString()}</span>
                    )}
                    {e.quantity != null && (
                      <span style={{ color: OPS.sub, fontSize: 14 }}>{e.quantity} 株</span>
                    )}
                    <span style={{ color: OPS.sub, fontSize: 14, marginLeft: 'auto' }}>{date}</span>
                  </div>
                  <p style={{ color: OPS.sub, fontSize: 14, lineHeight: 1.5, marginBottom: e.note || e.portfolio_message ? 6 : 0 }}>
                    {e.action?.slice(0, 80)}{(e.action?.length ?? 0) > 80 ? '…' : ''}
                  </p>
                  {e.note && (
                    <p style={{ color: OPS.sub, fontSize: 14 }}>📝 {e.note}</p>
                  )}
                  {e.portfolio_message && (
                    <p style={{ color: e.portfolio_updated ? OPS.green : OPS.sub, fontSize: 14 }}>
                      {e.portfolio_updated ? '✓ ' : ''}{e.portfolio_message}
                    </p>
                  )}
                </div>

                {/* 編集 + 削除ボタン */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, flexShrink: 0 }}>
                  <button
                    onClick={() => openEdit(e)}
                    title="編集"
                    style={{
                      fontSize: 13, padding: '4px 10px', borderRadius: 6, cursor: 'pointer',
                      background: OPS.goldBg, border: `1px solid ${OPS.gold}4d`, color: OPS.gold,
                    }}
                  >
                    ✏️ 編集
                  </button>
                  <button
                    onClick={() => handleDelete(e.id)}
                    disabled={deletingId === e.id}
                    title="削除"
                    style={{
                      fontSize: 13, padding: '4px 10px', borderRadius: 6, cursor: 'pointer',
                      background: 'transparent', border: `1px solid ${OPS.border}`, color: OPS.sub,
                    }}
                  >
                    {deletingId === e.id ? '…' : '✕ 削除'}
                  </button>
                </div>
              </Panel>
            )
          })}
        </div>
      )}

      {/* Fix 1C (2026-04-25): 編集モーダル */}
      {editingId && (() => {
        const target = executions.find(e => e.id === editingId)
        if (!target) return null
        const inputSt: React.CSSProperties = {
          background: OPS.inset, border: `1px solid ${OPS.border}`, borderRadius: 6,
          color: OPS.text, fontSize: 14, padding: '6px 10px', outline: 'none', width: '100%', boxSizing: 'border-box',
        }
        return (
          <Modal open onClose={closeEdit} width={520} fitViewport>
            <div style={{ overflowY: 'auto', paddingRight: 4 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
                <span style={{ fontSize: 20 }}>✏️</span>
                <h3 style={{ margin: 0, color: OPS.text, fontSize: 17, fontWeight: 700 }}>実行記録を編集</h3>
                <span style={{ marginLeft: 'auto', color: OPS.sub, fontSize: 13, fontFamily: OPS.mono }}>{target.ticker} ({target.direction.toUpperCase()})</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
                <div>
                  <label style={{ color: OPS.sub, fontSize: 13, display: 'block', marginBottom: 4 }}>約定価格</label>
                  <input type="number" step="any" placeholder="例: 182.5" value={editPrice} onChange={ev => setEditPrice(ev.target.value)} style={inputSt} />
                </div>
                <div>
                  <label style={{ color: OPS.sub, fontSize: 13, display: 'block', marginBottom: 4 }}>数量</label>
                  <input type="number" step="any" placeholder="例: 10" value={editQty} onChange={ev => setEditQty(ev.target.value)} style={inputSt} />
                </div>
                <div>
                  <label style={{ color: OPS.sub, fontSize: 13, display: 'block', marginBottom: 4 }}>ステータス</label>
                  <select value={editStatus} onChange={ev => setEditStatus(ev.target.value)} style={inputSt}>
                    <option value="executed">約定済み</option>
                    <option value="ordered">注文中</option>
                    <option value="partial">部分約定</option>
                    <option value="cancelled">キャンセル</option>
                    <option value="skip">スキップ</option>
                  </select>
                </div>
                <div>
                  <label style={{ color: OPS.sub, fontSize: 13, display: 'block', marginBottom: 4 }}>通貨</label>
                  <select value={editCurrency} onChange={ev => setEditCurrency(ev.target.value)} style={inputSt}>
                    <option value="USD">USD</option>
                    <option value="JPY">JPY</option>
                  </select>
                </div>
              </div>
              <div style={{ marginBottom: 14 }}>
                <label style={{ color: OPS.sub, fontSize: 13, display: 'block', marginBottom: 4 }}>メモ</label>
                <input type="text" placeholder="例: 成行約定" value={editNote} onChange={ev => setEditNote(ev.target.value)} style={inputSt} />
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                <button onClick={closeEdit} style={{ fontSize: 14, padding: '6px 14px', borderRadius: 6, cursor: 'pointer', background: OPS.panelAlt, border: `1px solid ${OPS.border}`, color: OPS.sub }}>キャンセル</button>
                <button onClick={() => submitEdit(editingId)} disabled={editSaving} style={{
                  fontSize: 14, padding: '6px 18px', borderRadius: 6, cursor: 'pointer', fontWeight: 700,
                  background: OPS.goldBg, border: `1px solid ${OPS.gold}80`, color: OPS.gold,
                }}>
                  {editSaving ? '保存中…' : '💾 保存'}
                </button>
              </div>
            </div>
          </Modal>
        )
      })()}
    </OpsPage>
  )
}

'use client'

import { useState, useCallback } from 'react'
import useSWR from 'swr'
import { fetcher, apiFetch } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'

interface Holding {
  ticker?: string
  entry_price: number
  shares: number
  entry_date?: string
  account?: string
  currency?: string
  name?: string
  investment_type?: string
  unit?: string
  current_nav?: number
  note?: string
  partial_taken?: boolean
}

type Holdings = Record<string, Holding>

const ACCOUNTS = ['特定', '一般', 'NISA成長投資枠', 'NISAつみたて投資枠']
const INV_TYPES = ['long', 'medium', 'swing']
const CURRENCIES = ['USD', 'JPY']

const inputSt: React.CSSProperties = {
  background: OPS.inset, border: `1px solid ${OPS.border}`, borderRadius: 6,
  color: OPS.text, fontSize: 14, padding: '6px 10px', outline: 'none',
  width: '100%', boxSizing: 'border-box',
}
const selectSt: React.CSSProperties = { ...inputSt, appearance: 'auto' as const }
const labelSt: React.CSSProperties = { color: OPS.sub, fontSize: 14, fontWeight: 600, display: 'block', marginBottom: 3 }

// ── 編集フォーム ──────────────────────────────────────
function EditForm({ hKey, holding, onSave, onCancel }: {
  hKey: string; holding: Holding; onSave: () => void; onCancel: () => void
}) {
  const [form, setForm] = useState({ ...holding })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const set = (field: string, value: unknown) => setForm(f => ({ ...f, [field]: value }))

  async function handleSave() {
    setSaving(true)
    setError('')
    const res = await apiFetch(`/api/holdings/${encodeURIComponent(hKey)}`, {
      method: 'PUT',
      body: JSON.stringify(form),
    })
    const data = await res.json()
    setSaving(false)
    if (data.ok) { onSave() } else { setError(data.error || '保存失敗') }
  }

  return (
    <div style={{ overflow: 'hidden' }}>
      <div style={{
        padding: '14px 16px', marginTop: 6, borderRadius: 10,
        background: OPS.goldBg, border: `1px solid ${OPS.gold}33`,
      }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 10, marginBottom: 10 }}>
          <div>
            <label style={labelSt}>銘柄名</label>
            <input style={inputSt} value={form.name ?? ''} onChange={e => set('name', e.target.value)} />
          </div>
          <div>
            <label style={labelSt}>ティッカー</label>
            <input style={inputSt} value={form.ticker ?? ''} onChange={e => set('ticker', e.target.value)} />
          </div>
          <div>
            <label style={labelSt}>株数</label>
            <input style={inputSt} type="number" value={form.shares} onChange={e => set('shares', parseFloat(e.target.value) || 0)} />
          </div>
          <div>
            <label style={labelSt}>取得単価</label>
            <input style={inputSt} type="number" step="0.01" value={form.entry_price} onChange={e => set('entry_price', parseFloat(e.target.value) || 0)} />
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 10, marginBottom: 10 }}>
          <div>
            <label style={labelSt}>口座</label>
            <select style={selectSt} value={form.account ?? '特定'} onChange={e => set('account', e.target.value)}>
              {ACCOUNTS.map(a => <option key={a} value={a}>{a}</option>)}
            </select>
          </div>
          <div>
            <label style={labelSt}>通貨</label>
            <select style={selectSt} value={form.currency ?? 'USD'} onChange={e => set('currency', e.target.value)}>
              {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label style={labelSt}>投資タイプ</label>
            <select style={selectSt} value={form.investment_type ?? 'medium'} onChange={e => set('investment_type', e.target.value)}>
              {INV_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div>
            <label style={labelSt}>取得日</label>
            <input style={inputSt} type="date" value={form.entry_date ?? ''} onChange={e => set('entry_date', e.target.value)} />
          </div>
        </div>
        {form.unit && (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 10, marginBottom: 10 }}>
            <div>
              <label style={labelSt}>基準価額</label>
              <input style={inputSt} type="number" value={form.current_nav ?? ''} onChange={e => set('current_nav', parseFloat(e.target.value) || 0)} />
            </div>
            <div>
              <label style={labelSt}>単位</label>
              <input style={inputSt} value={form.unit ?? ''} onChange={e => set('unit', e.target.value)} />
            </div>
          </div>
        )}
        {error && <p style={{ color: OPS.vermilion, fontSize: 14, margin: '4px 0' }}>{error}</p>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button onClick={onCancel} style={{
            fontSize: 14, padding: '5px 14px', borderRadius: 6, cursor: 'pointer',
            background: 'transparent', border: `1px solid ${OPS.border}`, color: OPS.sub,
          }}>キャンセル</button>
          <button onClick={handleSave} disabled={saving} style={{
            fontSize: 14, padding: '5px 14px', borderRadius: 6, cursor: 'pointer', fontWeight: 700,
            background: OPS.goldBg, border: `1px solid ${OPS.gold}66`, color: OPS.gold,
          }}>{saving ? '保存中…' : '保存'}</button>
        </div>
      </div>
    </div>
  )
}

// ── 新規追加フォーム ──────────────────────────────────
function AddForm({ onSave, onCancel }: { onSave: () => void; onCancel: () => void }) {
  const [form, setForm] = useState({
    key: '', ticker: '', name: '', shares: 0, entry_price: 0,
    account: '特定', currency: 'USD', investment_type: 'medium' as string, entry_date: '',
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const set = (field: string, value: unknown) => setForm(f => ({ ...f, [field]: value }))

  async function handleSave() {
    if (!form.key.trim()) { setError('key（識別子）は必須です'); return }
    setSaving(true); setError('')
    const res = await apiFetch(`/api/holdings`, {
      method: 'POST',
      body: JSON.stringify(form),
    })
    const data = await res.json()
    setSaving(false)
    if (data.ok) { onSave() } else { setError(data.error || '追加失敗') }
  }

  return (
    <div>
      <div style={{
        padding: '16px 18px', borderRadius: 12, marginBottom: 16,
        background: OPS.greenBg, border: `1px solid ${OPS.green}33`,
      }}>
        <p style={{ color: OPS.green, fontSize: 14, fontWeight: 700, marginBottom: 12 }}>+ 新規銘柄を追加</p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 10, marginBottom: 10 }}>
          <div>
            <label style={labelSt}>Key（識別子）</label>
            <input style={inputSt} placeholder="例: AAPL" value={form.key} onChange={e => set('key', e.target.value)} />
          </div>
          <div>
            <label style={labelSt}>ティッカー</label>
            <input style={inputSt} placeholder="例: AAPL" value={form.ticker} onChange={e => set('ticker', e.target.value)} />
          </div>
          <div>
            <label style={labelSt}>銘柄名</label>
            <input style={inputSt} placeholder="例: Apple" value={form.name} onChange={e => set('name', e.target.value)} />
          </div>
          <div>
            <label style={labelSt}>通貨</label>
            <select style={selectSt} value={form.currency} onChange={e => set('currency', e.target.value)}>
              {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 10, marginBottom: 10 }}>
          <div>
            <label style={labelSt}>株数</label>
            <input style={inputSt} type="number" value={form.shares || ''} onChange={e => set('shares', parseFloat(e.target.value) || 0)} />
          </div>
          <div>
            <label style={labelSt}>取得単価</label>
            <input style={inputSt} type="number" step="0.01" value={form.entry_price || ''} onChange={e => set('entry_price', parseFloat(e.target.value) || 0)} />
          </div>
          <div>
            <label style={labelSt}>口座</label>
            <select style={selectSt} value={form.account} onChange={e => set('account', e.target.value)}>
              {ACCOUNTS.map(a => <option key={a} value={a}>{a}</option>)}
            </select>
          </div>
          <div>
            <label style={labelSt}>投資タイプ</label>
            <select style={selectSt} value={form.investment_type} onChange={e => set('investment_type', e.target.value)}>
              {INV_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
        </div>
        {error && <p style={{ color: OPS.vermilion, fontSize: 14, margin: '4px 0' }}>{error}</p>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button onClick={onCancel} style={{
            fontSize: 14, padding: '5px 14px', borderRadius: 6, cursor: 'pointer',
            background: 'transparent', border: `1px solid ${OPS.border}`, color: OPS.sub,
          }}>キャンセル</button>
          <button onClick={handleSave} disabled={saving} style={{
            fontSize: 14, padding: '5px 14px', borderRadius: 6, cursor: 'pointer', fontWeight: 700,
            background: OPS.greenBg, border: `1px solid ${OPS.green}66`, color: OPS.green,
          }}>{saving ? '追加中…' : '追加'}</button>
        </div>
      </div>
    </div>
  )
}

// ── メインコンポーネント ──────────────────────────────
export default function HoldingsEditor() {
  const { data: holdings, mutate } = useSWR<Holdings>('/api/holdings', fetcher)
  const [editKey, setEditKey] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [deleting, setDeleting] = useState<string | null>(null)

  const refresh = useCallback(() => { mutate(); setEditKey(null); setShowAdd(false) }, [mutate])

  async function handleDelete(key: string) {
    const res = await apiFetch(`/api/holdings/${encodeURIComponent(key)}`, { method: 'DELETE' })
    const data = await res.json()
    if (data.ok) { refresh() }
    setDeleting(null)
  }

  if (!holdings) return <p style={{ color: OPS.sub, fontSize: 14, padding: 20 }}>読み込み中...</p>

  const entries = Object.entries(holdings)
  const byType: Record<string, [string, Holding][]> = { long: [], medium: [], swing: [] }
  for (const [k, v] of entries) {
    const t = v.investment_type ?? 'medium'
    if (byType[t]) byType[t].push([k, v])
    else byType.medium.push([k, v])
  }

  const tierLabel: Record<string, { label: string; color: string }> = {
    long:   { label: 'Long（コア）',   color: OPS.gold },
    medium: { label: 'Medium（戦術）', color: OPS.amber },
    swing:  { label: 'Swing（投機）',  color: OPS.vermilion },
  }

  return (
    <div>
      {/* ヘッダー */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ color: OPS.text, fontSize: 16, fontWeight: 700, margin: 0 }}>
          📋 ポートフォリオ編集 <span style={{ color: OPS.sub, fontSize: 14, fontWeight: 400 }}>({entries.length}銘柄)</span>
        </h2>
        <button
          onClick={() => { setShowAdd(!showAdd); setEditKey(null) }}
          style={{
            fontSize: 14, padding: '6px 16px', borderRadius: 8, cursor: 'pointer', fontWeight: 700,
            background: showAdd ? OPS.vermilionBg : OPS.greenBg,
            border: `1px solid ${showAdd ? `${OPS.vermilion}4d` : `${OPS.green}4d`}`,
            color: showAdd ? OPS.vermilion : OPS.green,
          }}
        >{showAdd ? '✕ 閉じる' : '+ 銘柄追加'}</button>
      </div>

      {/* 新規追加フォーム */}
      {showAdd && <AddForm onSave={refresh} onCancel={() => setShowAdd(false)} />}

      {/* ティア別テーブル */}
      {(['long', 'medium', 'swing'] as const).map(tier => {
        const items = byType[tier]
        if (items.length === 0) return null
        const meta = tierLabel[tier]

        return (
          <div key={tier} style={{ marginBottom: 24 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
              <span style={{
                width: 8, height: 8, borderRadius: '50%', background: meta.color,
              }} />
              <span style={{ color: meta.color, fontSize: 14, fontWeight: 700 }}>{meta.label}</span>
              <span style={{ color: OPS.sub, fontSize: 14 }}>({items.length})</span>
            </div>

            <div style={{
              borderRadius: 10, overflow: 'hidden',
              border: `1px solid ${OPS.border}`,
            }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
                <thead>
                  <tr style={{ background: OPS.panelAlt }}>
                    {['Key', '銘柄名', 'ティッカー', '株数', '取得単価', '通貨', '口座', ''].map(h => (
                      <th key={h} style={{
                        padding: '8px 10px', textAlign: 'left', color: OPS.sub,
                        fontSize: 13, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em',
                        borderBottom: `1px solid ${OPS.border}`,
                      }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {items.map(([key, h]) => (
                    <tr key={key}>
                      <td colSpan={8} style={{ padding: 0 }}>
                        {/* 行データ */}
                        <div style={{
                          display: 'grid', gridTemplateColumns: '80px 1fr 90px 80px 90px 50px 100px 80px',
                          alignItems: 'center', padding: '8px 10px',
                          borderBottom: `1px solid ${OPS.hairline}`,
                          background: editKey === key ? OPS.goldBg : 'transparent',
                        }}>
                          <span style={{ color: OPS.sub, fontFamily: OPS.mono }}>{key}</span>
                          <span style={{ color: OPS.text, fontWeight: 500 }}>{h.name}</span>
                          <span style={{ color: OPS.gold, fontFamily: OPS.mono, fontWeight: 600 }}>{h.ticker}</span>
                          <span style={{ color: OPS.text, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                            {h.shares.toLocaleString()}
                          </span>
                          <span style={{ color: OPS.sub, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                            {h.currency === 'JPY' ? `¥${h.entry_price.toLocaleString()}` : `$${h.entry_price.toLocaleString()}`}
                          </span>
                          <span style={{ color: OPS.sub, textAlign: 'center' }}>{h.currency}</span>
                          <span style={{ color: OPS.sub, fontSize: 14 }}>{h.account}</span>
                          <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
                            <button
                              onClick={() => setEditKey(editKey === key ? null : key)}
                              style={{
                                fontSize: 14, padding: '3px 8px', borderRadius: 4, cursor: 'pointer',
                                background: OPS.goldBg,
                                border: `1px solid ${OPS.gold}4d`, color: OPS.gold,
                              }}
                            >{editKey === key ? '閉' : '✏️'}</button>
                            {deleting === key ? (
                              <>
                                <button onClick={() => handleDelete(key)} style={{
                                  fontSize: 14, padding: '3px 8px', borderRadius: 4, cursor: 'pointer',
                                  background: OPS.vermilionBg, border: `1px solid ${OPS.vermilion}66`, color: OPS.vermilion,
                                }}>確認</button>
                                <button onClick={() => setDeleting(null)} style={{
                                  fontSize: 14, padding: '3px 6px', borderRadius: 4, cursor: 'pointer',
                                  background: 'transparent', border: `1px solid ${OPS.border}`, color: OPS.sub,
                                }}>✕</button>
                              </>
                            ) : (
                              <button onClick={() => setDeleting(key)} style={{
                                fontSize: 14, padding: '3px 8px', borderRadius: 4, cursor: 'pointer',
                                background: OPS.vermilionBg, border: `1px solid ${OPS.vermilion}33`, color: OPS.vermilion,
                              }}>🗑</button>
                            )}
                          </div>
                        </div>
                        {/* 編集フォーム */}
                        {editKey === key && <EditForm hKey={key} holding={h} onSave={refresh} onCancel={() => setEditKey(null)} />}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )
      })}
    </div>
  )
}

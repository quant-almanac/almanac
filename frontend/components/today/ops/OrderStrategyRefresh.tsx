'use client'

import { useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { OPS } from './tokens'
import { Modal } from './PageKit'

type PollResult = { running: boolean; last_result?: { status?: string; message?: string; updated?: number } }

export default function OrderStrategyRefresh() {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const pollCount = useRef(0)

  const poll = async (): Promise<void> => {
    try {
      const status = await apiFetch('/api/ai-analysis/order-strategy/status').then(response => response.json()) as PollResult
      if (!status.running) {
        const result = status.last_result
        setMessage(result?.status === 'ok' ? `完了: ${result.updated ?? 0}件を更新` : result?.message ?? '再計算が完了しました。')
        setBusy(false)
        return
      }
      if (pollCount.current >= 24) {
        setMessage('タイムアウト（2分超）。状態を後で確認してください。')
        setBusy(false)
        return
      }
    } catch {
      // 一時的な status 失敗はポーリングを継続する。
    }
    pollCount.current += 1
    window.setTimeout(() => { void poll() }, 5000)
  }

  const start = async () => {
    setConfirmOpen(false)
    setBusy(true)
    setMessage('注文方法を再計算しています…')
    pollCount.current = 0
    try {
      await apiFetch('/api/ai-analysis/order-strategy/refresh', { method: 'POST' })
      window.setTimeout(() => { void poll() }, 5000)
    } catch (error) {
      setBusy(false)
      setMessage(`再計算を開始できませんでした: ${String(error)}`)
    }
  }

  return <>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      {message && <span style={{ color: OPS.dim, fontSize: 11.5, maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{message}</span>}
      <button type="button" disabled={busy} onClick={() => setConfirmOpen(true)} style={{ background: OPS.amberBg, border: `1px solid ${OPS.amber}66`, borderRadius: 5, color: OPS.amber, cursor: busy ? 'wait' : 'pointer', fontFamily: OPS.mono, fontSize: 11.5, padding: '5px 8px' }}>{busy ? '⚡ 再計算中…' : '⚡ 指値再計算'}</button>
    </div>
    <Modal open={confirmOpen} onClose={() => setConfirmOpen(false)} width={520}>
      <div style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 11, letterSpacing: '0.1em', marginBottom: 9 }}>ORDER STRATEGY REFRESH</div>
      <h3 style={{ color: OPS.text, fontSize: 18, margin: '0 0 9px' }}>指値・注文方法を再計算しますか？</h3>
      <p style={{ color: OPS.sub, fontSize: 13, lineHeight: 1.7, margin: 0 }}>Sonnet が現在価格・VIX・ATRを使って注文方法を再評価します。LLM実行により課金が発生します。発注・記録は行いません。</p>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}><button onClick={() => setConfirmOpen(false)} style={secondary}>キャンセル</button><button onClick={() => void start()} style={primary}>再計算を開始</button></div>
    </Modal>
  </>
}

const secondary: React.CSSProperties = { background: 'transparent', border: `1px solid ${OPS.hairline}`, borderRadius: 5, color: OPS.sub, cursor: 'pointer', padding: '7px 11px' }
const primary: React.CSSProperties = { background: OPS.amberBg, border: `1px solid ${OPS.amber}66`, borderRadius: 5, color: OPS.amber, cursor: 'pointer', padding: '7px 11px' }

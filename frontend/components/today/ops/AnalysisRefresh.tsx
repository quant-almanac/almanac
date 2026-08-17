'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFetch, fetcher } from '@/lib/api'
import { OPS } from './tokens'
import { Modal } from './PageKit'

/**
 * AI 統合分析の再実行ボタン。
 *
 * 既存の API を使う:
 *   POST /api/ai-analysis/refresh   — バックグラウンド起動（多重起動は file lock で弾かれる）
 *   GET  /api/ai-analysis/progress  — {step,total,label,detail,pct}
 *   GET  /api/ai-analysis           — {refresh_running} で完了を判定
 *
 * LLM 課金と Telegram 通知が発生するため、必ず確認モーダルを挟む。
 * 発注・記録は行わない（分析の再生成のみ）。
 */

type Progress = { step?: number; total?: number; label?: string; detail?: string; pct?: number }
type Status = { refresh_running?: boolean }

const POLL_MS = 2500
/** 1〜2分想定の処理に対し、10分で見切る（POLL_MS × 240） */
const MAX_POLLS = 240

export default function AnalysisRefresh({ ageHours, onDone, compact = false }: { ageHours?: number | null; onDone?: () => void; compact?: boolean }) {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<Progress | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  // onDone は呼び出し側で毎レンダー新しい関数になりうる。依存に入れると
  // ポーリング中に effect が張り直され、タイマーが延々リセットされる。
  const onDoneRef = useRef(onDone)
  useEffect(() => { onDoneRef.current = onDone })

  // running が真の間だけポーリングする。cleanup で確実に止まるので、
  // アンマウントや再実行でタイマーが漏れない。
  useEffect(() => {
    if (!running) return
    let cancelled = false
    let timer: number | undefined
    let polls = 0

    const tick = async () => {
      if (cancelled) return
      try {
        const [status, prog] = await Promise.all([
          fetcher('/api/ai-analysis') as Promise<Status>,
          fetcher('/api/ai-analysis/progress') as Promise<Progress>,
        ])
        if (cancelled) return
        setProgress(prog)
        if (!status.refresh_running) {
          setRunning(false)
          setFailed(false)
          setMessage('分析が完了しました。最新の結果を読み込みます。')
          onDoneRef.current?.()
          return
        }
      } catch {
        // 一時的な取得失敗ではポーリングを止めない（分析自体は走り続けている）
      }
      if (cancelled) return
      polls += 1
      if (polls >= MAX_POLLS) {
        setRunning(false)
        setFailed(true)
        setMessage('進捗を追えなくなりました。分析は継続中の可能性があります。')
        return
      }
      timer = window.setTimeout(() => { void tick() }, POLL_MS)
    }

    timer = window.setTimeout(() => { void tick() }, POLL_MS)
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [running])

  const start = async () => {
    setConfirmOpen(false)
    setFailed(false)
    setProgress(null)
    setMessage('分析を開始しています…')
    // POST の応答を待たずに running にする。ボタンが即座に無効化され二重起動を防げる。
    // 初回ポーリングは POLL_MS 後なので、失敗時は下の catch が先に解除する。
    setRunning(true)
    try {
      const response = await apiFetch('/api/ai-analysis/refresh', { method: 'POST' })
      const json = await response.json() as { status?: string; message?: string }
      if (!response.ok) throw new Error(json?.message ?? `HTTP ${response.status}`)
      setMessage(json?.message ?? '分析を実行中です。')
    } catch (error) {
      setRunning(false)
      setFailed(true)
      setMessage(`分析を開始できませんでした: ${String(error)}`)
    }
  }

  const pct = Math.max(0, Math.min(100, progress?.pct ?? 0))
  const stale = (ageHours ?? 0) > 24
  const tone = failed ? OPS.vermilion : running ? OPS.blue : stale ? OPS.amber : OPS.gold

  return <>
    <div style={{ display: 'flex', flexDirection: 'column', gap: 7, minWidth: 0 }}>
      <button
        type="button"
        className="ops-btn"
        disabled={running}
        onClick={() => setConfirmOpen(true)}
        aria-label="AI分析を再実行"
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 8,
          background: running ? OPS.blueBg : stale ? OPS.amberBg : OPS.goldBg,
          border: `1px solid ${tone}77`,
          color: tone,
          fontFamily: OPS.mono,
          fontSize: 12.5,
          fontWeight: 600,
          letterSpacing: '.04em',
          padding: compact ? '4px 10px' : '8px 14px',
          width: compact ? 'auto' : '100%',
        }}
      >
        <span
          aria-hidden
          className={running ? 'ops-spin' : undefined}
          style={{ display: 'inline-block', lineHeight: 1, fontSize: 13 }}
        >
          {running ? '◐' : '⟳'}
        </span>
        {running ? '分析中…' : compact ? 'シグナル更新' : 'AI分析を再実行'}
      </button>

      {(running || message) && (
        <div style={{ minWidth: 0 }}>
          {running && (
            <>
              <div className="ops-well" style={{ height: 5, borderRadius: 5, overflow: 'hidden' }}>
                <div
                  style={{
                    width: `${Math.max(3, pct)}%`,
                    height: '100%',
                    borderRadius: 5,
                    background: `linear-gradient(90deg, ${OPS.blue}, ${OPS.gold})`,
                    boxShadow: `0 0 10px ${OPS.blue}88`,
                    transition: 'width .5s cubic-bezier(.22,.8,.3,1)',
                  }}
                />
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 5, color: OPS.sub, fontFamily: OPS.mono, fontSize: 11 }}>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {progress?.label ?? '準備中'}
                </span>
                <span style={{ marginLeft: 'auto', flexShrink: 0, color: OPS.dim }}>
                  {progress?.step != null && progress?.total ? `${progress.step}/${progress.total}` : `${Math.round(pct)}%`}
                </span>
              </div>
            </>
          )}
          {message && !running && (
            <div
              role="status"
              style={{ color: failed ? OPS.redSoft : OPS.dim, fontSize: 11.5, lineHeight: 1.55 }}
            >
              {message}
            </div>
          )}
        </div>
      )}
    </div>

    <Modal open={confirmOpen} onClose={() => setConfirmOpen(false)} width={540}>
      <div style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 11.5, letterSpacing: '0.12em', marginBottom: 10 }}>
        AI ANALYSIS REFRESH
      </div>
      <h3 style={{ color: OPS.text, fontSize: 19, margin: '0 0 10px' }}>統合分析を再実行しますか？</h3>
      <p style={{ color: OPS.sub, fontSize: 13.5, lineHeight: 1.75, margin: 0 }}>
        Sonnet の並列レーン分析と Opus の合成を最新の価格・シグナルでやり直します。所要 1〜2分・LLM 実行により課金が発生し、完了時に Telegram へ通知します。
        <strong style={{ color: OPS.text }}>発注・記録は行いません。</strong>
      </p>
      {ageHours != null && (
        <div style={{ color: stale ? OPS.amber : OPS.dim, fontFamily: OPS.mono, fontSize: 12, marginTop: 12 }}>
          現在の分析は {Math.round(ageHours)}時間前{stale ? '（24時間超）' : ''}
        </div>
      )}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 9, marginTop: 20 }}>
        <button type="button" className="ops-btn" onClick={() => setConfirmOpen(false)} style={secondary}>キャンセル</button>
        <button type="button" className="ops-btn" onClick={() => void start()} style={primary}>再分析を開始</button>
      </div>
    </Modal>
  </>
}

const secondary: React.CSSProperties = {
  background: OPS.panelAlt, border: `1px solid ${OPS.border}`, color: OPS.sub, padding: '8px 14px', fontSize: 13,
}
const primary: React.CSSProperties = {
  background: OPS.goldBg, border: `1px solid ${OPS.gold}88`, color: OPS.gold, padding: '8px 14px', fontSize: 13, fontWeight: 600,
}

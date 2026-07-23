'use client'

import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { OPS, fmtAge } from './tokens'
import type { DashboardDataHealth } from '@/lib/api'

const SOURCE_LABEL: Record<string, string> = {
  guard: 'ガード',
  regime: '相場判定',
  ai_analysis: '統合分析',
  scenario: 'シナリオ',
  vix: 'VIX',
  technical: 'テクニカル',
  macro: 'マクロ',
  news_sentiment: 'ニュース',
}

function sourceState(source: NonNullable<DashboardDataHealth['sources']>[string]): { label: string; color: string } {
  if (source.exists === false) return { label: 'missing', color: OPS.vermilion }
  if (source.stale) return { label: 'stale', color: OPS.amber }
  return { label: 'ok', color: OPS.green }
}

export default function FreshnessDots({
  health,
  trigger,
  buttonStyle,
}: {
  health?: DashboardDataHealth
  trigger?: ReactNode
  buttonStyle?: CSSProperties
}) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const containerRef = useRef<HTMLSpanElement>(null)
  const dialogId = useId()
  const sources = health?.sources ? Object.entries(health.sources) : []

  const close = () => {
    setOpen(false)
    triggerRef.current?.focus()
  }
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
    }
    const onPointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('pointerdown', onPointerDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('pointerdown', onPointerDown)
    }
  }, [open])

  return (
    <span ref={containerRef} style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={dialogId}
        aria-label="データ鮮度の詳細"
        onClick={() => setOpen(value => !value)}
        style={{ background: 'none', border: 'none', color: OPS.sub, cursor: 'pointer', padding: 0, ...buttonStyle }}
      >
        {trigger ?? (
          sources.length > 0 ? (
            <span aria-hidden style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
              {sources.map(([key, source]) => {
                const state = sourceState(source)
                return <span key={key} title={`${SOURCE_LABEL[key] ?? key}: ${state.label}`} style={{ color: state.color, fontSize: 12, lineHeight: 1 }}>●</span>
              })}
            </span>
          ) : '—'
        )}
      </button>
      {open && (
        <FreshnessPanel
          health={health}
          id={dialogId}
          style={{ position: 'absolute', top: 'calc(100% + 8px)', right: 0 }}
        />
      )}
    </span>
  )
}

/**
 * 鮮度ポップオーバー本体。overflow コンテナに閉じ込めたくない呼び出し元
 * （TraceStrip 等）は、これをスクロール領域の外に自前配置する。
 */
export function FreshnessPanel({
  health,
  id,
  style,
}: {
  health?: DashboardDataHealth
  id?: string
  style?: CSSProperties
}) {
  const sources = health?.sources ? Object.entries(health.sources) : []
  return (
    <div
      id={id}
      role="dialog"
      aria-label="データ鮮度の詳細"
      style={{ zIndex: 70, width: 294, maxWidth: 'calc(100vw - 24px)', background: OPS.panel, border: `1px solid ${OPS.border}`, borderRadius: 8, boxShadow: '0 12px 28px rgba(0,0,0,0.45)', padding: '11px 12px', ...style }}
    >
      <div style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 10.5, letterSpacing: '0.09em', marginBottom: 7 }}>DATA FRESHNESS</div>
      {sources.length === 0 ? <p style={{ color: OPS.dim, fontSize: 12, margin: 0 }}>鮮度データはありません。</p> : sources.map(([key, source], index) => {
        const state = sourceState(source)
        return (
          <div key={key} style={{ display: 'grid', gridTemplateColumns: '8px minmax(0,1fr) auto', gap: 8, alignItems: 'baseline', borderTop: index > 0 ? `1px solid ${OPS.hairline}` : 'none', padding: '7px 0', fontSize: 11.5 }}>
            <span style={{ color: state.color }}>●</span>
            <span style={{ color: OPS.sub }}>{SOURCE_LABEL[key] ?? key}<span style={{ color: OPS.dim }}> · {fmtAge(source.age_hours)}</span></span>
            <span style={{ color: state.color, fontFamily: OPS.mono, fontSize: 10.5 }}>{state.label} {source.stale_after_hours != null ? `${source.stale_after_hours}h` : '—'}</span>
          </div>
        )
      })}
    </div>
  )
}

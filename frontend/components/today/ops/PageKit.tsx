'use client'

import { useEffect, useId, useRef, type ReactNode } from 'react'
import { OPS } from './tokens'
import { ContentShell, SHELL_CSS, type ContentWidthMode } from './Shell'

/**
 * ops PageKit — 全スタンドアロンタブ共通の黒曜石ページ基盤。
 * ページ shell / パネル / スタット / チップ / バー / アニメ付きモーダル。
 */

export const PAGE_CSS = `
@keyframes opsFadeUp { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
@keyframes opsModalIn { from { opacity: 0; transform: translateY(10px) scale(.98); } to { opacity: 1; transform: none; } }
@keyframes opsBackdropIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes opsBarGrow { from { transform: scaleX(0); } to { transform: scaleX(1); } }
.ops-sec { animation: opsFadeUp .5s ease both; }
.ops-card { transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease; }
.ops-card:hover { transform: translateY(-2px); border-color: rgba(201,167,93,0.5) !important; box-shadow: 0 6px 18px rgba(0,0,0,0.35); }
.ops-clickable { cursor: pointer; }
.ops-row { transition: background .15s ease; }
.ops-row:hover { background: rgba(201,167,93,0.06); }
.ops-bar-fill { transform-origin: left; animation: opsBarGrow .7s cubic-bezier(.4,0,.2,1) both; }
@media (prefers-reduced-motion: reduce) {
  .ops-sec, .ops-bar-fill { animation: none; }
  .ops-card, .ops-row { transition: none; }
  .ops-card:hover { transform: none; }
}
/* 固定列数Gridは狭幅で折り返せない語(例: "新規エントリー禁止")が縦積みに
   潰れるため、560px未満は1カラムへ落とす。auto-fill(minmax)側は元々
   可変列なので対象外。 */
@media (max-width: 560px) {
  .ops-grid-cols { grid-template-columns: 1fr !important; }
}
`

export function OpsPage({
  title,
  en,
  subtitle,
  right,
  children,
  widthMode = 'standard',
}: {
  title: string
  en: string
  subtitle?: string
  right?: ReactNode
  children: ReactNode
  widthMode?: ContentWidthMode
}) {
  return (
    <div
      style={{
        margin: 'calc(-1 * clamp(16px, 3vw, 32px)) calc(-1 * clamp(16px, 3vw, 36px))',
        background: OPS.bg,
        color: OPS.text,
        minHeight: 'calc(100vh - 54px)',
        fontFamily: OPS.sans,
        paddingBottom: 60,
      }}
    >
      <style dangerouslySetInnerHTML={{ __html: PAGE_CSS + SHELL_CSS }} />
      <ContentShell widthMode={widthMode}>
        <div style={{ padding: '22px 24px 36px' }}>
          <header className="ops-sec" style={{ marginBottom: 18, display: 'flex', alignItems: 'flex-end', gap: 16 }}>
          <div>
            <div
              style={{
                fontFamily: OPS.dot,
                fontSize: 11.5,
                color: OPS.gold,
                letterSpacing: '0.3em',
                marginBottom: 6,
              }}
            >
              {en}
            </div>
            <h1
              style={{
                fontFamily: OPS.sans,
                fontSize: 'clamp(22px, 2vw, 28px)',
                fontWeight: 700,
                color: OPS.text,
                margin: 0,
                letterSpacing: '0.02em',
              }}
            >
              {title}
            </h1>
            {subtitle && (
              <p style={{ fontSize: 13, color: OPS.sub, margin: '6px 0 0', lineHeight: 1.65, maxWidth: 820 }}>
                {subtitle}
              </p>
            )}
          </div>
          {right && <div style={{ marginLeft: 'auto', flexShrink: 0 }}>{right}</div>}
          </header>
          <div
            aria-hidden
            className="ops-sec"
            style={{ height: 1, background: `linear-gradient(90deg, ${OPS.gold}88, ${OPS.gold}22 30%, ${OPS.hairline} 70%)`, marginBottom: 20 }}
          />
          {children}
        </div>
      </ContentShell>
    </div>
  )
}

export function Panel({
  children,
  pad = '18px 20px',
  onClick,
  hover,
  style,
  className,
}: {
  children: ReactNode
  pad?: string
  onClick?: () => void
  hover?: boolean
  style?: React.CSSProperties
  className?: string
}) {
  return (
    <div
      onClick={onClick}
      className={`${hover ? 'ops-card' : ''}${onClick ? ' ops-clickable' : ''}${className ? ' ' + className : ''}`}
      style={{
        background: OPS.panel,
        border: `1px solid ${OPS.border}`,
        borderRadius: 10,
        padding: pad,
        ...style,
      }}
    >
      {children}
    </div>
  )
}

export function PanelTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 12 }}>
      <span style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.gold, letterSpacing: '0.12em', fontWeight: 600 }}>
        {children}
      </span>
      {right && <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, fontSize: 11, color: OPS.dim }}>{right}</span>}
    </div>
  )
}

export function Stat({
  label,
  value,
  unit,
  color,
  sub,
}: {
  label: string
  value: string
  unit?: string
  color?: string
  sub?: string
}) {
  return (
    <div style={{ background: OPS.panelAlt, borderRadius: 8, padding: '12px 14px', minWidth: 0 }}>
      <div style={{ fontSize: 11, color: OPS.dim, marginBottom: 5 }}>{label}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4, minWidth: 0 }}>
        <span
          style={{
            fontFamily: OPS.mono,
            fontSize: 24,
            fontWeight: 500,
            color: color ?? OPS.text,
            letterSpacing: '-0.02em',
            lineHeight: 1,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            minWidth: 0,
          }}
        >
          {value}
        </span>
        {unit && <span style={{ fontSize: 12, color: OPS.dim }}>{unit}</span>}
      </div>
      {sub && <div style={{ fontSize: 10.5, color: OPS.dim, marginTop: 5 }}>{sub}</div>}
    </div>
  )
}

export function Chip({
  children,
  color = OPS.sub,
  bg = OPS.dimBg,
  mono,
  title,
}: {
  children: ReactNode
  color?: string
  bg?: string
  mono?: boolean
  title?: string
}) {
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: '2px 9px',
        borderRadius: 5,
        background: bg,
        color,
        border: `1px solid ${color}2e`,
        fontSize: 11,
        fontFamily: mono ? OPS.mono : OPS.sans,
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  )
}

export function Bar({ pct, color = OPS.gold, height = 6 }: { pct: number; color?: string; height?: number }) {
  return (
    <div style={{ flex: 1, height, background: OPS.hairline, borderRadius: height, overflow: 'hidden' }}>
      <div
        className="ops-bar-fill"
        style={{
          width: `${Math.max(0, Math.min(100, pct))}%`,
          height: '100%',
          background: color,
          borderRadius: height,
        }}
      />
    </div>
  )
}

/**
 * アニメ付きモーダル。position:fixed で全画面。
 * fitViewport=true で中央寄せ + 高さを画面内に収める（ページスクロール無し）。
 */
export function Modal({
  open, onClose, children, width = 640, fitViewport, ariaLabelledBy,
}: {
  open: boolean; onClose: () => void; children: ReactNode; width?: number; fitViewport?: boolean; ariaLabelledBy?: string
}) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const openerRef = useRef<HTMLElement | null>(null)
  const fallbackLabelId = useId()

  useEffect(() => {
    if (!open) return
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
        return
      }
      if (e.key !== 'Tab') return
      const focusable = dialogRef.current
        ? Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        )).filter(el => !el.hasAttribute('hidden'))
        : []
      if (!focusable.length) {
        e.preventDefault()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (e.shiftKey && (active === first || !dialogRef.current?.contains(active))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (active === last || !dialogRef.current?.contains(active))) {
        e.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus())
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('keydown', onKey)
      openerRef.current?.focus()
    }
  }, [open, onClose])

  if (!open) return null
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 100,
        background: 'rgba(6,8,12,0.72)',
        backdropFilter: 'blur(4px)',
        WebkitBackdropFilter: 'blur(4px)',
        display: 'flex',
        alignItems: fitViewport ? 'center' : 'flex-start',
        justifyContent: 'center',
        padding: fitViewport ? '20px' : '8vh 20px 40px',
        overflowY: fitViewport ? 'hidden' : 'auto',
        animation: 'opsBackdropIn .18s ease both',
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={ariaLabelledBy ?? fallbackLabelId}
        onClick={e => e.stopPropagation()}
        style={{
          width: '100%',
          maxWidth: width,
          maxHeight: fitViewport ? 'calc(100vh - 40px)' : undefined,
          background: OPS.panel,
          border: `1px solid ${OPS.gold}44`,
          borderRadius: 12,
          boxShadow: '0 24px 64px rgba(0,0,0,0.6)',
          padding: '22px 24px',
          animation: 'opsModalIn .22s cubic-bezier(.4,0,.2,1) both',
          position: 'relative',
          display: fitViewport ? 'flex' : undefined,
          flexDirection: fitViewport ? 'column' : undefined,
          overflow: fitViewport ? 'hidden' : undefined,
        }}
      >
        {!ariaLabelledBy && <h2 id={fallbackLabelId} style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clipPath: 'inset(50%)' }}>ALMANAC ダイアログ</h2>}
        <button
          ref={closeButtonRef}
          onClick={onClose}
          aria-label="閉じる"
          style={{
            position: 'absolute',
            top: 14,
            right: 16,
            background: 'none',
            border: 'none',
            color: OPS.dim,
            fontSize: 22,
            cursor: 'pointer',
            lineHeight: 1,
            zIndex: 2,
          }}
        >
          ×
        </button>
        {children}
      </div>
    </div>
  )
}

export function Loading({ label = 'ALMANAC LOADING…' }: { label?: string }) {
  return (
    <div
      style={{
        padding: '80px 0',
        textAlign: 'center',
        color: OPS.dim,
        fontFamily: OPS.dot,
        letterSpacing: '0.24em',
        fontSize: 14,
      }}
    >
      {label}
    </div>
  )
}

export function Grid({ cols, gap = 12, children, minmax }: { cols?: number; gap?: number; minmax?: number; children: ReactNode }) {
  return (
    <div
      className={minmax ? undefined : 'ops-grid-cols'}
      style={{
        display: 'grid',
        // minmax(0, 1fr): グリッドアイテムの暗黙の最小幅(auto)を無効化し、
        // 折り返せない長いテキスト（例: "max_sharpe"）でもトラックがコンテナ幅を超えないようにする。
        // 560px未満はops-grid-cols(PAGE_CSS)が1カラムへ強制するので、そちらで可読性を確保する。
        gridTemplateColumns: minmax ? `repeat(auto-fill, minmax(${minmax}px, 1fr))` : `repeat(${cols ?? 3}, minmax(0, 1fr))`,
        gap,
      }}
    >
      {children}
    </div>
  )
}

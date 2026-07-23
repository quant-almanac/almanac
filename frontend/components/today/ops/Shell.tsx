'use client'
import type { ReactNode } from 'react'
import { OPS } from './tokens'

export type ContentWidthMode = 'standard' | 'wide' | 'fluid'

/**
 * Shared content-width tiers. Keep width decisions in CSS so browser zoom and
 * OS display scaling naturally resolve to the effective viewport size.
 */
export const SHELL_CSS = `
.ops-shell { --content-max: 1240px; }
.ops-shell[data-width-mode="wide"] { --content-max: 1240px; }
@media (min-width: 1920px) {
  .ops-shell[data-width-mode="wide"] { --content-max: 1680px; }
}
@media (min-width: 2560px) {
  .ops-shell[data-width-mode="wide"] { --content-max: 2200px; }
}
@media (min-width: 3840px) {
  .ops-shell[data-width-mode="wide"] { --content-max: 2600px; }
}
.ops-shell[data-width-mode="fluid"] { --content-max: min(96vw, 2800px); }
.ops-shell-content {
  width: 100%;
  container-type: inline-size;
  container-name: ops-content;
}
`

export function ContentShell({
  children,
  widthMode = 'standard',
}: {
  children: ReactNode
  widthMode?: ContentWidthMode
}) {
  return (
    <div className="ops-shell" data-width-mode={widthMode}>
      <div className="ops-shell-content" style={{ maxWidth: 'var(--content-max)', margin: '0 auto' }}>
        {children}
      </div>
    </div>
  )
}

/**
 * セクションヘッダー v8 — 連番 + 英語コード + 和名。可読性優先で mono 太字。
 */
export function SectionHead({
  no,
  en,
  jp,
  note,
  right,
}: {
  no: string
  en: string
  jp: string
  note?: React.ReactNode
  right?: React.ReactNode
}) {
  return (
    <div style={{ margin: '0 0 18px' }}>
      <h2
        style={{
          display: 'flex',
          alignItems: 'baseline',
          gap: 14,
          margin: 0,
          fontWeight: 500,
        }}
      >
        <span
          style={{
            fontFamily: OPS.mono,
            fontSize: 14,
            color: OPS.dim,
            letterSpacing: '0.06em',
          }}
        >
          {no}
        </span>
        <span
          style={{
            fontFamily: OPS.mono,
            fontSize: 18,
            fontWeight: 600,
            color: OPS.gold,
            letterSpacing: '0.22em',
          }}
        >
          {en}
        </span>
        <span
          style={{
            fontFamily: OPS.sans,
            fontSize: 16,
            fontWeight: 500,
            color: OPS.text,
            letterSpacing: '0.1em',
          }}
        >
          {jp}
        </span>
        {note && (
          <span
            style={{
              marginLeft: 'auto',
              fontFamily: OPS.mono,
              fontSize: 13,
              fontWeight: 400,
              color: OPS.dim,
            }}
          >
            {note}
          </span>
        )}
        {right && <span style={{ marginLeft: note ? 10 : 'auto', display: 'inline-flex', alignItems: 'center' }}>{right}</span>}
      </h2>
      <div
        aria-hidden
        style={{
          marginTop: 9,
          height: 1,
          background: `linear-gradient(90deg, ${OPS.gold}88, ${OPS.gold}22 30%, ${OPS.hairline} 70%)`,
        }}
      />
    </div>
  )
}

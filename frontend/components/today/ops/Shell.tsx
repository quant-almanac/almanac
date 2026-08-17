'use client'
import type { ReactNode } from 'react'
import { OPS } from './tokens'
import { DoubleRule } from './ornament'

export type ContentWidthMode = 'standard' | 'wide' | 'fluid'

/**
 * Shared content-width tiers. Keep width decisions in CSS so browser zoom and
 * OS display scaling naturally resolve to the effective viewport size.
 */
export const SHELL_CSS = `
.ops-shell { --content-max: min(96vw, 1720px); }
.ops-shell[data-width-mode="wide"] { --content-max: min(96vw, 1720px); }
@media (min-width: 1920px) {
  .ops-shell[data-width-mode="wide"] { --content-max: min(96vw, 1880px); }
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
        {/* 連番は漢数字。暦の章立てらしく、縦罫を添えて版面の起点にする */}
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 10, alignSelf: 'stretch' }}>
          <span
            style={{
              fontFamily: OPS.display,
              fontSize: 17,
              fontWeight: 600,
              color: OPS.gold,
              letterSpacing: '.08em',
            }}
          >
            {kanjiNumeral(no)}
          </span>
          <span aria-hidden style={{ width: 1, alignSelf: 'stretch', background: OPS.border }} />
        </span>
        <span className="ops-latin" style={{ fontSize: 19, color: OPS.gold }}>{en}</span>
        <span
          style={{
            fontFamily: OPS.display,
            fontSize: 17,
            fontWeight: 600,
            color: OPS.text,
            letterSpacing: '0.14em',
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
      <DoubleRule />
    </div>
  )
}

const KANJI = ['〇', '一', '二', '三', '四', '五', '六', '七', '八', '九', '十']

/** "01" → "一"。暦の章番号は漢数字で組む。 */
function kanjiNumeral(no: string): string {
  const n = Number(no)
  if (!Number.isFinite(n) || n < 1) return no
  if (n <= 10) return KANJI[n]
  if (n < 20) return `十${KANJI[n - 10]}`
  return no
}

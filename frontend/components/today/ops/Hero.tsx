'use client'
import { OPS, TYPE_META, STANCE_LABEL, fmtAge } from './tokens'
import type { TodayOps } from './types'

/** 見出し（体言止め・決定論生成） */
function buildHeadline(data: TodayOps): { pre: string; ticker: string | null; post: string } {
  const f = data.focus
  if (!f) return { pre: '新規指値なし。観察継続。', ticker: null, post: '' }
  const t = (f.type && TYPE_META[f.type]?.label) || 'アクション'
  return { pre: '', ticker: f.ticker ?? '', post: ` ${t}を最優先。指値 ${data.board.length} 件。` }
}

function firstSentence(s?: string): string | null {
  if (!s) return null
  const i = s.indexOf('。')
  return i > 0 ? s.slice(0, i + 1) : s
}

/**
 * ヘッドライン v7 — 手紙口調廃止。結論 1 行 + STANCE + 前回比デルタ。
 */
export default function Hero({ data }: { data: TodayOps }) {
  const h = buildHeadline(data)
  const stanceLabel = data.command.stance
    ? STANCE_LABEL[data.command.stance] ?? data.command.stance
    : null
  const operational = data.command.operational_stance
  const lead = firstSentence(data.engine.stance_reason)
  const stale = (data.command.data_age_hours ?? 0) > 24
  const delta = data.delta

  const now = new Date()
  const wd = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'][now.getDay()]

  return (
    <header style={{ padding: '15px 0 4px' }}>
      <div
        style={{
          fontFamily: OPS.mono,
          fontSize: 12,
          color: OPS.dim,
          letterSpacing: '0.08em',
          marginBottom: 12,
          display: 'flex',
          gap: 12,
        }}
      >
        <span style={{ color: OPS.gold }}>
          {now.getFullYear()}.{String(now.getMonth() + 1).padStart(2, '0')}.
          {String(now.getDate()).padStart(2, '0')} {wd}
        </span>
        <span>DAILY BRIEF</span>
        {stale && <span style={{ color: OPS.amber }}>DATA {fmtAge(data.command.data_age_hours)}</span>}
      </div>

      <h1
        style={{
          fontFamily: OPS.sans,
          fontSize: 'clamp(24px, 2.5vw, 34px)',
          fontWeight: 700,
          color: OPS.text,
          lineHeight: 1.45,
          letterSpacing: '0.01em',
          margin: 0,
        }}
      >
        {h.pre}
        {h.ticker && (
          <>
            <span style={{ fontFamily: OPS.mono, color: OPS.gold }}>{h.ticker}</span>
            {h.post}
          </>
        )}
      </h1>

      <div
        style={{
          marginTop: 10,
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'baseline',
          gap: 16,
          fontSize: 13,
        }}
      >
        {stanceLabel && (
          <span style={{ fontFamily: OPS.mono, fontSize: 12.5 }}>
            <span style={{ color: OPS.dim, letterSpacing: '0.1em' }}>STANCE </span>
            <span style={{ color: OPS.gold, fontWeight: 600 }}>{stanceLabel}</span>
          </span>
        )}
        {operational?.label && (
          <span style={{ fontFamily: OPS.mono, fontSize: 12.5 }}>
            <span style={{ color: OPS.dim, letterSpacing: '0.1em' }}>OPERATION </span>
            <span style={{ color: operational.code === 'actionable' ? OPS.green : OPS.amber, fontWeight: 600 }}>{operational.label}</span>
          </span>
        )}
        {delta && (
          <span style={{ fontFamily: OPS.mono, fontSize: 12.5, color: OPS.sub }}>
            <span style={{ color: OPS.dim, letterSpacing: '0.1em' }}>Δ 前回比 </span>
            {delta.added.length > 0 && (
              <span style={{ color: OPS.green }}>
                +{delta.added.map(a => a.ticker).join(' +')}{' '}
              </span>
            )}
            {delta.removed.length > 0 && (
              <span style={{ color: OPS.redSoft }}>
                −{delta.removed.map(a => a.ticker).join(' −')}{' '}
              </span>
            )}
            <span style={{ color: OPS.dim }}>継続 {delta.kept.length}</span>
            {delta.stance_prev !== delta.stance_now && (
              <span style={{ color: OPS.amber }}>
                {' '}
                · スタンス {STANCE_LABEL[delta.stance_prev ?? ''] ?? delta.stance_prev} →{' '}
                {STANCE_LABEL[delta.stance_now ?? ''] ?? delta.stance_now}
              </span>
            )}
          </span>
        )}
      </div>

      {lead && (
        <p
          style={{
            fontSize: 13.5,
            color: OPS.sub,
            lineHeight: 1.8,
            margin: '10px 0 0',
            maxWidth: 880,
          }}
        >
          {lead}
        </p>
      )}
    </header>
  )
}

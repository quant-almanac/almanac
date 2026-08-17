'use client'
import { useState } from 'react'
import { OPS, TYPE_META, fmtJpy, rankGlyph } from './tokens'
import { SelectionPulseSvg } from './motionAccents'
import type { BoardRow } from './types'

export interface RejectedDecision {
  ticker?: string
  action?: string
  reason?: string
  source: string
  verdict?: string
  confidence_pct?: number
  impact_nav_pct?: number
  estimated_notional_jpy?: number
}

/**
 * OrderMap — 発注ボード直結の確信度×影響度スキャッタ。
 * 各ドットは発注リストの ①②③ と一致。hover/select は双方向連動。click で詳細ポップアップ。
 */

const VB_W = 440
const VB_H = 320
const PAD_L = 46
const PAD_T = 20
const PAD_R = 16
const PAD_B = 40
const PLOT_W = VB_W - PAD_L - PAD_R
const PLOT_H = VB_H - PAD_T - PAD_B

function rejectedColor(source: string): string {
  if (source === 'RED TEAM') return OPS.vermilion
  if (source === 'PLAN GATE') return OPS.amber
  return OPS.blue
}

export default function OrderMap({
  board,
  selected,
  hovered,
  onSelect,
  onHover,
  onOpen,
  rejected = [],
  review = [],
}: {
  board: BoardRow[]
  selected: number
  hovered: number | null
  onSelect: (i: number) => void
  onHover: (i: number | null) => void
  onOpen: (i: number) => void
  rejected?: RejectedDecision[]
  /** 要確認（review_board）。発注可能ではないが座標を持つので、判断材料として点にする。 */
  review?: BoardRow[]
}) {
  const [hoveredRejected, setHoveredRejected] = useState<number | null>(null)
  const dots = board
    .map((b, idx) => ({ ...b, idx }))
    .filter(b => b.confidence_pct != null && b.impact_nav_pct != null)
  const rejectedRows = rejected.map((item, idx) => ({ ...item, idx }))
  const plottedRejected = rejectedRows.filter(item => (
    item.confidence_pct != null
    && Number.isFinite(item.confidence_pct)
    && item.confidence_pct >= 0
    && item.confidence_pct <= 100
    && item.impact_nav_pct != null
    && Number.isFinite(item.impact_nav_pct)
    && item.impact_nav_pct >= 0
  ))
  const reviewDots = review
    .map((b, idx) => ({ ...b, idx }))
    .filter(b => b.confidence_pct != null && b.impact_nav_pct != null)
  const yMax = Math.max(
    0.8,
    ...dots.map(d => d.impact_nav_pct as number),
    ...reviewDots.map(d => d.impact_nav_pct as number),
    ...plottedRejected.map(d => d.impact_nav_pct as number),
  ) * 1.35
  const maxNotional = Math.max(1, ...dots.map(d => d.estimated_notional_jpy ?? 0))

  const toX = (c: number) => PAD_L + (c / 100) * PLOT_W
  const toY = (v: number) => PAD_T + PLOT_H - (v / yMax) * PLOT_H
  const cx = toX(50)
  const cy = toY(yMax / 2)
  const hoverRow = hovered != null ? board[hovered] : null
  const hoverRejectedRow = hoveredRejected != null
    ? rejectedRows.find(item => item.idx === hoveredRejected) ?? null
    : null

  return (
    <div
      className="ops-elev"
      style={{
        borderRadius: 10,
        padding: '14px 16px 10px',
        alignSelf: 'start',
        position: 'sticky',
        top: 66,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 3 }}>
        <span style={{ fontFamily: OPS.display, fontSize: 14, color: OPS.text, letterSpacing: '0.12em', fontWeight: 600 }}>
          確信度 × 資産インパクト
        </span>
        <span className="ops-latin" style={{ fontSize: 10, color: OPS.dim }}>IMPACT MAP</span>
        <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, fontSize: 11.5, color: OPS.sub }}>
          採用 {dots.length} · 要確認 {reviewDots.length} · 不採用 {plottedRejected.length}
        </span>
      </div>
      {/* 凡例。実際に描いている系列だけを並べる */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap', margin: '0 0 6px' }}>
        <span style={legendItem}><i style={{ ...legendDot, background: OPS.gold }} />採用<span style={legendNote}>大きさ＝金額</span></span>
        <span style={legendItem}><i style={{ ...legendDot, background: 'transparent', border: `1.5px solid ${OPS.amber}` }} />要確認</span>
        <span style={legendItem}><i style={{ ...legendDot, background: 'transparent', border: `1.5px dashed ${OPS.vermilion}` }} />不採用</span>
      </div>

      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} style={{ width: '100%', height: 'auto', display: 'block' }} aria-label="確信度×影響度マップ">
        <rect x={cx} y={PAD_T} width={toX(100) - cx} height={cy - PAD_T} fill={OPS.goldBg} rx={4} />
        {[25, 50, 75].map(v => (
          <line key={`gx${v}`} x1={toX(v)} y1={PAD_T} x2={toX(v)} y2={toY(0)} stroke={OPS.hairline} strokeWidth={1} strokeDasharray={v === 50 ? '4 4' : undefined} opacity={v === 50 ? 0.8 : 0.5} />
        ))}
        {[0.5].map(f => (
          <line key={`gy${f}`} x1={PAD_L} y1={toY(yMax * f)} x2={toX(100)} y2={toY(yMax * f)} stroke={OPS.hairline} strokeWidth={1} strokeDasharray="4 4" opacity={0.8} />
        ))}
        <line x1={PAD_L} y1={toY(0)} x2={toX(100)} y2={toY(0)} stroke={OPS.border} strokeWidth={1} />
        <line x1={PAD_L} y1={PAD_T} x2={PAD_L} y2={toY(0)} stroke={OPS.border} strokeWidth={1} />

        <text x={cx + 8} y={PAD_T + 16} fontSize={13} fill={OPS.gold} fontFamily={OPS.sans} fontWeight={600}>主戦場</text>
        <text x={PAD_L + 8} y={PAD_T + 16} fontSize={12} fill={OPS.dim} fontFamily={OPS.sans}>要観察</text>
        <text x={cx + 8} y={toY(0) - 8} fontSize={12} fill={OPS.dim} fontFamily={OPS.sans}>流し見</text>
        <text x={PAD_L + 8} y={toY(0) - 8} fontSize={12} fill={OPS.dim} fontFamily={OPS.sans}>優先度低</text>

        {/* x軸: 数値と語の二段。低/中/高があると読み手が位置を掴みやすい */}
        {[0, 50, 100].map(v => (
          <text key={`tx${v}`} x={toX(v)} y={toY(0) + 16} fontSize={12} fill={OPS.dim} textAnchor="middle" fontFamily={OPS.mono}>{v}%</text>
        ))}
        {([[0, '低'], [50, '中'], [100, '高']] as const).map(([v, word]) => (
          <text key={`txw${v}`} x={toX(v)} y={toY(0) + 30} fontSize={12} fill={OPS.sub} textAnchor="middle" fontFamily={OPS.display}>{word}</text>
        ))}
        <text x={PAD_L + PLOT_W / 2} y={VB_H - 4} fontSize={13} fill={OPS.sub} textAnchor="middle" fontFamily={OPS.display} letterSpacing="1.5">確信度</text>

        {/* y軸: 高/中/低 */}
        {([[1, '高'], [0.5, '中'], [0, '低']] as const).map(([f, word]) => (
          <text key={`tyw${f}`} x={PAD_L - 8} y={toY(yMax * f) + 4} fontSize={12} fill={OPS.sub} textAnchor="end" fontFamily={OPS.display}>{word}</text>
        ))}
        <text x={13} y={PAD_T + PLOT_H / 2} fontSize={13} fill={OPS.sub} textAnchor="middle" transform={`rotate(-90,13,${PAD_T + PLOT_H / 2})`} fontFamily={OPS.display} letterSpacing="1.5">資産インパクト</text>

        {/* 要確認。発注可能ではないので塗らず、輪郭だけの琥珀点にする */}
        {reviewDots.map(item => {
          const x = toX(item.confidence_pct as number)
          const y = toY(item.impact_nav_pct as number)
          return (
            <g key={`review-plot-${item.ticker}-${item.idx}`} style={{ cursor: 'help' }}>
              <title>{`${item.ticker} · 要確認\n確信度 ${item.confidence_pct}% · 資産インパクト ${(item.impact_nav_pct as number).toFixed(2)}%`}</title>
              <circle cx={x} cy={y} r={9} fill={OPS.amberBg} stroke={OPS.amber} strokeWidth={1.6} />
              <text x={x} y={y + 4} textAnchor="middle" fontSize={10} fill={OPS.amber} fontFamily={OPS.mono}>!</text>
              <text x={x + 13} y={y + 4} fontSize={11} fill={OPS.sub} fontFamily={OPS.mono}>{item.ticker}</text>
            </g>
          )
        })}

        {plottedRejected.map(item => {
          const x = toX(item.confidence_pct as number)
          const y = toY(item.impact_nav_pct as number)
          const color = rejectedColor(item.source)
          const active = hoveredRejected === item.idx
          const size = active ? 8 : 6
          const label = item.ticker ?? '—'
          return (
            <g
              key={`rejected-plot-${item.source}-${label}-${item.idx}`}
              tabIndex={0}
              role="button"
              aria-label={`${label} 不採用。${item.reason ?? item.action ?? item.verdict ?? ''}`}
              onMouseEnter={() => setHoveredRejected(item.idx)}
              onMouseLeave={() => setHoveredRejected(null)}
              onFocus={() => setHoveredRejected(item.idx)}
              onBlur={() => setHoveredRejected(null)}
              onClick={() => setHoveredRejected(item.idx)}
              onKeyDown={event => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  setHoveredRejected(item.idx)
                }
              }}
              style={{ cursor: 'help', outline: 'none' }}
            >
              <title>{`${label} · ${item.source}\n${item.reason ?? item.action ?? item.verdict ?? '不採用'}`}</title>
              <circle cx={x} cy={y} r={active ? 12 : 10} fill={OPS.inset} stroke={color} strokeWidth={1} strokeDasharray="2 3" opacity={active ? 1 : 0.75} />
              <line x1={x - size} y1={y - size} x2={x + size} y2={y + size} stroke={color} strokeWidth={active ? 2.4 : 1.8} />
              <line x1={x + size} y1={y - size} x2={x - size} y2={y + size} stroke={color} strokeWidth={active ? 2.4 : 1.8} />
              <text x={x} y={y - 15} fontSize={11.5} fontFamily={OPS.mono} textAnchor="middle" fill={active ? color : OPS.sub} fontWeight={600}>
                {label}
              </text>
            </g>
          )
        })}

        {dots.map(d => {
          const x = toX(d.confidence_pct as number)
          const y = toY(d.impact_nav_pct as number)
          const notional = d.estimated_notional_jpy ?? 0
          const baseR = 7 + Math.sqrt(notional / maxNotional) * 11
          const isHover = hovered === d.idx
          const isSel = selected === d.idx
          const active = isHover || isSel
          const r = active ? baseR + 3 : baseR
          const color = (d.type && TYPE_META[d.type]?.color) || OPS.blue
          return (
            <g
              key={d.ticker}
              onMouseEnter={() => onHover(d.idx)}
              onMouseLeave={() => onHover(null)}
              onClick={() => { onSelect(d.idx); onOpen(d.idx) }}
              style={{ cursor: 'pointer' }}
            >
              {/* 選択中は静的なリングで状態を示し続け、選択が変わった瞬間だけ
                  Framer Motion で一度だけ波紋を再生する(常時ループの SMIL は廃止)。 */}
              {isSel && <circle cx={x} cy={y} r={r + 8} fill="none" stroke={OPS.gold} strokeWidth={1.5} opacity={0.7} />}
              <SelectionPulseSvg active={isSel} cx={x} cy={y} r={r + 8} />
              {isHover && !isSel && <circle cx={x} cy={y} r={r + 6} fill="none" stroke={OPS.gold} strokeWidth={1.2} opacity={0.5} />}
              <circle cx={x} cy={y} r={r} fill={color} opacity={active ? 0.5 : 0.3} style={{ transition: 'r .15s ease, opacity .15s ease' }} />
              <circle cx={x} cy={y} r={r} fill="none" stroke={color} strokeWidth={active ? 2.4 : 1.6} style={{ transition: 'r .15s ease' }} />
              <text x={x} y={y - r - 6} fontSize={13} fontFamily={OPS.mono} textAnchor="middle" fill={active ? OPS.gold : OPS.text} fontWeight={600}>
                <tspan fill={color} fontWeight={700}>{rankGlyph(d.idx)} </tspan>
                {d.ticker}
              </text>
            </g>
          )
        })}

      </svg>

      {plottedRejected.length > 0 && (
        <div
          aria-live="polite"
          style={{
            minHeight: 52,
            borderTop: `1px solid ${OPS.hairline}`,
            padding: '9px 2px 2px',
            color: OPS.dim,
            fontSize: 11.5,
            lineHeight: 1.55,
          }}
        >
          {hoverRejectedRow ? (
            <>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, minWidth: 0 }}>
                <span style={{ color: rejectedColor(hoverRejectedRow.source), fontFamily: OPS.mono, fontSize: 12, fontWeight: 700 }}>
                  × {hoverRejectedRow.ticker ?? '—'}
                </span>
                <span style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 9.5 }}>{hoverRejectedRow.source}</span>
                {hoverRejectedRow.confidence_pct != null && hoverRejectedRow.impact_nav_pct != null && (
                  <span style={{ marginLeft: 'auto', color: OPS.sub, fontFamily: OPS.mono, fontSize: 9.5 }}>
                    確信度 {hoverRejectedRow.confidence_pct}% · 影響 {hoverRejectedRow.impact_nav_pct.toFixed(2)}%
                  </span>
                )}
              </div>
              <div style={{ color: OPS.sub, marginTop: 2 }}>
                {hoverRejectedRow.reason ?? hoverRejectedRow.action ?? hoverRejectedRow.verdict ?? '不採用'}
              </div>
            </>
          ) : (
            <span>×印にカーソルを合わせると、不採用・保留理由を確認できます。</span>
          )}
        </div>
      )}

      {hoverRow && (
        <div
          style={{
            position: 'absolute',
            left: 14,
            right: 14,
            bottom: 8,
            background: 'rgba(14,17,23,0.97)',
            border: `1px solid ${OPS.gold}66`,
            borderRadius: 8,
            padding: '8px 12px',
            fontSize: 13,
            lineHeight: 1.55,
            color: OPS.sub,
            pointerEvents: 'none',
          }}
        >
          <span style={{ fontFamily: OPS.mono, color: OPS.gold, fontWeight: 600, marginRight: 8 }}>
            {hovered != null ? `${rankGlyph(hovered)} ` : ''}{hoverRow.ticker}
          </span>
          <span style={{ color: OPS.text }}>{hoverRow.action}</span>
          {hoverRow.estimated_notional_jpy != null && (
            <span style={{ color: OPS.dim, marginLeft: 6 }}>· 想定 {fmtJpy(hoverRow.estimated_notional_jpy)}</span>
          )}
        </div>
      )}
    </div>
  )
}

const legendItem: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 6,
  fontFamily: OPS.sans, fontSize: 12, color: OPS.sub,
}
const legendDot: React.CSSProperties = {
  width: 9, height: 9, borderRadius: '50%', flexShrink: 0, display: 'inline-block',
}
const legendNote: React.CSSProperties = { color: OPS.dim, fontSize: 11 }

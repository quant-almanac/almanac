'use client'
import { useState } from 'react'
import { OPS, TYPE_META, STANCE_LABEL, fmtJpy } from './tokens'
import { SectionHead } from './Shell'
import BenchmarkChart from './BenchmarkChart'
import ScenarioStrip from './ScenarioStrip'
import type {
  BoardRow, Engine, RedTeamVerdict, LaneVerdict, ChartsData, DeltaData, BenchmarkData,
} from './types'

/**
 * RATIONALE 判断の根拠 — シナリオ + 成績チャート（ベンチ/P&L）+ Δ前回比 +
 * 判断ロジック + 採用/棄却の反論・情報レーン。個別の位置は 02 発注の地図に移設。
 */
export default function SignalMap({
  engine,
  board,
  charts,
  delta,
  benchmark,
}: {
  engine: Engine
  board: BoardRow[]
  charts?: ChartsData
  delta?: DeltaData | null
  benchmark?: BenchmarkData | null
}) {
  const adopted = engine.red_team.filter(r => r.verdict !== 'reject')
  const rejected = engine.red_team.filter(r => r.verdict === 'reject')
  const usedLanes = engine.lanes.filter(l => ['adopt', 'partial', 'adopt_partial'].includes(l.verdict))
  const otherLanes = engine.lanes.length - usedLanes.length

  return (
    <section>
      <SectionHead
        no="03"
        en="RATIONALE"
        jp="判断の根拠"
        note={`候補 ${engine.funnel.find(f => f.key === 'tiers')?.count ?? '—'} → 最終 ${board.length}（個別の位置は 02 発注の地図）`}
      />

      {/* シナリオ（Strategy 統合） */}
      <ScenarioStrip />

      {/* 成績チャート */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 280px), 1fr))', gap: 16, alignItems: 'stretch' }}>
        {benchmark && <BenchmarkChart data={benchmark} />}
        <PnlChart data={charts?.pnl ?? []} />
      </div>

      <DeltaPanel delta={delta} />

      {/* 判断根拠 + 漏斗 */}
      <div style={{ marginTop: 18 }}>
        {engine.stance_reason && (
          <p style={{ fontSize: 13.5, color: OPS.sub, lineHeight: 1.9, margin: 0 }}>
            <span
              style={{
                fontFamily: OPS.mono,
                fontSize: 11.5,
                color: OPS.gold,
                letterSpacing: '0.14em',
                marginRight: 10,
                fontWeight: 600,
              }}
            >
              LOGIC
            </span>
            {engine.stance_reason}
          </p>
        )}
        <p style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.dim, margin: '10px 0 0', letterSpacing: '0.02em' }}>
          {engine.funnel.map((s, i) => (
            <span key={s.key}>
              {i > 0 && <span style={{ margin: '0 8px' }}>→</span>}
              {s.label}{' '}
              <span style={{ color: s.hot ? OPS.gold : OPS.sub, fontWeight: 500 }}>{s.count}</span>
            </span>
          ))}
        </p>
      </div>

      {/* 反論と情報の3列 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 240px), 1fr))', gap: 24, marginTop: 20 }}>
        <VerdictColumn title="採用した反論" accent={OPS.green} empty="採用なし" items={adopted.map(redTeamItem)} />
        <VerdictColumn title="棄却した反論" accent={OPS.redSoft} empty="棄却なし" items={rejected.map(redTeamItem)} />
        <VerdictColumn
          title="採用した情報レーン"
          accent={OPS.blue}
          empty="採用なし"
          items={usedLanes.map(laneItem)}
          footer={otherLanes > 0 ? `ほか ${otherLanes} 件は棄却・無視` : undefined}
        />
      </div>

    </section>
  )
}

/* ── 前回比デルタ ───────────────────────────────────────── */

function DeltaPanel({ delta }: { delta?: DeltaData | null }) {
  if (!delta) return null
  const chip = (t: string, ty: string, color: string) => (
    <span
      key={`${t}-${ty}`}
      style={{
        fontFamily: OPS.mono,
        fontSize: 12,
        color,
        border: `1px solid ${color}55`,
        borderRadius: 4,
        padding: '2px 8px',
      }}
    >
      {t} <span style={{ opacity: 0.7 }}>{TYPE_META[ty]?.label ?? ty}</span>
    </span>
  )
  return (
    <div
      className="ops-card"
      style={{
        background: OPS.panel,
        border: `1px solid ${OPS.border}`,
        borderRadius: 10,
        padding: '12px 16px',
        marginTop: 14,
        display: 'flex',
        flexWrap: 'wrap',
        gap: 8,
        alignItems: 'center',
      }}
    >
      <span style={{ fontFamily: OPS.mono, fontSize: 11.5, color: OPS.gold, letterSpacing: '0.14em', fontWeight: 600 }}>
        Δ 前回分析比
      </span>
      <span style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.dim }}>({delta.prev_as_of})</span>
      {delta.added.map(a => chip(a.ticker, a.type, OPS.green))}
      {delta.removed.map(a => chip(a.ticker, a.type, OPS.redSoft))}
      {delta.added.length === 0 && delta.removed.length === 0 && (
        <span style={{ fontSize: 12, color: OPS.dim }}>アクション構成に変化なし</span>
      )}
      <span style={{ fontFamily: OPS.mono, fontSize: 11.5, color: OPS.dim, marginLeft: 'auto' }}>
        継続 {delta.kept.length} · スタンス{' '}
        {delta.stance_prev === delta.stance_now
          ? '変化なし'
          : `${STANCE_LABEL[delta.stance_prev ?? ''] ?? delta.stance_prev} → ${
              STANCE_LABEL[delta.stance_now ?? ''] ?? delta.stance_now
            }`}
      </span>
    </div>
  )
}

/* ── 累積損益チャート ───────────────────────────────────── */

function PnlChart({ data }: { data: { d: string; v: number }[] }) {
  if (data.length < 2) return null
  const W = 460
  const H = 130
  const PAD = { l: 8, r: 58, t: 12, b: 18 }
  const vals = data.map(p => p.v)
  let min = Math.min(...vals, 0)
  let max = Math.max(...vals, 0)
  const range = max - min || 1
  min -= range * 0.08
  max += range * 0.08

  const toX = (i: number) => PAD.l + (i / (data.length - 1)) * (W - PAD.l - PAD.r)
  const toY = (v: number) => PAD.t + (1 - (v - min) / (max - min)) * (H - PAD.t - PAD.b)

  const line = vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join('')
  const area = `${line}L${toX(vals.length - 1).toFixed(1)},${toY(0)}L${toX(0).toFixed(1)},${toY(0)}Z`
  const last = vals[vals.length - 1]
  const up = last >= 0
  const color = up ? OPS.green : OPS.redSoft

  return (
    <div
      className="ops-card"
      style={{ background: OPS.panel, border: `1px solid ${OPS.border}`, borderRadius: 10, padding: '12px 16px', flex: 1 }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', fontFamily: OPS.mono, fontSize: 11.5, marginBottom: 4 }}>
        <span style={{ color: OPS.gold, letterSpacing: '0.14em', fontWeight: 600 }}>
          P&L 累積（参考・{data.length}日）
        </span>
        <span style={{ marginLeft: 'auto', fontSize: 16, fontWeight: 600, color }}>
          {up ? '+' : ''}
          {fmtJpy(last)}
        </span>
      </div>
      <div style={{ fontSize: 10.5, color: OPS.dim, marginBottom: 2 }}>円建て · 入出金未調整</div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }} aria-label="累積損益チャート">
        <line x1={PAD.l} y1={toY(0)} x2={W - PAD.r} y2={toY(0)} stroke={OPS.border} strokeWidth={1} strokeDasharray="3 3" />
        <text x={W - PAD.r + 5} y={toY(0) + 3} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono}>¥0</text>
        <path d={area} fill={color} opacity={0.1} />
        <path d={line} stroke={color} strokeWidth={1.6} fill="none" />
        <circle cx={toX(vals.length - 1)} cy={toY(last)} r={2.6} fill={color} />
        <text x={toX(0)} y={H - 3} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono}>{data[0].d}</text>
        <text x={toX(vals.length - 1)} y={H - 3} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono} textAnchor="end">
          {data[data.length - 1].d}
        </text>
      </svg>
    </div>
  )
}

/* ── 反論/レーンの3列 ───────────────────────────────────── */

interface VerdictItem {
  key: string
  head: React.ReactNode
  body: string
  suffix?: string
}

function redTeamItem(r: RedTeamVerdict, i: number): VerdictItem {
  return {
    key: `rt-${i}`,
    head: (
      <>
        {r.ticker && (
          <span style={{ fontFamily: OPS.mono, color: OPS.text, fontWeight: 500, marginRight: 6 }}>{r.ticker}</span>
        )}
        {r.hypothesis ?? r.action ?? ''}
      </>
    ),
    body: r.reason ?? r.verdict_reason ?? '',
    suffix: r.adopted_as || undefined,
  }
}

function laneItem(l: LaneVerdict, i: number): VerdictItem {
  return {
    key: `lane-${i}`,
    head: (
      <>
        <span style={{ fontFamily: OPS.mono, color: OPS.text, fontWeight: 500, marginRight: 6 }}>{l.ticker}</span>
        <span style={{ color: OPS.dim }}>{l.lane}</span>
      </>
    ),
    body: l.verdict_reason ?? '',
    suffix: l.adopted_as && l.adopted_as !== 'n/a' ? l.adopted_as : undefined,
  }
}

function VerdictColumn({
  title,
  accent,
  items,
  empty,
  footer,
}: {
  title: string
  accent: string
  items: VerdictItem[]
  empty: string
  footer?: string
}) {
  const [showAll, setShowAll] = useState(false)
  const visible = showAll ? items : items.slice(0, 3)

  return (
    <div style={{ borderLeft: `2px solid ${accent}66`, paddingLeft: 14 }}>
      <h3
        style={{
          fontSize: 13.5,
          fontWeight: 600,
          color: OPS.text,
          margin: '0 0 10px',
          letterSpacing: '0.06em',
        }}
      >
        {title}
        <span style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.dim, marginLeft: 8, fontWeight: 400 }}>
          {items.length}
        </span>
      </h3>

      {items.length === 0 && <p style={{ fontSize: 12, color: OPS.dim, margin: 0 }}>{empty}</p>}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {visible.map(it => (
          <div key={it.key} style={{ fontSize: 12.5, lineHeight: 1.7 }}>
            <div style={{ color: OPS.text, marginBottom: 2 }}>{it.head}</div>
            <div style={{ color: OPS.dim }}>
              {it.body}
              {it.suffix && <span style={{ color: OPS.sub }}> → {it.suffix}</span>}
            </div>
          </div>
        ))}
      </div>

      {items.length > 3 && (
        <button
          onClick={() => setShowAll(!showAll)}
          style={{
            background: 'none',
            border: 'none',
            padding: '8px 0 0',
            cursor: 'pointer',
            fontSize: 12,
            color: OPS.gold,
            fontFamily: OPS.sans,
          }}
        >
          {showAll ? '折りたたむ' : `残り ${items.length - 3} 件`}
        </button>
      )}
      {footer && <p style={{ fontSize: 11.5, color: OPS.dim, margin: '8px 0 0' }}>{footer}</p>}
    </div>
  )
}

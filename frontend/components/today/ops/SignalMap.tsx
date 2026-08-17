'use client'
import { useState } from 'react'
import { OPS, TYPE_META, STANCE_LABEL } from './tokens'
import { SectionHead } from './Shell'
import PerformanceChart from './PerformanceChart'
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

      <RationaleSummary engine={engine} delta={delta} benchmark={benchmark} />

      {/* 折り畳みは廃止。隠すほどの情報ではなく、開かないと存在に気づけなかった。 */}
      <div style={{ marginTop: 12 }}>

      {/* シナリオ（Strategy 統合） */}
      <ScenarioStrip />

      {/* 成績チャート: 腕前(%)と金額(円)を1枠のタブに統合 */}
      <PerformanceChart benchmark={benchmark} pnl={charts?.pnl ?? []} />

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

      </div>

    </section>
  )
}

function RationaleSummary({ engine, delta, benchmark }: {
  engine: Engine
  delta?: DeltaData | null
  benchmark?: BenchmarkData | null
}) {
  const adopted = engine.red_team.filter(item => item.verdict !== 'reject')
  const rejected = engine.red_team.filter(item => item.verdict === 'reject')
  const used = engine.lanes.filter(item => ['adopt', 'partial', 'adopt_partial'].includes(item.verdict))
  const lastTwr = benchmark?.portfolio?.at(-1)
  const cards = [
    { en: 'MARKET REGIME', title: engine.operational_stance?.label ?? '市場判断', value: engine.funnel.at(-1)?.count != null ? `最終 ${engine.funnel.at(-1)?.count}` : '観測中', body: engine.operational_stance?.reason ?? engine.stance_reason ?? '市場環境を継続観測します。', ink: OPS.paperGreenInk },
    { en: 'PORTFOLIO', title: 'ベンチマーク比較', value: lastTwr != null ? `${lastTwr >= 0 ? '+' : ''}${lastTwr.toFixed(2)}%` : '—', body: benchmark?.outperf.sp500 != null ? `S&P500円比 ${benchmark.outperf.sp500 >= 0 ? '+' : ''}${benchmark.outperf.sp500.toFixed(2)}pt` : '比較データを集計中', ink: OPS.paperBlueInk },
    { en: 'DELTA', title: '前回分析との差', value: delta ? `＋${delta.added.length} / −${delta.removed.length}` : '—', body: delta ? `継続 ${delta.kept.length}件 · ${delta.prev_as_of ?? '前回'}比` : '比較可能な前回分析なし', ink: OPS.paperBlueInk },
    { en: 'ADOPTED', title: '採用した反論', value: `${adopted.length}件`, body: adopted[0] ? redTeamItem(adopted[0], 0).body : '採用した反論はありません。', ink: OPS.paperGreenInk },
    { en: 'REJECTED', title: '棄却した反論', value: `${rejected.length}件`, body: rejected[0] ? redTeamItem(rejected[0], 0).body : '棄却した反論はありません。', ink: OPS.paperVermilionInk },
    { en: 'AI LANES', title: '採用した情報レーン', value: `${used.length} / ${engine.lanes.length}`, body: used.slice(0, 3).map(item => item.lane).join(' · ') || '採用レーンなし', ink: OPS.paperBlueInk },
  ]

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(155px,1fr))', gap: 7 }}>
      {cards.map(card => (
        <article key={card.en} style={{ minWidth: 0, minHeight: 126, border: `1px solid ${OPS.paperBorder}`, borderRadius: 8, padding: '11px 12px', background: OPS.paper, color: OPS.paperText }}>
          <div className="ops-latin" style={{ color: card.ink, fontSize: 8.5, letterSpacing: '.13em' }}>{card.en}</div>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 7, marginTop: 7 }}>
            <strong style={{ fontFamily: OPS.display, fontSize: 13, letterSpacing: '.05em' }}>{card.title}</strong>
            <b style={{ color: card.ink, fontFamily: OPS.mono, fontSize: 13, whiteSpace: 'nowrap' }}>{card.value}</b>
          </div>
          <p style={{ color: OPS.paperSub, fontSize: 10.5, lineHeight: 1.55, margin: '9px 0 0', display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{card.body}</p>
        </article>
      ))}
    </div>
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
        // background/border/shadow は .ops-card が供給する（inline で上書きしない）
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

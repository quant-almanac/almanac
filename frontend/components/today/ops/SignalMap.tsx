'use client'
import { OPS, TYPE_META, STANCE_LABEL } from './tokens'
import { SectionHead } from './Shell'
import PerformanceChart from './PerformanceChart'
import ScenarioStrip from './ScenarioStrip'
import type {
  BoardRow, Engine, RedTeamVerdict, ChartsData, DeltaData, BenchmarkData,
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

      {/* 反論・情報レーンの内訳は DECISION FLOW の表に統合した (2026-08-19)。
          同じ engine.red_team / engine.lanes を、ここでは3列カードとして、
          DECISION FLOW では銘柄ごとの停止点と同じ表として、別々に描いていた。
          結論(何件採用・何件棄却)はこの上の RationaleSummary カードに残す。 */}

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


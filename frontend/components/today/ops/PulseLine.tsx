'use client'

import { useEffect, useMemo, useState } from 'react'
import { OPS, STANCE_LABEL } from './tokens'
import type { BenchmarkData, Command, TodayOps } from './types'
import {
  beatSeconds, computeVitals, ecgTileDataUri, marketRiskScore, ownRiskScore, relationVerdict,
} from './pulseVitals'

/**
 * MARKET PULSE — 市場の鼓動。
 *
 * 価格の折れ線ではない。張り詰め具合を心電図の「拍の速さ」として鳴らす。
 * リスクが高いほど速く打つ。
 *
 * 3つ並べる:
 *   自分   … 月間DD・ガード状態。自分の資産が今どれだけ危ないか
 *   市場   … VIX。相場そのものがどれだけ荒れているか
 *   総合   … 上2つの合成。実際の判断はこれを見る
 *
 * 合成だけだと「なぜ速いのか」が消えるので、必ず3つ一緒に見せる。
 * この波形は体感用の表示で、売買判断には使わない(停止判断は behavioral_guard が権威)。
 */

const CYCLE_W = 108
const LANE_H = 46

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(prefersReducedMotion)
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return reduced
}

function signed(value: number | null | undefined, suffix = '%'): string {
  return value == null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}${suffix}`
}

function lastFinite(values?: (number | null)[]): number | null {
  if (!values?.length) return null
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const v = values[i]
    if (typeof v === 'number' && Number.isFinite(v)) return v
  }
  return null
}

function toneFor(score: number | null): string {
  if (score == null) return OPS.dim
  return score < 25 ? OPS.green : score < 50 ? OPS.blue : score < 75 ? OPS.amber : OPS.vermilion
}

function stateFor(score: number | null): string {
  if (score == null) return 'データなし'
  return score < 25 ? '平静' : score < 50 ? 'やや緊張' : score < 75 ? '緊張' : '警戒'
}

/** 心電図1レーン。score から拍数を決めて波形を流す。 */
function PulseLane({
  label, sub, score, detail, emphasis = false, reducedMotion,
}: {
  label: string
  sub: string
  score: number | null
  detail: string
  emphasis?: boolean
  reducedMotion: boolean
}) {
  const vitals = useMemo(() => (score == null ? null : computeVitals(score, null)), [score])
  const tone = toneFor(score)
  const tile = useMemo(() => ecgTileDataUri({
    width: CYCLE_W, height: LANE_H, color: tone, flat: !vitals,
    strokeWidth: emphasis ? 2 : 1.5,
  }), [tone, vitals, emphasis])
  const beat = vitals ? beatSeconds(vitals.bpm) : 0

  return (
    <div className={`pulse-lane${emphasis ? ' is-total' : ''}`}>
      <div className="pulse-lane-id">
        <b style={{ color: emphasis ? OPS.gold : OPS.text }}>{label}</b>
        <i>{sub}</i>
      </div>

      <div className="pulse-lane-ecg">
        <div
          className={`pulse-lane-track${vitals && !reducedMotion ? ' is-beating' : ''}`}
          aria-hidden="true"
          style={{
            backgroundImage: tile,
            backgroundSize: `${CYCLE_W}px ${LANE_H}px`,
            filter: `drop-shadow(0 0 3px ${tone}55)`,
            animationDuration: beat ? `${beat.toFixed(3)}s` : undefined,
          }}
        />
      </div>

      <div className="pulse-lane-vitals">
        <span className="pulse-lane-bpm" style={{ color: tone, fontSize: emphasis ? 25 : 19 }}>
          {vitals ? vitals.bpm : '—'}
        </span>
        <span className="pulse-lane-unit">BPM</span>
        <span className="pulse-lane-state" style={{ color: tone }}>{stateFor(score)}</span>
        <span className="pulse-lane-detail">{detail}</span>
      </div>
    </div>
  )
}

export default function PulseLine({
  command, pulse, benchmark,
}: {
  command?: Command
  pulse?: TodayOps['pulse']
  benchmark?: BenchmarkData | null
}) {
  const reducedMotion = useReducedMotion()

  const guard = command?.guard
  const vix = pulse?.vix ?? command?.vix ?? null
  const market = useMemo(() => marketRiskScore(vix), [vix])
  const own = useMemo(() => ownRiskScore(guard), [guard])
  // 総合レーンは置かない。2つを平均すると「市場だけ荒れている」と
  // 「自分だけ痛んでいる」が同じ数字に潰れ、一番知りたい違いが消えるため。
  // 代わりに組み合わせに名前を付ける。
  const relation = useMemo(() => relationVerdict(own, market), [own, market])

  const stance = command?.stance ? STANCE_LABEL[command.stance] ?? command.stance : '—'
  const guardOpen = Boolean(guard)
    && guard?.new_entry_allowed !== false
    && guard?.trading_allowed !== false
    && (guard?.alerts.length ?? 0) === 0

  const japanChange = lastFinite(benchmark?.nikkei)
  const usChange = lastFinite(benchmark?.sp500)
  const monthly = guard?.monthly_pnl_pct

  const ownDetail = own == null
    ? 'ガード情報なし'
    : `月間 ${monthly == null ? '—' : signed(monthly * 100)}・ガード ${guardOpen ? '開' : '締'}`
  const marketDetail = vix == null ? 'VIX不明' : `VIX ${vix.toFixed(1)}・${signed(pulse?.vix_change_1d)}`
  const relationTone = !relation ? OPS.dim
    : relation.key === 'both_tense' ? OPS.vermilion
    : relation.key === 'own_led' ? OPS.amber
    : relation.key === 'market_led' ? OPS.blue
    : relation.key === 'calm' ? OPS.green
    : OPS.sub

  return <section className="market-pulse ops-elev" aria-label="市場の鼓動">
    <style dangerouslySetInnerHTML={{ __html: `
      .market-pulse { border-radius:10px; padding:9px 13px 10px; background:${OPS.panel}; overflow:hidden; }
      .pulse-title { display:flex; align-items:baseline; gap:9px; min-height:15px; flex-wrap:wrap; }
      .pulse-state-chips { margin-left:auto; display:inline-flex; align-items:baseline; gap:8px; color:${OPS.dim}; font-family:${OPS.mono}; font-size:9.5px; white-space:nowrap; }
      .pulse-state-chips i { color:${OPS.dim}; font-family:${OPS.brand}; font-size:8.5px; font-style:normal; letter-spacing:.12em; }

      .pulse-lanes { display:flex; flex-direction:column; gap:5px; margin-top:6px; }
      .pulse-lane { display:grid; grid-template-columns:66px minmax(0,1fr) 178px; gap:10px; align-items:center; }
      .pulse-lane.is-total { padding-top:6px; border-top:1px solid ${OPS.hairline}; }

      .pulse-lane-id { display:flex; flex-direction:column; gap:1px; }
      .pulse-lane-id b { font-family:${OPS.display}; font-size:12px; font-weight:600; line-height:1.15; }
      .pulse-lane-id i { color:${OPS.dim}; font-family:${OPS.brand}; font-size:7.5px; font-style:normal; letter-spacing:1px; }

      .pulse-lane-ecg { position:relative; height:${LANE_H}px; overflow:hidden; border-radius:6px;
        background:
          repeating-linear-gradient(90deg, ${OPS.hairline}44 0 1px, transparent 1px 22px),
          repeating-linear-gradient(0deg, ${OPS.hairline}2b 0 1px, transparent 1px 15px),
          ${OPS.sunken};
        box-shadow:inset 0 0 0 1px ${OPS.hairline}; }
      .pulse-lane-track { position:absolute; top:0; bottom:0; left:0; width:calc(100% + ${CYCLE_W}px);
        background-repeat:repeat-x; background-position:left center; }
      @keyframes pulseScroll { from { transform:translateX(0); } to { transform:translateX(-${CYCLE_W}px); } }
      .pulse-lane-track.is-beating { animation-name:pulseScroll; animation-timing-function:linear; animation-iteration-count:infinite; }
      .pulse-lane-ecg::after { content:''; position:absolute; top:0; bottom:0; right:0; width:40px;
        background:linear-gradient(90deg, transparent, ${OPS.sunken}); pointer-events:none; }

      .pulse-lane-vitals { display:grid; grid-template-columns:auto auto 1fr; grid-template-rows:auto auto;
        column-gap:5px; align-items:baseline; }
      .pulse-lane-bpm { grid-row:1; font-family:${OPS.mono}; font-weight:600; line-height:1; }
      .pulse-lane-unit { grid-row:1; font-family:${OPS.brand}; font-size:8px; letter-spacing:.14em; color:${OPS.dim}; }
      .pulse-lane-state { grid-row:1; font-family:${OPS.display}; font-size:11px; letter-spacing:.06em; justify-self:end; }
      .pulse-lane-detail { grid-row:2; grid-column:1 / -1; font-family:${OPS.mono}; font-size:8.5px; color:${OPS.dim}; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

      .pulse-relation { display:flex; align-items:baseline; gap:9px; flex-wrap:wrap;
        margin-top:7px; padding-top:6px; border-top:1px solid ${OPS.hairline}; }
      .pulse-relation b { font-family:${OPS.display}; font-size:13px; letter-spacing:.08em; }
      .pulse-relation span { color:${OPS.dim}; font-family:${OPS.sans}; font-size:10.5px; }

      .pulse-readout { display:flex; flex-wrap:wrap; gap:2px 16px; margin-top:8px; padding-top:7px;
        border-top:1px solid ${OPS.hairline}; }
      .pulse-readout span { font-family:${OPS.mono}; font-size:9px; color:${OPS.dim}; white-space:nowrap; }
      .pulse-readout b { color:${OPS.sub}; font-weight:500; }
      .pulse-sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }

      @media (prefers-reduced-motion:reduce) { .pulse-lane-track.is-beating { animation:none; } }
      @media (max-width:760px) {
        .pulse-state-chips { display:none; }
        .pulse-lane { grid-template-columns:54px minmax(0,1fr); grid-template-rows:auto auto; }
        .pulse-lane-vitals { grid-column:1 / -1; grid-template-columns:auto auto auto 1fr; }
        .pulse-lane-detail { grid-row:2; }
      }
    ` }} />

    <div className="pulse-title">
      <strong className="ops-latin" style={{ color: OPS.gold, fontSize: 12.5 }}>MARKET PULSE</strong>
      <span style={{ fontFamily: OPS.display, color: OPS.sub, fontSize: 11.5, letterSpacing: '.08em' }}>市場の鼓動</span>
      <span style={{ color: OPS.dim, fontFamily: OPS.sans, fontSize: 9.5 }}>速いほど張り詰めている</span>
      <span className="pulse-state-chips">
        <b style={{ color: OPS.green }}>{command?.scenario ?? '—'}</b>
        <i>STANCE</i><b style={{ color: OPS.sub }}>{stance}</b>
        <i>GUARD</i><b style={{ color: guardOpen ? OPS.green : OPS.vermilion }}>{guardOpen ? 'OPEN' : 'CHECK'}</b>
      </span>
    </div>

    <div className="pulse-lanes">
      <PulseLane label="自分" sub="PORTFOLIO" score={own} detail={ownDetail} reducedMotion={reducedMotion} />
      <PulseLane label="市場" sub="MARKET" score={market} detail={marketDetail} reducedMotion={reducedMotion} />
    </div>

    {/* 2つを平均した数字ではなく、組み合わせが何を意味するかを書く */}
    <div className="pulse-relation">
      <b style={{ color: relationTone }}>{relation ? relation.label : '判定不能'}</b>
      <span>{relation ? relation.detail : '自分・市場のどちらも判定に必要なデータが揃っていない'}</span>
    </div>

    <div className="pulse-readout" aria-hidden="true">
      <span>VIX <b>{vix == null ? '—' : vix.toFixed(1)}</b> {signed(pulse?.vix_change_1d)}</span>
      <span>原油 <b>{pulse?.oil_price == null ? '—' : pulse.oil_price.toFixed(1)}</b> {signed(pulse?.oil_change_1d_pct)}</span>
      <span>米10年 <b>{pulse?.us_10y == null ? '—' : pulse.us_10y.toFixed(2)}</b> {signed(pulse?.us_10y_change_1d_pt, 'pt')}</span>
      <span>ドル指数 <b>{pulse?.dxy_level == null ? '—' : pulse.dxy_level.toFixed(1)}</b> {signed(pulse?.dxy_change_1d_pct)}</span>
      <span>日本株 <b>{signed(japanChange)}</b></span>
      <span>米国株 <b>{signed(usChange)}</b></span>
    </div>

    <p className="pulse-sr-only">
      {relation
        ? `市場の鼓動。自分の資産 ${own ?? '不明'}（${stateFor(own)}）、市場 ${market ?? '不明'}（${stateFor(market)}）。${relation.label} — ${relation.detail}。`
        : '市場の鼓動: 判定に必要なデータが取得できていません。'}
      {` VIX ${vix ?? '不明'}。月間損益 ${monthly == null ? '不明' : signed(monthly * 100)}。`}
    </p>
  </section>
}

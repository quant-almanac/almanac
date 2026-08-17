'use client'
import { OPS, STANCE_LABEL, fmtAge } from './tokens'
import type { TodayOps } from './types'

function firstSentence(s?: string): string | null {
  if (!s) return null
  const i = s.indexOf('。')
  return i > 0 ? s.slice(0, i + 1) : s
}

function marketBrief(data: TodayOps, guardOk: boolean): string {
  const scenario = data.command.scenario
  const regime = scenario === 'BULL'
    ? '株式市場は強気基調を維持しています'
    : scenario === 'BEAR'
      ? '株式市場は慎重な局面が続いています'
      : '市場は方向感を見極める局面です'
  const vix = data.command.vix
  const volatility = vix == null
    ? '変動性データを確認中です'
    : vix < 18
      ? `VIX ${vix.toFixed(1)}で変動性は低位です`
      : vix <= 28
        ? `VIX ${vix.toFixed(1)}で変動性は通常域です`
        : `VIX ${vix.toFixed(1)}で変動性は高い状態です`
  const gate = guardOk ? '市場ガードは開いていますが、個別候補は安全条件を優先します。' : '市場ガードの警告を優先し、新規判断は慎重に扱います。'
  return `${regime}。${volatility}。${gate}`
}

/**
 * ヘッドライン v7 — 手紙口調廃止。結論 1 行 + STANCE + 前回比デルタ。
 */
export default function Hero({ data }: { data: TodayOps }) {
  const stanceLabel = data.command.stance
    ? STANCE_LABEL[data.command.stance] ?? data.command.stance
    : null
  const operational = data.command.operational_stance
  const rawLead = firstSentence(data.engine.stance_reason)
  const stale = (data.command.data_age_hours ?? 0) > 24
  const guardOk = data.command.guard.new_entry_allowed !== false
    && data.command.guard.trading_allowed !== false
    && data.command.guard.alerts.length === 0
  const sentiment = data.command.fear_greed == null ? '—' : data.command.fear_greed >= 60 ? '強気' : data.command.fear_greed <= 40 ? '慎重' : '中立'
  const volatility = data.command.vix == null ? '—' : data.command.vix < 18 ? 'やや低下' : data.command.vix <= 28 ? '通常' : '高い'
  const confidence = data.focus?.confidence_pct
  const lead = rawLead && !/regime_|stance_guard|[a-z]+_[a-z]+/.test(rawLead)
    ? rawLead
    : marketBrief(data, guardOk)

  const now = new Date()
  const wd = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'][now.getDay()]

  const wdJp = ['日', '月', '火', '水', '木', '金', '土'][now.getDay()]

  return (
    // 暦の「紙面」。山並みはこのブリーフだけに留め、ページ全体の環境光にはしない。
    <header className="today-brief" style={{ position: 'relative', isolation: 'isolate', overflow: 'hidden', padding: '22px 24px 22px', border: `1px solid ${OPS.border}`, borderRadius: 12, background: '#07192b' }}>
      <style dangerouslySetInnerHTML={{ __html: `
        .today-brief { background:
          radial-gradient(ellipse 48% 42% at 79% 56%, rgba(236,171,98,.24), transparent 66%),
          radial-gradient(ellipse 90% 72% at 72% 42%, rgba(72,124,176,.18), transparent 73%),
          linear-gradient(128deg,#0a2a47 0%,#071b30 52%,#051523 100%) !important; }
        .today-brief::before { content:''; position:absolute; z-index:-1; left:-5%; right:-5%; bottom:-3%; height:45%;
          background:linear-gradient(180deg,#153b55 0%,#09243a 74%); opacity:.88;
          clip-path:polygon(0 56%,8% 43%,16% 53%,25% 27%,34% 50%,43% 34%,53% 58%,63% 30%,72% 49%,82% 23%,91% 45%,100% 31%,100% 100%,0 100%); }
        .today-brief::after { content:''; position:absolute; z-index:-1; left:-8%; right:-8%; bottom:-8%; height:36%;
          background:#061827; opacity:.94;
          clip-path:polygon(0 48%,13% 31%,24% 49%,38% 22%,49% 48%,61% 27%,73% 52%,87% 29%,100% 51%,100% 100%,0 100%); }
        .today-brief-metrics { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); margin-top:14px; border:1px solid rgba(63,96,119,.72); border-radius:7px; background:rgba(2,14,25,.5); }
        .today-brief-metric { min-width:0; padding:9px 10px; text-align:center; }
        .today-brief-metric + .today-brief-metric { border-left:1px solid rgba(63,96,119,.72); }
        @container ops-content (max-width:620px) { .today-brief-metrics { grid-template-columns:repeat(2,minmax(0,1fr)); } .today-brief-metric + .today-brief-metric { border-left:0; border-top:1px solid rgba(63,96,119,.72); } }
      ` }} />
      <div
        className="ops-pop"
        style={{ display: 'flex', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap', marginBottom: 24 }}
      >
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
            <span style={{ fontFamily: OPS.display, fontSize: 27, fontWeight: 600, color: OPS.text, letterSpacing: '.04em' }}>
              {now.getFullYear()}.{String(now.getMonth() + 1).padStart(2, '0')}.{String(now.getDate()).padStart(2, '0')}
            </span>
            <span style={{ fontFamily: OPS.display, fontSize: 15, color: OPS.sub, letterSpacing: '.1em' }}>{wdJp}曜</span>
            <span className="ops-latin" style={{ fontSize: 12, color: OPS.dim }}>{wd}</span>
            <span className="ops-latin" style={{ fontSize: 12, color: OPS.dim }}>DAILY BRIEF</span>
            {stale && <span style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.amber }}>DATA {fmtAge(data.command.data_age_hours)}</span>}
          </div>
        </div>

      </div>

      <h1
        className="ops-pop"
        style={{
          fontFamily: OPS.display,
          fontSize: 'clamp(37px, 3.4vw, 54px)',
          fontWeight: 600,
          color: OPS.text,
          lineHeight: 1.14,
          letterSpacing: '0.03em',
          margin: 0,
          animationDelay: '60ms',
        }}
      >
        Today
      </h1>

      {lead && <p className="ops-pop" style={{ animationDelay: '95ms', fontSize: 14, color: OPS.text, lineHeight: 1.72, margin: '7px 0 0', maxWidth: 700, textShadow: '0 1px 12px rgba(0,0,0,.34)' }}>{lead}</p>}

      <div
        className="ops-pop"
        style={{
          animationDelay: '130ms',
          marginTop: 13,
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'baseline',
          gap: 16,
          fontSize: 13,
        }}
      >
        <span style={{ color: OPS.gold, fontFamily: OPS.display, fontSize: 17, letterSpacing: '.08em' }}>スタンス</span>
        <span style={{ color: '#172030', background: OPS.gold, borderRadius: 7, padding: '5px 13px', fontFamily: OPS.display, fontSize: 17, fontWeight: 700 }}>{stanceLabel ?? '—'}</span>
        <span style={{ color: OPS.sub, fontSize: 12.5 }}>{operational?.reason ?? '方向性と安全条件を継続確認します。'}</span>
      </div>

      <div className="today-brief-metrics">
        <HeroMetric label="市場レジーム" value={data.command.scenario ?? '—'} color={data.command.scenario === 'BULL' ? OPS.green : OPS.gold} />
        <HeroMetric label="リスク環境" value={guardOk ? '安定' : '注意'} color={guardOk ? OPS.green : OPS.vermilion} />
        <HeroMetric label="センチメント" value={sentiment} color={OPS.gold} />
        <HeroMetric label="ボラティリティ" value={volatility} color={data.command.vix != null && data.command.vix > 28 ? OPS.vermilion : OPS.green} />
        <HeroMetric label="確信度（全体）" value={confidence == null ? '—' : `${confidence.toFixed(0)}%`} color={OPS.blue} />
      </div>
    </header>
  )
}

function HeroMetric({ label, value, color }: { label: string; value: string; color: string }) {
  return <div className="today-brief-metric"><div style={{ color: OPS.sub, fontSize: 10.5 }}>{label}</div><div style={{ color, fontFamily: OPS.display, fontSize: 16, marginTop: 4 }}>{value}</div></div>
}

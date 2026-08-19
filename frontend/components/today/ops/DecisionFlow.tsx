'use client'

import { useMemo, useState } from 'react'
import { OPS } from './tokens'
import { SelectionPulse } from './motionAccents'
import {
  STAGE_LABELS, buildDecisionRows, buildRebuttals, type DecisionRow, type StopKind,
} from './decisionRows'
import type { DecisionFlow as DecisionFlowData, Engine } from './types'

/**
 * DECISION FLOW — 提案1件 = 1行の表。
 *
 * これまでの5案 (Sankey・リボン・横棒・力学グラフ・固定配置グラフ) は
 * どれも「15件が2件に絞られる工程」を描こうとしていた。しかし画面の
 * 大半を占めるのは毎日変わらないパイプラインで、知りたいのは
 * 「今日の候補はどこまで進んだか」「他に何を検討して、なぜ落ちたか」の2つ。
 * 実データで確認すると、不採用候補も対案もすべて「評価された提案」で、
 * 違うのは止まった場所だけだった (2026-08-19)。工程は列見出しとして
 * 1回だけ描き、各提案は1行・どこまで進んだかを塗りで示す形に統合する。
 */

function stopTone(kind: StopKind): string {
  if (kind === 'pass') return OPS.green
  if (kind === 'review') return OPS.amber
  if (kind === 'defer') return OPS.blue
  return OPS.vermilion
}

const GLYPH: Record<StopKind, string> = { pass: '✓', review: '!', defer: '‖', reject: '✕' }

function DecisionRowView({
  row, active, isOpen, onToggle,
}: {
  row: DecisionRow
  active: boolean
  isOpen: boolean
  onToggle: () => void
}) {
  const tone = stopTone(row.stop.kind)
  return (
    <button type="button" className="df-row" data-active={active} data-kind={row.kind}
      aria-expanded={isOpen} onClick={onToggle}>
      {row.kind === 'candidate' && <SelectionPulse active={active} color="#4FD0F5" />}
      <span className="df-row-ticker">
        <b>{row.ticker}</b>
        {row.subtitle && <span>{row.subtitle}</span>}
      </span>

      {STAGE_LABELS.map((label, gi) => {
        const reachable = gi < row.stop.depth
        const passed = reachable && gi < row.stop.gate
        const here = reachable && gi === row.stop.gate
        const color = passed ? '#4FD0F5' : here ? tone : OPS.hairline
        return (
          <span className="df-gate" key={label} aria-hidden="true">
            <i className="df-dot" style={{ borderColor: reachable ? color : OPS.hairline, color }}>
              {passed ? '✓' : here ? GLYPH[row.stop.kind] : reachable ? '·' : ''}
            </i>
            {gi < STAGE_LABELS.length - 1 && (
              <i className="df-rail" style={{
                background: passed ? '#4FD0F5' : OPS.hairline,
                opacity: gi + 1 < row.stop.depth ? 1 : 0.35,
              }} />
            )}
          </span>
        )
      })}

      <span className="df-outcome" style={{ color: tone }}>{row.outcomeLabel}</span>
      <span className="df-chevron" aria-hidden="true">{isOpen ? '▾' : '▸'}</span>

      {row.headline && !isOpen && <span className="df-note">{row.headline}</span>}
      {isOpen && (
        <div className="df-detail">
          {row.detail.split('\n').filter(Boolean).map((line, i) => <p key={i}>{line}</p>)}
        </div>
      )}
    </button>
  )
}

export default function DecisionFlow({
  flow, engine, selectedKey, onSelect,
}: {
  flow?: DecisionFlowData
  /** Red Team の対案と判定。見送り一覧に統合する。 */
  engine?: Engine
  selectedKey: string | null
  onSelect: (key: string) => void
}) {
  const actions = useMemo(() => flow?.actions ?? [], [flow])
  const unselected = useMemo(() => flow?.unselected ?? [], [flow])
  const rebuttals = useMemo(
    () => buildRebuttals(engine?.attacks, engine?.red_team), [engine])
  const rows = useMemo(
    () => buildDecisionRows(actions, unselected, rebuttals, engine?.lanes),
    [actions, unselected, rebuttals, engine?.lanes])

  // 展開のON/OFFは選択(selectedKey)と独立させる。選択は他パネルとの連動用で
  // 外から動くことがあり、そこへ連動させると「自分でクリックして閉じても
  // 選択が残っている限り開いたまま」というトグルが効かない不具合になる。
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())
  const toggle = (row: DecisionRow) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (!next.delete(row.id)) next.add(row.id)
      return next
    })
    if (row.actionKey) onSelect(row.actionKey)
  }

  if (!flow || flow.status === 'unavailable' || (!rows.today.length && !rows.considered.length)) {
    return <section className="ops-elev" aria-label="判断フロー" style={{ borderRadius: 10, padding: '14px 16px' }}>
      <strong className="ops-latin" style={{ color: OPS.gold, fontSize: 13 }}>DECISION FLOW</strong>
      <p style={{ color: OPS.dim, margin: '7px 0 0', fontSize: 13 }}>
        {flow?.status === 'unavailable' ? '今回の分析経路を取得できません。' : '今回の分析で追跡できる候補がありません。'}
      </p>
    </section>
  }

  const mismatch = flow.integrity.status === 'mismatch'
  const readyCount = rows.today.filter(r => r.stop.kind === 'pass').length

  return <section className="decision-flow ops-elev" aria-label="判断フロー">
    <style dangerouslySetInnerHTML={{ __html: `
      .decision-flow { border-radius:10px; padding:9px 13px 10px; background:${OPS.panel}; }
      .df-title { display:flex; align-items:baseline; gap:9px; flex-wrap:wrap; margin-bottom:8px; }
      .df-headline { color:${OPS.sub}; font-family:${OPS.sans}; font-size:11.5px; }
      .df-headline b { color:${OPS.text}; font-weight:600; }

      .df-cols { display:grid; grid-template-columns:minmax(150px,1fr) 52px 52px 52px 74px 14px; gap:6px;
        align-items:center; padding:0 10px 4px; }
      .df-cols span { color:${OPS.dim}; font-family:${OPS.brand}; font-size:8.5px; letter-spacing:.08em;
        text-align:center; }
      .df-cols span:first-child { text-align:left; }

      .df-section-label { display:flex; align-items:baseline; gap:6px; color:${OPS.gold};
        font-family:${OPS.brand}; font-size:9px; letter-spacing:1.2px; margin:8px 0 3px; padding:0 10px; }
      .df-section-label span { color:${OPS.dim}; font-family:${OPS.sans}; font-size:9px; letter-spacing:0; }

      .df-rows { display:flex; flex-direction:column; gap:2px; }
      .df-rows.is-scroll { max-height:340px; overflow-y:auto; }

      .df-row { position:relative; display:grid; grid-template-columns:minmax(150px,1fr) 52px 52px 52px 74px 14px;
        gap:6px; align-items:center; width:100%; padding:6px 10px; border-radius:6px;
        border:1px solid transparent; background:transparent; color:inherit; text-align:left; cursor:pointer; }
      .df-row:hover { background:${OPS.panelAlt}; }
      .df-row[data-active="true"] { border-color:#4FD0F5; background:rgba(79,208,245,.06); }
      .df-row[data-kind="candidate"] .df-row-ticker b { color:${OPS.text}; }
      .df-row[data-kind="dropped"] .df-row-ticker b,
      .df-row[data-kind="rebuttal"] .df-row-ticker b,
      .df-row[data-kind="lane"] .df-row-ticker b { color:${OPS.sub}; }

      .df-row-ticker { display:flex; flex-direction:column; gap:1px; overflow:hidden; min-width:0; }
      .df-row-ticker b { font-family:${OPS.mono}; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .df-row-ticker span { color:${OPS.dim}; font-family:${OPS.sans}; font-size:9.5px;
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

      .df-gate { display:flex; align-items:center; justify-content:center; }
      .df-dot { flex:none; width:14px; height:14px; display:grid; place-items:center; border-radius:50%;
        border:1px solid ${OPS.hairline}; background:${OPS.bg}; font-family:${OPS.mono}; font-size:8px; }
      .df-rail { flex:1; height:1.5px; min-width:5px; margin:0 2px; border-radius:1px; }

      .df-outcome { font-family:${OPS.brand}; font-size:10px; letter-spacing:.04em; text-align:right;
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .df-chevron { color:${OPS.dim}; font-size:9px; text-align:center; }

      .df-note { grid-column:1 / -1; color:${OPS.sub}; font-size:10.5px; line-height:1.4; margin-top:3px; }
      .df-detail { grid-column:1 / -1; margin-top:6px; padding-top:6px; border-top:1px solid ${OPS.hairline}; }
      .df-detail p { color:${OPS.sub}; font-size:11px; line-height:1.55; margin:0 0 3px; }
      .df-detail p:last-child { margin-bottom:0; }
    ` }} />

    <div className="df-title">
      <strong className="ops-latin" style={{ color: OPS.gold, fontSize: 12.5 }}>DECISION FLOW</strong>
      <span style={{ fontFamily: OPS.display, color: OPS.sub, fontSize: 11.5, letterSpacing: '.06em' }}>提案がどこで止まったか</span>
      <span className="df-headline">
        候補 <b>{rows.today.length}</b> 件 → 発注可能 <b>{readyCount}</b> 件 ・ 検討して見送り <b>{rows.considered.length}</b> 件
      </span>
      {mismatch && (
        <span style={{ marginLeft: 'auto', color: OPS.amber, fontFamily: OPS.mono, fontSize: 9.5 }}>
          ⚠ board / review_board を正本として表示
        </span>
      )}
    </div>

    <div className="df-cols" aria-hidden="true">
      <span />
      {STAGE_LABELS.map(l => <span key={l}>{l}</span>)}
      <span>結末</span>
      <span />
    </div>

    {rows.today.length > 0 && (
      <>
        <div className="df-section-label">今日の候補</div>
        <div className="df-rows">
          {rows.today.map(row => (
            <DecisionRowView key={row.id} row={row}
              active={!!row.actionKey && row.actionKey === selectedKey}
              isOpen={expanded.has(row.id)}
              onToggle={() => toggle(row)} />
          ))}
        </div>
      </>
    )}

    {rows.considered.length > 0 && (
      <>
        <div className="df-section-label">検討して見送ったもの<span>{rows.considered.length}件</span></div>
        <div className={`df-rows${rows.considered.length > 8 ? ' is-scroll' : ''}`}>
          {rows.considered.map(row => (
            <DecisionRowView key={row.id} row={row}
              active={false}
              isOpen={expanded.has(row.id)}
              onToggle={() => toggle(row)} />
          ))}
        </div>
      </>
    )}
  </section>
}

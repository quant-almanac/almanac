'use client'

import dynamic from 'next/dynamic'
import { useCallback, useMemo, useState } from 'react'
import { OPS } from './tokens'
import { SelectionPulse } from './motionAccents'
import {
  biggestDrop, buildFunnelModel, buildUnitLanes, canRenderAsCells, type DropKind,
} from './decisionFunnel'
import { buildDecisionGraph, buildRebuttals, type GraphNodeKind } from './decisionGraph'
import type { DecisionFlow as DecisionFlowData, DecisionFlowAction, Engine } from './types'
import type { EChartsOption } from 'echarts-for-react'

const ReactECharts = dynamic(() => import('echarts-for-react'), { ssr: false })

/**
 * DECISION FLOW — 「候補が何件、どこで落ちたか」と「この銘柄がどこで止まったか」は
 * 別の問いなので、別の表示にする。
 *
 *   MAP    … Obsidianのグラフビュー相当。候補・ゲート・結末のつながりを見る
 *   FUNNEL … 段ごとのマス。件数がどこで落ちたかを数える
 *   TRACKS … 銘柄ごとの停止点と理由
 *
 * MAPは件数比を太さで表さないので、15→2 のような極端な比でも後段が潰れない
 * (Sankey・リボン・横棒はいずれもここで失敗した)。
 */

const NODE_TONE: Record<GraphNodeKind, string> = {
  source: OPS.dim,
  stage: OPS.gold,
  candidate: '#4FD0F5',
  gate: OPS.blue,
  outcome: OPS.green,
  dropped: OPS.dim,
  rebuttal: OPS.sub,
  rebuttal_group: OPS.sub,
}

/** 結末は種別ごとに色を変える。要確認を緑にすると成功に見えて嘘になる。 */
const OUTCOME_TONE: Record<string, string> = {
  ready: OPS.green,
  review: OPS.amber,
  filtered: OPS.vermilion,
  deferred: OPS.blue,
  closed: OPS.dim,
}

/** ラベルをノードの下に出す種別。格子状に並ぶもの＝横に隣がいるもの。 */
const LABEL_BELOW: GraphNodeKind[] = ['dropped', 'rebuttal', 'rebuttal_group', 'outcome']

const NODE_SIZE: Record<GraphNodeKind, number> = {
  source: 13, stage: 30, candidate: 22, gate: 20, outcome: 24, dropped: 11,
  rebuttal_group: 19, rebuttal: 12,
}

const GATES = [
  { key: 'red', label: 'RED TEAM', jp: '対案検証' },
  { key: 'safety', label: 'SAFETY GATE', jp: '安全ゲート' },
  { key: 'budget', label: 'BUDGET GATE', jp: '執行可否' },
]

type Stop = { gate: number; kind: 'pass' | 'reject' | 'review' | 'defer' }

/**
 * アクションがどのゲートまで進んだか。
 * 位置の正本は decision_status（board / review_board 由来）。
 * stage_states はそれ以前のゲートでの停止を特定するためだけに使う。
 */
function laneStop(a: DecisionFlowAction): Stop {
  const s = a.stage_states ?? {}
  if (s.policy_rejected) return { gate: 0, kind: 'reject' }
  if (s.post_filter_rejected) return { gate: 1, kind: 'reject' }
  if (s.post_filter_deferred) return { gate: 1, kind: 'defer' }
  if (a.decision_status === 'review') return { gate: 2, kind: 'review' }
  if (a.decision_status === 'filtered') return { gate: 2, kind: 'reject' }
  if (a.decision_status === 'deferred') return { gate: 2, kind: 'defer' }
  if (a.decision_status === 'ready') return { gate: 3, kind: 'pass' }
  // closed 等（取消・期限切れ）は執行段まで進んだが生きていない。
  return { gate: 3, kind: 'defer' }
}

function stopTone(kind: Stop['kind']): string {
  if (kind === 'pass') return OPS.green
  if (kind === 'review') return OPS.amber
  if (kind === 'defer') return OPS.blue
  return OPS.vermilion
}

const GLYPH: Record<Stop['kind'], string> = { pass: '✓', review: '!', defer: '‖', reject: '✕' }

const DROP_TONE: Record<DropKind, string> = {
  unselected: OPS.dim,
  rejected: OPS.vermilion,
  review: OPS.amber,
  deferred: OPS.blue,
}

function actionLabel(a: DecisionFlowAction): string {
  if (a.execution_status === 'ordered') return 'ORDERED'
  if (a.execution_status === 'filled') return 'FILLED'
  if (a.execution_status === 'executed') return 'EXECUTED'
  if (a.execution_status === 'cancelled') return 'CANCELLED'
  if (a.execution_status === 'expired') return 'EXPIRED'
  if (a.execution_status === 'reprice_required') return 'REPRICE'
  if (a.decision_status === 'ready') return 'APPROVED'
  if (a.decision_status === 'deferred') return 'DEFERRED'
  if (a.decision_status === 'filtered') return 'FILTERED'
  return 'REVIEW'
}

function statusLabel(a: DecisionFlowAction): string {
  if (a.execution_status === 'ordered') return '指値中'
  if (a.execution_status === 'filled') return '約定'
  if (a.execution_status === 'executed') return '実行済み'
  if (a.execution_status === 'cancelled') return '取消・終了'
  if (a.execution_status === 'expired') return '期限切れ'
  if (a.execution_status === 'reprice_required') return '再評価待ち'
  if (a.decision_status === 'ready') return '発注可能'
  if (a.decision_status === 'deferred') return '保留'
  if (a.decision_status === 'filtered') return '除外'
  return '要確認'
}

function actionTypeLabel(type?: string): string {
  return ({ buy: '買い', add: '買い増し', trim: '部分利確', sell: '売り', hold: '保持', hedge: 'ヘッジ' } as Record<string, string>)[type ?? ''] ?? type ?? ''
}


/**
 * 正規化座標(0〜1)を実ピクセルへ直すだけ。どこに何を置くかは
 * decisionGraph.ts が決めている（純粋関数なのでテストできる）。
 */
function toPixels(
  n: { nx?: number; ny?: number }, box: { w: number; h: number },
): { x: number; y: number } | null {
  if (n.nx == null || n.ny == null) return null
  const padX = 30, padY = 18
  return {
    x: padX + n.nx * Math.max(1, box.w - padX * 2),
    y: padY + n.ny * Math.max(1, box.h - padY * 2),
  }
}

/** Obsidian のグラフビュー相当。force レイアウトで自然に散らし、hoverで経路だけ光らせる。 */
function buildGraphOption(
  graph: ReturnType<typeof buildDecisionGraph>,
  selectedKey: string | null,
  box: { w: number; h: number },
): EChartsOption {
  return {
    animation: true,
    animationDuration: 600,
    tooltip: {
      backgroundColor: OPS.panelAlt,
      borderColor: OPS.hairline,
      textStyle: { color: OPS.text, fontFamily: OPS.mono, fontSize: 10.5 },
      confine: true,
      extraCssText: 'max-width:290px;white-space:normal;line-height:1.55;',
      formatter: (p: { dataType?: string; data?: Record<string, unknown> }) => {
        const d = (p.data ?? {}) as
          { name?: string; value?: number; kind?: string; detail?: string }
        if (p.dataType === 'edge') return ''
        const head = d.value ? `${d.name}　<b>${d.value} 件</b>` : String(d.name ?? '')
        if (!d.detail) return head
        // 対案は提案と判定が別の主張なので、行を分けたまま出す
        const body = d.detail.split('\n')
          .map(line => `<span style="color:${OPS.sub}">${line}</span>`).join('<br/>')
        return `${head}<br/>${body}`
      },
    },
    series: [{
      type: 'graph',
      // force は使わない。13件の不採用を力学に任せると AI合成 の周りに
      // 放射状に散り、入力ノードと混ざって左→右の流れが消えた。
      // 掴んで動かせる・hoverで関係だけ光る手触りは roam/draggable で残る。
      layout: 'none',
      roam: true,
      draggable: true,
      emphasis: { focus: 'adjacency', scale: 1.12, lineStyle: { width: 2.4, opacity: 0.95 } },
      edgeSymbol: ['none', 'arrow'],
      edgeSymbolSize: [0, 5],
      lineStyle: { color: OPS.hairline, width: 1.1, opacity: 0.5, curveness: 0.06 },
      label: {
        show: true, position: 'right', distance: 5,
        color: OPS.sub, fontFamily: OPS.sans, fontSize: 10,
      },
      data: graph.nodes.map(n => {
        const active = !!n.actionKey && n.actionKey === selectedKey
        const tone = n.outcome ? (OUTCOME_TONE[n.outcome] ?? OPS.sub) : NODE_TONE[n.kind]
        const at = toPixels(n, box)
        return {
          id: n.id,
          name: n.count ? `${n.label} ${n.count}` : n.label,
          kind: n.kind,
          value: n.count,
          detail: n.detail,
          ...(at ? { x: at.x, y: at.y } : {}),
          symbolSize: NODE_SIZE[n.kind] * (active ? 1.3 : 1),
          symbol: n.kind === 'rebuttal_group' && n.expandable === 'closed' ? 'roundRect' : 'circle',
          itemStyle: {
            color: tone,
            // 対案は枝葉なので背骨より薄く。開いた束は中空にして「開いている」を示す
            opacity: n.kind === 'source' ? 0.55
              : n.kind === 'rebuttal' || n.kind === 'dropped' ? 0.62
              : n.expandable === 'open' ? 0.25 : 0.9,
            borderColor: active ? OPS.gold
              : n.expandable === 'open' ? tone : 'transparent',
            borderWidth: active ? 2.5 : n.expandable === 'open' ? 1.6 : 0,
          },
          label: {
            color: active ? OPS.gold
              : n.kind === 'source' || n.kind === 'rebuttal' || n.kind === 'dropped'
                ? OPS.dim : OPS.sub,
            fontWeight: active || n.kind === 'stage' ? 600 : 400,
            fontSize: n.kind === 'rebuttal' || n.kind === 'dropped' ? 9 : 10,
            // 格子に並ぶものと右端の結末はラベルを下に出す。
            // 右出しだと隣のノードにラベルが刺さる（GS_MMF_USD↔XLF Long,
            // 執行可否↔要確認 が実際に潰れた）。下出しなら横では絶対にぶつからない。
            position: LABEL_BELOW.includes(n.kind) ? 'bottom' : 'right',
            distance: LABEL_BELOW.includes(n.kind) ? 7 : 5,
          },
        }
      }),
      links: graph.links.map(l => ({
        source: l.source,
        target: l.target,
        lineStyle: {
          color: l.live ? '#4FD0F5' : OPS.hairline,
          opacity: l.live ? 0.45 : 0.2,
          width: l.count && l.count > 3 ? 2.2 : 1.1,
        },
      })),
    }],
  } as EChartsOption
}

export default function DecisionFlow({
  flow, engine, selectedKey, onSelect,
}: {
  flow?: DecisionFlowData
  /** Red Team の対案と判定。MAPの枝として出す。 */
  engine?: Engine
  selectedKey: string | null
  onSelect: (key: string) => void
}) {
  const actions = useMemo(() => flow?.actions ?? [], [flow])
  const model = useMemo(() => buildFunnelModel(flow?.stages), [flow])
  const lanes = useMemo(() => buildUnitLanes(model), [model])
  const asCells = useMemo(() => canRenderAsCells(model), [model])
  const drop = useMemo(() => biggestDrop(model), [model])
  const stops = useMemo(() => actions.map(laneStop), [actions])
  const [view, setView] = useState<'map' | 'funnel'>('map')
  // 不採用候補は action_stage_log 由来の個票がAPIに乗っているので、そのまま1件1ノードにする
  const unselected = useMemo(() => flow?.unselected ?? [], [flow])
  const rebuttals = useMemo(
    () => buildRebuttals(engine?.attacks, engine?.red_team), [engine])
  // 対案は畳んだ状態が既定。常時展開すると背骨が読めなくなる
  const [openGroups, setOpenGroups] = useState<ReadonlySet<string>>(new Set())
  // 座標はピクセルに落とすので、実寸を測ってから配る。
  // 格子の列数も実幅から決まるため、これはグラフを組む前に要る。
  const [box, setBox] = useState({ w: 1200, h: 430 })
  const graphBoxRef = useCallback((el: HTMLDivElement | null) => {
    if (!el) return
    const read = () => setBox({ w: el.clientWidth || 1200, h: el.clientHeight || 430 })
    read()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(read)
    ro.observe(el)
  }, [])
  const graph = useMemo(
    () => buildDecisionGraph(actions, unselected, rebuttals, openGroups),
    [actions, unselected, rebuttals, openGroups])
  const graphOption = useMemo(
    () => buildGraphOption(graph, selectedKey, box), [graph, selectedKey, box])

  const onGraphClick = useCallback((p: { data?: { id?: string; kind?: string } }) => {
    const d = p?.data
    if (!d?.id) return
    if (d.kind === 'rebuttal_group') {
      const key = d.id.replace('rebutgrp:', '')
      setOpenGroups(prev => {
        const next = new Set(prev)
        if (!next.delete(key)) next.add(key)
        return next
      })
      return
    }
    if (d.kind === 'candidate') {
      const node = graph.nodes.find(n => n.id === d.id)
      if (node?.actionKey) onSelect(node.actionKey)
    }
  }, [graph, onSelect])

  if (!flow || flow.status === 'unavailable' || (!model.steps.length && !actions.length)) {
    return <section className="ops-elev" aria-label="判断フロー" style={{ borderRadius: 10, padding: '14px 16px' }}>
      <strong className="ops-latin" style={{ color: OPS.gold, fontSize: 13 }}>DECISION FLOW</strong>
      <p style={{ color: OPS.dim, margin: '7px 0 0', fontSize: 13 }}>
        {flow?.status === 'unavailable' ? '今回の分析経路を取得できません。' : '今回の分析で追跡できる候補がありません。'}
      </p>
    </section>
  }

  const mismatch = flow.integrity.status === 'mismatch'
  const started = model.steps[0]?.entered ?? 0
  const survived = model.steps[model.steps.length - 1]?.passed ?? 0

  return <section className="decision-flow ops-elev" aria-label="判断フロー">
    <style dangerouslySetInnerHTML={{ __html: `
      .decision-flow { border-radius:10px; padding:9px 13px 10px; background:${OPS.panel}; }
      .df-title { display:flex; align-items:baseline; gap:9px; flex-wrap:wrap; margin-bottom:6px; }
      .df-headline { color:${OPS.sub}; font-family:${OPS.sans}; font-size:11.5px; }
      .df-headline b { color:${OPS.amber}; font-weight:600; }

      .df-split { display:grid; grid-template-columns:minmax(0,1.05fr) minmax(300px,.95fr); gap:16px; align-items:start; }
      .df-panel-cap { display:flex; align-items:baseline; gap:8px; color:${OPS.gold};
        font-family:${OPS.brand}; font-size:9px; letter-spacing:1.5px; margin-bottom:6px; }
      .df-panel-cap span { color:${OPS.dim}; font-family:${OPS.sans}; font-size:9px; letter-spacing:0; }

      /* ── ファネル ── */
      .df-funnel { display:flex; flex-direction:column; gap:5px; }
      .df-step-row { display:grid; grid-template-columns:64px minmax(0,1fr); align-items:center;
        column-gap:8px; row-gap:2px; }
      .df-step-name { color:${OPS.sub}; font-family:${OPS.sans}; font-size:10.5px; text-align:right; }
      .df-step-bar { position:relative; height:19px; min-width:34px; border-radius:3px;
        background:${OPS.sunken}; box-shadow:inset 0 0 0 1px ${OPS.hairline}; overflow:hidden; }
      .df-step-fill { position:absolute; inset:0 auto 0 0; background:#4FD0F5; opacity:.3; }
      .df-step-count { position:absolute; left:7px; top:50%; transform:translateY(-50%);
        color:${OPS.text}; font-family:${OPS.mono}; font-size:11px; font-weight:600; }
      .df-step-drops { grid-column:2; display:flex; flex-wrap:wrap; gap:2px 10px; padding-left:1px; }
      .df-step-drop { font-family:${OPS.mono}; font-size:9.5px; font-style:normal; }
      .df-step-allpass { color:${OPS.dim}; font-family:${OPS.mono}; font-size:9.5px; font-style:normal; }

      .df-funnel-total { margin-top:7px; padding-top:6px; border-top:1px solid ${OPS.hairline};
        color:${OPS.dim}; font-family:${OPS.mono}; font-size:9.5px; }
      .df-funnel-total b { color:${OPS.text}; }

      /* ── 表示切替 ── */
      .df-viewbar { display:flex; align-items:center; gap:5px; margin-bottom:7px; flex-wrap:wrap; }
      .df-viewtab { border:1px solid ${OPS.hairline}; border-radius:999px; background:transparent;
        color:${OPS.dim}; padding:2px 10px; font:9.5px ${OPS.brand}; letter-spacing:1.2px; cursor:pointer; }
      .df-viewtab[aria-pressed="true"] { color:${OPS.gold}; border-color:${OPS.gold}88; background:${OPS.goldBg}; }
      .df-viewhint { color:${OPS.dim}; font-family:${OPS.sans}; font-size:9px; margin-left:2px; }

      /* ── 判断マップ ── */
      .df-graph { height:430px; border-radius:8px; background:${OPS.sunken};
        box-shadow:inset 0 0 0 1px ${OPS.hairline}; overflow:hidden; }

      /* ── 段ごとのマス ── */
      .df-lanes { display:flex; flex-direction:column; gap:3px; }
      .df-lane-row { display:grid; grid-template-columns:66px minmax(0,1fr); column-gap:9px; row-gap:1px;
        align-items:center; padding:3px 0; border-radius:4px; }
      /* 生き残りが降りていく列を目で追えるよう、左端を揃えた薄い導線を敷く */
      .df-lane-row + .df-lane-row { box-shadow:inset 0 1px 0 ${OPS.hairline}44; }
      .df-lane-name { color:${OPS.sub}; font-family:${OPS.sans}; font-size:10.5px; text-align:right;
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .df-lane-cells { display:flex; flex-wrap:wrap; gap:2.5px; align-items:center; min-height:13px; }
      .df-cell { width:11px; height:11px; border-radius:2.5px; flex:none; }
      .df-lane-bar { position:relative; display:block; height:11px; min-width:20px; border-radius:3px;
        background:${OPS.sunken}; box-shadow:inset 0 0 0 1px ${OPS.hairline}; overflow:hidden; }
      .df-lane-bar b { position:absolute; inset:0 auto 0 0; background:#4FD0F5; opacity:.55; }
      .df-lane-note { grid-column:2; display:flex; flex-wrap:wrap; gap:1px 10px; }
      .df-lane-note em { font-family:${OPS.mono}; font-size:9.5px; font-style:normal; }

      /* ── 銘柄ごとの経路 ── */
      .df-tracks { display:flex; flex-direction:column; gap:6px; }
      .df-track { position:relative; width:100%; border:1px solid ${OPS.hairline}; border-radius:7px;
        padding:8px 10px; background:${OPS.panelAlt}; color:inherit; text-align:left; cursor:pointer; }
      .df-track[data-active="true"] { border-color:#4FD0F5; background:rgba(79,208,245,.07); }
      .df-track-head { display:flex; align-items:baseline; gap:8px; padding-bottom:5px; border-bottom:1px solid ${OPS.hairline}; }
      .df-steps { display:flex; align-items:center; gap:0; margin-top:7px; }
      .df-step { display:flex; align-items:center; flex:1; min-width:0; }
      .df-step:last-child { flex:0 0 auto; }
      .df-dot { flex:none; width:15px; height:15px; display:grid; place-items:center; border-radius:50%;
        border:1px solid ${OPS.hairline}; background:${OPS.bg}; font-family:${OPS.mono}; font-size:8.5px; }
      .df-rail { flex:1; height:1.5px; min-width:8px; border-radius:1px; }
      .df-step-labels { display:flex; margin-top:4px; }
      .df-step-labels span { flex:1; min-width:0; color:${OPS.dim}; font-family:${OPS.sans}; font-size:8px;
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .df-step-labels span:last-child { flex:0 0 auto; text-align:right; }
      .df-reason { color:${OPS.sub}; font-size:10.5px; line-height:1.45; margin-top:6px; }

      @media (max-width:900px) { .df-split { grid-template-columns:1fr; } }
    ` }} />

    <div className="df-title">
      <strong className="ops-latin" style={{ color: OPS.gold, fontSize: 12.5 }}>DECISION FLOW</strong>
      <span style={{ fontFamily: OPS.display, color: OPS.sub, fontSize: 11.5, letterSpacing: '.06em' }}>候補がどこで落ちたか</span>
      {drop && (
        <span className="df-headline">
          最大の脱落: <b>{drop.stage}</b> で <b>{drop.value}件</b> が {drop.reason}
        </span>
      )}
      {mismatch && (
        <span style={{ marginLeft: 'auto', color: OPS.amber, fontFamily: OPS.mono, fontSize: 9.5 }}>
          ⚠ board / review_board を正本として表示
        </span>
      )}
    </div>

    <div className="df-split">
      <div>
        <div className="df-viewbar">
          <button type="button" className="df-viewtab" aria-pressed={view === 'map'}
            onClick={() => setView('map')}>MAP</button>
          <button type="button" className="df-viewtab" aria-pressed={view === 'funnel'}
            onClick={() => setView('funnel')}>FUNNEL</button>
          <span className="df-viewhint">
            {view === 'map'
              ? 'ノードをドラッグ・ホイールで拡大。対案の束はクリックで開く'
              : '1マス = 候補1件。生き残りは左詰めで下の段へ降りる'}
          </span>
        </div>

        {view === 'map' && (
          <div className="df-graph" role="img" ref={graphBoxRef}
            aria-label={`候補 ${started} 件の判断経路マップ。${survived} 件が発注可能`}>
            <ReactECharts option={graphOption} notMerge
              style={{ height: '100%', width: '100%' }} opts={{ renderer: 'svg' }}
              onEvents={{ click: onGraphClick }} />
          </div>
        )}

        {view === 'funnel' && (

        <div className="df-lanes" role="img"
          aria-label={`候補 ${started} 件が各ゲートで脱落し ${survived} 件が残った`}>
          {lanes.map(lane => (
            <div className="df-lane-row" key={lane.key}>
              <span className="df-lane-name">{lane.label}</span>

              <span className="df-lane-cells">
                {asCells
                  ? lane.units.map((state, index) => (
                      <i key={index} className="df-cell"
                        style={{
                          background: state === 'survive' ? '#4FD0F5' : DROP_TONE[state],
                          opacity: state === 'survive' ? 0.92 : 0.34,
                        }} />
                    ))
                  : (
                      // 件数が多すぎるとマスが潰れるので比率バーへ退避する
                      <i className="df-lane-bar" style={{ width: `${(lane.entered / model.scale) * 100}%` }}>
                        <b style={{ width: `${lane.entered > 0 ? (lane.passed / lane.entered) * 100 : 0}%` }} />
                      </i>
                    )}
              </span>

              <span className="df-lane-note">
                {lane.lost === 0
                  ? <em style={{ color: OPS.dim }}>{lane.entered}件すべて通過</em>
                  : lane.drops.map(d => (
                      <em key={d.kind} style={{ color: DROP_TONE[d.kind] }}>−{d.value} {d.label}</em>
                    ))}
              </span>
            </div>
          ))}
        </div>

        )}

        <div className="df-funnel-total">
          候補 <b>{started}</b> 件 → 発注可能 <b>{survived}</b> 件
        </div>
      </div>

      <div>
        <span className="df-panel-cap">TRACKS<span>銘柄ごとの停止点</span></span>
        <div className="df-tracks">
          {actions.length === 0 && (
            <p style={{ color: OPS.dim, fontSize: 11.5, margin: 0 }}>個別に追跡できる候補はありません。</p>
          )}
          {actions.map((action, index) => {
            const stop = stops[index]
            const tone = stopTone(stop.kind)
            const active = action.key === selectedKey
            const reason = action.reasons?.[0]?.message ?? action.reason_codes?.join(' · ') ?? ''
            return (
              <button key={action.key} type="button" className="df-track" data-active={active}
                onClick={() => onSelect(action.key)}
                aria-label={`${action.ticker ?? '候補'} ${actionLabel(action)}`}>
                <SelectionPulse active={active} color="#4FD0F5" />
                <span className="df-track-head">
                  <strong style={{ color: OPS.text, fontFamily: OPS.mono, fontSize: 12.5 }}>{action.ticker ?? '—'}</strong>
                  <span style={{ color: OPS.dim, fontSize: 10 }}>{actionTypeLabel(action.type)}</span>
                  <b style={{ marginLeft: 'auto', color: tone, fontFamily: OPS.brand, fontSize: 10, letterSpacing: '.08em' }}>
                    {statusLabel(action)}
                  </b>
                </span>

                <span className="df-steps" aria-hidden="true">
                  <span className="df-step">
                    <i className="df-dot" style={{ borderColor: '#4FD0F5', color: '#4FD0F5' }}>✓</i>
                    <i className="df-rail" style={{ background: stop.gate >= 1 ? '#4FD0F5' : OPS.hairline }} />
                  </span>
                  {GATES.map((gate, gi) => {
                    const passed = gi < stop.gate
                    const here = gi === stop.gate
                    const color = passed ? '#4FD0F5' : here ? tone : OPS.hairline
                    return (
                      <span className="df-step" key={gate.key}>
                        <i className="df-dot" style={{ borderColor: color, color }}>
                          {passed ? '✓' : here ? GLYPH[stop.kind] : '·'}
                        </i>
                        {gi < GATES.length - 1 && (
                          <i className="df-rail" style={{ background: passed ? '#4FD0F5' : OPS.hairline }} />
                        )}
                      </span>
                    )
                  })}
                </span>
                <span className="df-step-labels" aria-hidden="true">
                  <span>候補</span>
                  {GATES.map(g => <span key={g.key}>{g.jp}</span>)}
                </span>

                {reason && <span className="df-reason">{reason}</span>}
              </button>
            )
          })}
        </div>
      </div>
    </div>
  </section>
}

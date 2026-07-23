'use client'

import { useState, useEffect, Suspense } from 'react'
import { useSearchParams } from 'next/navigation'
import useSWR from 'swr'
import { fetcher, apiFetch, type DecisionLog } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { Chip, OpsPage, Panel, PanelTitle } from '@/components/today/ops/PageKit'

const DECISION_CSS = `
.decision-case-grid { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:7px; }
.decision-workspace { display:grid; grid-template-columns:minmax(300px,.72fr) minmax(0,1.55fr); gap:16px; align-items:start; }
.decision-field-pair { display:grid; grid-template-columns:minmax(0,.7fr) minmax(0,1.3fr); gap:10px; }
@container ops-content (max-width: 900px) {
  .decision-case-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .decision-workspace { grid-template-columns:1fr; }
}
@container ops-content (max-width: 520px) {
  .decision-case-grid { grid-template-columns:repeat(5,180px); overflow-x:auto; padding-bottom:4px; }
  .decision-field-pair { grid-template-columns:1fr; }
}
`

const CASE_LABELS: Record<string, { label: string; color: string; desc: string }> = {
  A: { label: 'ケースA: 短期シグナル', color: OPS.gold, desc: '買いシグナルの最終判断' },
  B: { label: 'ケースB: 長期買い増し', color: OPS.green, desc: '長期銘柄の追加購入判断' },
  C: { label: 'ケースC: 持株会', color: OPS.amber, desc: '持株会の売却/継続判断' },
  D: { label: 'ケースD: クレカ積立売却', color: OPS.blue, desc: '積立ファンドの売却タイミング' },
  E: { label: 'ケースE: リバランス実行', color: OPS.orchid, desc: 'リバランス実行の最終確認' },
}

function DecisionPageInner() {
  const { data: logs } = useSWR<DecisionLog[]>('/api/decision/log', fetcher, { revalidateOnFocus: false })
  const searchParams = useSearchParams()

  const [caseType, setCaseType] = useState<string>('A')
  const [ticker, setTicker]     = useState('')
  const [signal, setSignal]     = useState('買い')
  const [strategy, setStrategy] = useState('短期モメンタム')
  const [reason, setReason]     = useState('')
  const [question, setQuestion] = useState('')
  const [person, setPerson]     = useState('husband')

  // URL パラメータで case/ticker を事前入力（他ページの「AI相談」ボタン対応）
  useEffect(() => {
    const caseParam = searchParams.get('case')
    const tickerParam = searchParams.get('ticker')
    if (caseParam && caseParam in CASE_LABELS) setCaseType(caseParam)
    if (tickerParam) setTicker(tickerParam)
  }, [searchParams])

  const [analyzing, setAnalyzing]   = useState(false)
  const [judging, setJudging]       = useState(false)
  const [result, setResult]         = useState<Record<string, unknown> | null>(null)
  const [judgment, setJudgment]     = useState<string>('')
  const [error, setError]           = useState<string>('')

  async function handleAnalyze() {
    if (!window.confirm('入力内容をLLMで分析します。API利用料が発生します。続行しますか？')) return
    setAnalyzing(true)
    setResult(null)
    setJudgment('')
    setError('')
    try {
      const res = await apiFetch(`/api/decision/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ case_type: caseType, ticker, signal, strategy, reason, question, person }),
      })
      const data = await res.json()
      if (data.error) { setError(data.error); return }
      setResult(data)
    } catch (e) {
      setError(String(e))
    } finally {
      setAnalyzing(false)
    }
  }

  async function handleJudge() {
    if (!result) return
    if (!window.confirm('分析結果をLLMで最終判定します。追加のAPI利用料が発生します。続行しますか？')) return
    setJudging(true)
    setJudgment('')
    try {
      const res = await apiFetch(`/api/decision/judge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ case_result: result, user_preference: question }),
      })
      const data = await res.json()
      if (data.error) { setError(data.error); return }
      setJudgment(data.judgment ?? '')
    } catch (e) {
      setError(String(e))
    } finally {
      setJudging(false)
    }
  }

  const caseInfo = CASE_LABELS[caseType] ?? CASE_LABELS['A']

  return (
    <OpsPage en="AI DECISION" title="AI判断支援" subtitle="ケース別の分析と最終判断。実行前にLLM利用と費用を確認する。" widthMode="wide" right={<Chip color={OPS.amber} bg={OPS.amberBg} mono>⚡ LLM実行・課金あり</Chip>}>
      <style dangerouslySetInnerHTML={{ __html: DECISION_CSS }} />

      <section aria-label="判断ケース" style={{ marginBottom: 14 }}>
        <div className="decision-case-grid">
          {Object.entries(CASE_LABELS).map(([key, info]) => {
            const active = caseType === key
            return <button key={key} type="button" aria-pressed={active} onClick={() => { setCaseType(key); setResult(null); setJudgment('') }} style={{ minWidth: 0, padding: '8px 10px', borderRadius: 7, textAlign: 'left', cursor: 'pointer', background: active ? `${info.color}18` : OPS.panelAlt, border: `1px solid ${active ? `${info.color}55` : OPS.hairline}` }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}><span style={{ color: active ? info.color : OPS.dim, fontFamily: OPS.mono, fontSize: 10.5 }}>{key}</span><span style={{ color: active ? OPS.text : OPS.sub, fontSize: 12.5, fontWeight: active ? 700 : 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{info.label.replace(/^ケース.: /, '')}</span></div>
              <div style={{ color: active ? OPS.sub : OPS.dim, fontSize: 10.5, marginTop: 3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{info.desc}</div>
            </button>
          })}
        </div>
      </section>

      <div className="decision-workspace">
        <Panel pad="16px 18px">
          <PanelTitle right={caseType}>判断条件</PanelTitle>
          <div style={{ color: caseInfo.color, fontSize: 15, fontWeight: 700, marginBottom: 14 }}>{caseInfo.label}</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 11 }}>
            {(caseType === 'A' || caseType === 'B') && <Field label="ティッカー"><input value={ticker} onChange={e => setTicker(e.target.value)} placeholder="例: NVDA" style={fieldStyle} /></Field>}
            {caseType === 'A' && <div className="decision-field-pair"><Field label="方向"><select value={signal} onChange={e => setSignal(e.target.value)} style={fieldStyle}><option value="買い">買い</option><option value="売り">売り</option></select></Field><Field label="戦略"><input value={strategy} onChange={e => setStrategy(e.target.value)} placeholder="例: 短期モメンタム" style={fieldStyle} /></Field></div>}
            {caseType === 'B' && <Field label="買い増し理由"><input value={reason} onChange={e => setReason(e.target.value)} placeholder="例: AI半導体需要継続" style={fieldStyle} /></Field>}
            {caseType === 'D' && <Field label="対象"><select value={person} onChange={e => setPerson(e.target.value)} style={fieldStyle}><option value="husband">本人</option><option value="wife">妻</option></select></Field>}
            <Field label="追加質問・メモ（任意）"><textarea value={question} onChange={e => setQuestion(e.target.value)} placeholder="例: 今週末に判断したい。税務上の観点もふまえて。" rows={4} style={{ ...fieldStyle, resize: 'vertical' }} /></Field>
            <button onClick={handleAnalyze} disabled={analyzing} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 12px', borderRadius: 7, cursor: analyzing ? 'not-allowed' : 'pointer', background: analyzing ? OPS.border : OPS.goldBg, color: analyzing ? OPS.sub : OPS.gold, fontWeight: 700, fontSize: 13, border: `1px solid ${analyzing ? OPS.border : OPS.gold}66` }}><span>{analyzing ? '分析中…' : '分析を実行'}</span><span style={{ fontFamily: OPS.mono, fontSize: 10.5 }}>SONNET</span></button>
          </div>
        </Panel>

        <div>
          <Panel pad="16px 18px" style={{ minHeight: 390 }}>
            <PanelTitle right={result ? 'ANALYZED' : 'STANDBY'}>判断出力</PanelTitle>
            {error && <p style={{ color: OPS.vermilion, fontSize: 13 }}>⚠ エラー: {error}</p>}
            {result && <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}><Chip color={caseInfo.color} bg={`${caseInfo.color}18`} mono>{caseInfo.label}</Chip><span style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5 }}>SONNET ANALYSIS</span></div>
              <div style={{ background: OPS.inset, border: `1px solid ${OPS.border}`, borderRadius: 8, padding: 14, fontSize: 13.5, color: OPS.sub, whiteSpace: 'pre-wrap', lineHeight: 1.75, maxHeight: 300, overflowY: 'auto' }}>{String(result.sonnet_analysis ?? 'no analysis')}</div>
              <button onClick={handleJudge} disabled={judging} style={{ marginTop: 11, width: '100%', display: 'flex', justifyContent: 'space-between', padding: '9px 11px', borderRadius: 7, cursor: judging ? 'not-allowed' : 'pointer', background: judging ? OPS.border : OPS.orchidBg, color: judging ? OPS.sub : OPS.orchid, fontWeight: 700, fontSize: 13, border: `1px solid ${judging ? OPS.border : OPS.orchid}66` }}><span>{judging ? '最終判断中…' : '最終判断へ進む'}</span><span style={{ fontFamily: OPS.mono, fontSize: 10.5 }}>OPUS</span></button>
              {judgment && <div style={{ marginTop: 14, borderTop: `1px solid ${OPS.hairline}`, paddingTop: 14 }}><div style={{ color: OPS.orchid, fontFamily: OPS.mono, fontSize: 11, letterSpacing: '0.1em', marginBottom: 8 }}>FINAL JUDGMENT</div><div style={{ background: OPS.orchidBg, border: `1px solid ${OPS.orchid}4d`, borderRadius: 8, padding: 14, fontSize: 13.5, color: OPS.text, whiteSpace: 'pre-wrap', lineHeight: 1.8, maxHeight: 350, overflowY: 'auto' }}>{judgment}</div></div>}
            </>}
            {!result && !error && <div style={{ minHeight: 300, display: 'grid', placeItems: 'center', textAlign: 'center' }}><div><div style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 26, marginBottom: 12 }}>◇</div><div style={{ color: OPS.sub, fontSize: 14 }}>出力待機</div><p style={{ color: OPS.dim, fontSize: 12, lineHeight: 1.7, margin: '6px 0 0' }}>左の条件を確認し、分析を実行してください。<br />分析結果から最終判断へ進めます。</p></div></div>}
          </Panel>

          {logs && logs.length > 0 && <Panel pad="14px 16px" style={{ marginTop: 12 }}><PanelTitle right={`${Math.min(logs.length, 5)} RECORDS`}>最近の判断</PanelTitle><div style={{ display: 'grid', gap: 6 }}>{[...logs].reverse().slice(0, 5).map((log, i) => { const info = CASE_LABELS[log.case_type] ?? { color: OPS.sub, label: log.case_type }; return <div key={i} style={{ display: 'grid', gridTemplateColumns: 'minmax(120px,.7fr) minmax(0,1.6fr) auto', gap: 10, alignItems: 'center', background: OPS.panelAlt, borderLeft: `2px solid ${info.color}`, padding: '7px 9px', fontSize: 11.5 }}><span style={{ color: info.color }}>{info.label}</span><span style={{ color: OPS.sub, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{log.opus_judgment ?? log.ticker ?? '—'}</span><span style={{ color: OPS.dim, fontFamily: OPS.mono }}>{log.created_at ?? ''}</span></div> })}</div></Panel>}
        </div>
      </div>
    </OpsPage>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label style={{ display: 'block' }}><span style={{ color: OPS.sub, fontSize: 11.5, display: 'block', marginBottom: 4 }}>{label}</span>{children}</label>
}

const fieldStyle: React.CSSProperties = { width: '100%', padding: '8px 10px', borderRadius: 7, fontSize: 13, background: OPS.inset, border: `1px solid ${OPS.border}`, color: OPS.text, outline: 'none' }

export default function DecisionPage() {
  return (
    <Suspense fallback={<div style={{ color: OPS.sub, padding: 40 }}>読み込み中…</div>}>
      <DecisionPageInner />
    </Suspense>
  )
}

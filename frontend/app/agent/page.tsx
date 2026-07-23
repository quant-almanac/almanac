'use client'

import { useState, useRef, useEffect } from 'react'
import { API_BASE, apiFetch } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { Chip, OpsPage } from '@/components/today/ops/PageKit'

// ─── 型定義 ───────────────────────────────────────────────
type Mode = 'default' | 'risk' | 'nisa'

type LogEntry =
  | { type: 'start'; message: string }
  | { type: 'text'; content: string }
  | { type: 'tool'; name: string; input: string }
  | { type: 'done'; success: boolean; cost_usd?: number; result?: string; error?: string }
  | { type: 'error'; message: string }

type AgentResult = {
  as_of?: string
  overall_stance?: string
  headline?: string
  priority_actions?: Array<{ rank: number; urgency: string; ticker?: string; action: string; reason?: string }>
  risk_warnings?: string[]
  opportunity?: string
  error?: string
  // risk mode
  summary?: string
  // nisa mode
  strategy?: string
  [key: string]: unknown
}

const MODES: { value: Mode; label: string; icon: string; desc: string }[] = [
  { value: 'default', label: '総合分析', icon: '🤖', desc: '保有・シグナル・マクロを統合' },
  { value: 'risk',    label: 'リスク',   icon: '⚠️', desc: '集中リスク・ガードレール余裕度' },
  { value: 'nisa',    label: 'NISA',     icon: '🏦', desc: '枠消化ペース・長期候補照合' },
]

const STANCE_CFG: Record<string, { label: string; color: string }> = {
  defensive:             { label: '守りモード',   color: OPS.blue },
  neutral:               { label: 'ニュートラル', color: OPS.gold },
  moderately_aggressive: { label: '攻めモード',   color: OPS.amber },
  aggressive:            { label: '積極攻勢',     color: OPS.vermilion },
}

const URGENCY_CFG: Record<string, { label: string; color: string; bg: string }> = {
  high:   { label: 'HIGH',   color: OPS.vermilion, bg: OPS.vermilionBg },
  medium: { label: 'MED',    color: OPS.amber, bg: OPS.amberBg },
  low:    { label: 'LOW',    color: OPS.green, bg: OPS.greenBg },
}

// ─── ログ表示（再実行時のみ） ─────────────────────────────
function StreamLog({ logs, running }: { logs: LogEntry[]; running: boolean }) {
  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => { bottomRef.current?.scrollIntoView() }, [logs])

  if (logs.length === 0 && !running) return null

  return (
    <div style={{
      background: OPS.inset, border: `1px solid ${OPS.border}`, borderRadius: 10,
      padding: 12, maxHeight: 240, overflowY: 'auto', fontFamily: OPS.mono, marginTop: 16,
    }}>
      <p style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 12, marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.08em' }}>
        実行ログ
      </p>
      {logs.map((log, i) => (
        <div key={i} style={{ marginBottom: 4 }}>
          {log.type === 'start' && <p style={{ color: OPS.gold, fontSize: 14 }}>▶ {log.message}</p>}
          {log.type === 'tool'  && <p style={{ color: OPS.blue, fontSize: 14 }}>🔧 {log.name}</p>}
          {log.type === 'text'  && <p style={{ color: OPS.sub, fontSize: 14, whiteSpace: 'pre-wrap' }}>{log.content}</p>}
          {log.type === 'done' && log.success  && <p style={{ color: OPS.green, fontSize: 14 }}>✅ 完了{log.cost_usd ? ` ($${log.cost_usd.toFixed(4)})` : ''}</p>}
          {(log.type === 'error' || (log.type === 'done' && !log.success)) && (
            <p style={{ color: OPS.vermilion, fontSize: 14 }}>❌ {log.type === 'error' ? log.message : log.error}</p>
          )}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}

// ─── 結果カード ───────────────────────────────────────────
function ResultCard({ result }: { result: AgentResult }) {
  if (result.error) {
    return (
      <div style={{ background: OPS.panelAlt, border: `1px dashed ${OPS.border}`, borderRadius: 12, padding: 32, textAlign: 'center' }}>
        <p style={{ color: OPS.sub, fontSize: 13 }}>まだ自動分析が実行されていません</p>
        <p style={{ color: OPS.dim, fontSize: 14, marginTop: 6 }}>毎平日 8:30 に自動実行、または「再実行」ボタンで手動実行できます</p>
      </div>
    )
  }

  const stanceCfg = STANCE_CFG[result.overall_stance ?? ''] ?? STANCE_CFG.neutral

  return (
    <div>
      {/* ヘッドライン */}
      {(result.headline || result.summary || result.strategy) && (
        <div style={{
          background: `${stanceCfg.color}10`, border: `1px solid ${stanceCfg.color}28`,
          borderLeft: `4px solid ${stanceCfg.color}`, borderRadius: 10, padding: '14px 18px', marginBottom: 14,
        }}>
          {result.overall_stance && (
            <p style={{ color: stanceCfg.color, fontSize: 13, fontWeight: 700, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              {stanceCfg.label}
            </p>
          )}
          <p style={{ color: OPS.text, fontSize: 15, fontWeight: 600, lineHeight: 1.65 }}>
            {result.headline ?? result.summary ?? result.strategy ?? ''}
          </p>
        </div>
      )}

      {/* 優先アクション */}
      {(result.priority_actions ?? []).length > 0 && (
        <div style={{ background: OPS.panelAlt, border: `1px solid ${OPS.border}`, borderRadius: 10, padding: '14px 18px', marginBottom: 12 }}>
          <p style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 12, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
            優先アクション
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {result.priority_actions!.map((a, i) => {
              const u = URGENCY_CFG[a.urgency] ?? URGENCY_CFG.low
              return (
                <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
                  <span style={{ fontSize: 14, padding: '2px 7px', borderRadius: 4, fontWeight: 700, flexShrink: 0, background: u.bg, color: u.color, border: `1px solid ${u.color}40`, marginTop: 1 }}>
                    {u.label}
                  </span>
                  <div style={{ minWidth: 0 }}>
                    {a.ticker && <span style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 14, fontWeight: 700, marginRight: 6 }}>{a.ticker}</span>}
                    <span style={{ color: OPS.text, fontSize: 13 }}>{a.action}</span>
                    {a.reason && <p style={{ color: OPS.sub, fontSize: 14, marginTop: 2, lineHeight: 1.5 }}>{a.reason}</p>}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* リスク警告 */}
      {(result.risk_warnings ?? []).length > 0 && (
        <div style={{ background: OPS.vermilionBg, border: `1px solid ${OPS.vermilion}33`, borderRadius: 10, padding: '12px 16px', marginBottom: 12 }}>
          <p style={{ color: OPS.vermilion, fontSize: 13, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>
            ⚠️ リスク警告
          </p>
          {result.risk_warnings!.map((w, i) => (
            <p key={i} style={{ color: OPS.redSoft, fontSize: 13, lineHeight: 1.6, paddingLeft: 12, position: 'relative' }}>
              <span style={{ position: 'absolute', left: 0 }}>•</span>{w}
            </p>
          ))}
        </div>
      )}

      {/* 機会 */}
      {result.opportunity && (
        <div style={{ background: OPS.greenBg, border: `1px solid ${OPS.green}33`, borderRadius: 10, padding: '12px 16px' }}>
          <p style={{ color: OPS.green, fontSize: 13, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>
            💡 今週の機会
          </p>
          <p style={{ color: OPS.green, fontSize: 13, lineHeight: 1.65 }}>{result.opportunity}</p>
        </div>
      )}
    </div>
  )
}

// ─── メインページ ─────────────────────────────────────────
export default function AgentPage() {
  const [mode, setMode] = useState<Mode>('default')
  const [result, setResult] = useState<AgentResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [logs, setLogs] = useState<LogEntry[]>([])
  // P0-1: EventSource は GET 専用で X-API-Key を載せられないため、AbortController + fetch SSE に変更
  const abortRef = useRef<AbortController | null>(null)

  // 結果を取得
  async function fetchResult(m: Mode) {
    setLoading(true)
    try {
      const r = await fetch(`${API_BASE}/api/agent/result?mode=${m}`)
      setResult(await r.json())
    } catch {
      setResult({ error: 'fetch failed' })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchResult(mode) }, [mode])
  useEffect(() => () => abortRef.current?.abort(), [])

  // 手動再実行（SSE）— P0-1: POST + apiFetch で X-API-Key を付与
  async function startAgent() {
    if (running) return
    if (!window.confirm(`「${MODES.find(item => item.value === mode)?.label ?? mode}」をLLMで再実行します。API利用料が発生します。続行しますか？`)) return
    setLogs([])
    setRunning(true)

    const ac = new AbortController()
    abortRef.current = ac

    try {
      const resp = await apiFetch(`/api/agent/run?mode=${mode}`, {
        method: 'POST',
        headers: { Accept: 'text/event-stream' },
        signal: ac.signal,
      })
      if (!resp.ok || !resp.body) {
        setLogs(prev => [...prev, { type: 'error', message: `HTTP ${resp.status}` }])
        setRunning(false)
        return
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      let done = false

      while (!done) {
        const { value, done: streamDone } = await reader.read()
        if (streamDone) break
        buffer += decoder.decode(value, { stream: true })

        // SSE event boundary: "\n\n"
        let idx: number
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          const raw = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)

          let eventName = 'message'
          let dataLine = ''
          for (const line of raw.split('\n')) {
            if (line.startsWith('event:')) eventName = line.slice(6).trim()
            else if (line.startsWith('data:')) dataLine += line.slice(5).trim()
          }
          if (!dataLine) continue
          try {
            const data = JSON.parse(dataLine)
            setLogs(prev => [...prev, { ...data, type: eventName } as LogEntry])
            if (eventName === 'done' || eventName === 'error') {
              done = true
              if (eventName === 'done') fetchResult(mode)
              break
            }
          } catch { /* ignore parse error */ }
        }
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        setLogs(prev => [...prev, { type: 'error', message: String(e) }])
      }
    } finally {
      setRunning(false)
    }
  }

  return (
    <OpsPage en="AI AGENT" title="Agent 分析" subtitle="統合・リスク・NISAの分析を実行・閲覧する。手動実行はLLM費用を伴う。" widthMode="wide" right={<Chip color={OPS.amber} bg={OPS.amberBg} mono>⚡ LLM実行・課金あり</Chip>}>
      {/* ヘッダー */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <div>
          <h2 style={{ color: OPS.text, fontSize: 18, margin: 0 }}>実行コントロール</h2>
          <p style={{ color: OPS.sub, fontSize: 12, marginTop: 4 }}>毎平日 8:30 自動実行 — 最新結果を表示</p>
        </div>
        <button
          onClick={startAgent}
          disabled={running}
          style={{
            padding: '8px 18px', borderRadius: 8, fontSize: 13, fontWeight: 600,
            cursor: running ? 'not-allowed' : 'pointer',
            background: running ? OPS.border : OPS.goldBg,
            border: `1px solid ${running ? OPS.border : OPS.gold}66`,
            color: running ? OPS.sub : OPS.gold,
            display: 'flex', alignItems: 'center', gap: 6,
          }}
        >
          {running
            ? <><span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%', background: OPS.gold }} />実行中…</>
            : '↺ 再実行'
          }
        </button>
      </div>

      {/* モードタブ */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 20, borderBottom: `1px solid ${OPS.border}`, paddingBottom: 0 }}>
        {MODES.map(m => (
          <button
            key={m.value}
            onClick={() => !running && setMode(m.value)}
            style={{
              padding: '8px 16px', background: 'transparent', border: 'none',
              borderBottom: mode === m.value ? `2px solid ${OPS.gold}` : '2px solid transparent',
              color: mode === m.value ? OPS.gold : OPS.sub,
              fontWeight: mode === m.value ? 700 : 400, fontSize: 13,
              cursor: running ? 'not-allowed' : 'pointer',
              display: 'flex', alignItems: 'center', gap: 6,
            }}
          >
            <span>{m.icon}</span> {m.label}
          </button>
        ))}
      </div>

      {/* 最終更新日時 */}
      {result?.as_of && !loading && (
        <p style={{ color: OPS.sub, fontSize: 14, marginBottom: 14 }}>
          最終更新: {new Date(result.as_of).toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
        </p>
      )}

      {/* 結果 */}
      {loading
        ? <div style={{ background: OPS.panelAlt, border: `1px solid ${OPS.border}`, borderRadius: 12, padding: 40, textAlign: 'center' }}>
            <p style={{ color: OPS.sub, fontSize: 13 }}>読み込み中…</p>
          </div>
        : result && <ResultCard result={result} />
      }

      {/* ストリームログ */}
      {(logs.length > 0 || running) && <StreamLog logs={logs} running={running} />}
    </OpsPage>
  )
}

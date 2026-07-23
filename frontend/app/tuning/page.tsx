'use client'

import { useState } from 'react'
import useSWR, { useSWRConfig } from 'swr'
import { fetcher, apiFetch, apiErrorMessage } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, Chip, Loading } from '@/components/today/ops/PageKit'

interface Param {
  key: string
  value: number
  default?: number
  min?: number
  max?: number
  step?: number
  unit?: string
  category?: string
  label?: string
  desc?: string
  ai_recommended?: number
  ai_rationale?: string
  last_changed?: string
}
interface TuningData {
  by_category: Record<string, Param[]>
  categories: string[]
  total: number
}
interface AutoRun {
  run_id: string
  started_at?: string
  finished_at?: string
  status: string
  dry_run?: boolean
  applied_count?: number
  would_apply_count?: number
  blockers?: string[]
  error?: string
  changes?: Record<string, { old: number; new: number }>
}
interface AutoMode {
  mode: 'off' | 'shadow' | 'apply'
  enabled: boolean
  effective_apply: boolean
  disabled_reason?: string | null
  policy_version?: number
  allowlist: string[]
  denylist: string[]
  risk_class: Record<string, 'low' | 'medium' | 'high'>
  schedule?: { timezone?: string; times?: string[]; weekdays?: string[]; source?: string }
  last_run?: string
  last_status?: string
  recent_runs?: AutoRun[]
  audit?: { status?: string; issue_count?: number; issues?: unknown[] }
}

const CAT_LABEL: Record<string, string> = {
  post_filter: 'ポストフィルタ', behavioral_guard: '行動ガード', rebalance: 'リバランス',
  drawdown: 'ドローダウン', margin_short: '信用・空売り', cache: 'キャッシュ', signal: 'シグナル',
  guardrail: 'ガードレール', stance: 'スタンス', leverage: 'レバレッジ', screening: 'スクリーニング',
}
const STATUS_LABEL: Record<string, string> = {
  applied: '適用済み', no_change: '変更不要', dry_run: 'プレビュー', shadow: 'シャドー',
  blocked_stale_inputs: '入力鮮度で停止', skipped_same_context: '同一状況で省略', failed: '失敗',
  lock_busy: '実行中', rolled_back: '取消済み', disabled: '無効',
}
const MODE_LABEL = { off: 'OFF', shadow: 'SHADOW', apply: 'APPLY' }

export default function TuningPage() {
  const { mutate } = useSWRConfig()
  const { data, isLoading } = useSWR<TuningData>('/api/tuning', fetcher, { refreshInterval: 300000 })
  const { data: auto } = useSWR<AutoMode>('/api/tuning/auto-mode', fetcher, { refreshInterval: 60000 })
  const [cat, setCat] = useState<string | null>(null)
  const [editing, setEditing] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [preview, setPreview] = useState<AutoRun | null>(null)

  const cats = data?.categories ?? []
  const activeCat = cat ?? cats[0]
  const params = data?.by_category[activeCat] ?? []

  async function jsonOrThrow(res: Response, fallback: string) {
    const json = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(apiErrorMessage(json, fallback))
    return json
  }

  async function save(key: string) {
    const value = editing[key]
    if (value === undefined) return
    setBusy(key); setMessage(null)
    try {
      const numeric = parseFloat(value)
      const isCurrencyPair = key === 'currency_usd_target_pct' || key === 'currency_jpy_target_pct'
      const values = isCurrencyPair
        ? { [key]: numeric, [key === 'currency_usd_target_pct' ? 'currency_jpy_target_pct' : 'currency_usd_target_pct']: 100 - numeric }
        : null
      const res = values
        ? await apiFetch('/api/tuning/batch', { method: 'POST', body: JSON.stringify({ values, rationale: 'manual paired edit' }) })
        : await apiFetch(`/api/tuning/${key}`, { method: 'POST', body: JSON.stringify({ value: numeric, rationale: 'manual edit' }) })
      await jsonOrThrow(res, '保存に失敗しました')
      setEditing(state => { const copy = { ...state }; delete copy[key]; if (isCurrencyPair) { delete copy.currency_usd_target_pct; delete copy.currency_jpy_target_pct } return copy })
      mutate('/api/tuning')
    } catch (error) { setMessage(String(error)) } finally { setBusy(null) }
  }

  async function applyAi(key: string, recommended: number) {
    setBusy(key); setMessage(null)
    try {
      const res = await apiFetch(`/api/tuning/${key}`, { method: 'POST', body: JSON.stringify({ value: recommended, rationale: 'AI推奨を手動適用' }) })
      await jsonOrThrow(res, 'AI推奨の適用に失敗しました')
      mutate('/api/tuning')
    } catch (error) { setMessage(String(error)) } finally { setBusy(null) }
  }

  async function changeMode(mode: AutoMode['mode']) {
    if (mode === 'apply' && !window.confirm('Auto Tuneを実適用モードにします。次回定期実行から対象パラメータが自動変更されます。続行しますか？')) return
    setBusy('mode'); setMessage(null)
    try {
      const res = await apiFetch('/api/tuning/auto-mode', { method: 'POST', body: JSON.stringify({ mode, confirm: mode === 'apply' }) })
      await jsonOrThrow(res, 'モード変更に失敗しました')
      mutate('/api/tuning/auto-mode')
    } catch (error) { setMessage(String(error)) } finally { setBusy(null) }
  }

  async function runPreview() {
    if (!window.confirm('最新データとAIを使ってAuto Tuneの強制プレビューを実行します。実値は変更されません。')) return
    setBusy('preview'); setMessage(null); setPreview(null)
    try {
      const res = await apiFetch('/api/tuning/auto-tune-now?force=true', { method: 'POST' })
      const json = await jsonOrThrow(res, 'プレビューに失敗しました') as AutoRun
      setPreview(json)
      mutate('/api/tuning/auto-mode'); mutate('/api/tuning')
    } catch (error) { setMessage(String(error)) } finally { setBusy(null) }
  }

  async function rollback(run: AutoRun) {
    if (!window.confirm(`${run.run_id.slice(0, 8)} の変更を取り消します。現在値が変更後の値と一致する場合だけ実行されます。`)) return
    setBusy(run.run_id); setMessage(null)
    try {
      const res = await apiFetch(`/api/tuning/auto-runs/${run.run_id}/rollback`, { method: 'POST', body: JSON.stringify({ confirm: true }) })
      await jsonOrThrow(res, 'ロールバックに失敗しました')
      mutate('/api/tuning/auto-mode'); mutate('/api/tuning')
    } catch (error) { setMessage(String(error)) } finally { setBusy(null) }
  }

  const modeColor = auto?.mode === 'apply' ? OPS.green : auto?.mode === 'shadow' ? OPS.amber : OPS.dim

  return (
    <OpsPage en="TUNING" title="パラメータ・チューニング" subtitle="運用値、AI推奨、自動適用の実効状態と変更履歴を一か所で管理する。" right={data && <Chip color={OPS.dim} mono>{data.total} パラメータ</Chip>}>
      {isLoading && <Loading />}
      {message && <Panel pad="12px 14px"><span role="alert" style={{ color: OPS.redSoft, fontSize: 12.5 }}>{message}</span></Panel>}

      <Panel pad="16px 18px">
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.dim, letterSpacing: '.1em' }}>AUTO TUNE</div>
            <div style={{ fontSize: 21, color: modeColor, fontFamily: OPS.mono, marginTop: 4 }}>{auto ? MODE_LABEL[auto.mode] : '確認中'}</div>
          </div>
          <div style={{ color: OPS.sub, fontSize: 12, lineHeight: 1.7, flex: '1 1 320px' }}>
            {auto?.mode === 'apply' ? '次回定期実行から安全検証に合格した変更を自動適用します。' : auto?.mode === 'shadow' ? '推奨と拒否理由だけを記録し、実値は変更しません。' : auto?.disabled_reason ?? '自動適用は停止中です。'}
            <div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10.5 }}>
              {auto?.schedule?.weekdays?.join('/') ?? '平日'} {auto?.schedule?.times?.join(' · ') ?? '—'} {auto?.schedule?.timezone ?? ''} · policy v{auto?.policy_version ?? '—'} · 対象 {auto?.allowlist?.length ?? 0}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {(['off', 'shadow', 'apply'] as const).map(mode => <button key={mode} disabled={busy === 'mode' || auto?.mode === mode} onClick={() => changeMode(mode)} style={modeButton(auto?.mode === mode)}>{MODE_LABEL[mode]}</button>)}
            <button disabled={busy === 'preview'} onClick={runPreview} style={primaryBtn}>{busy === 'preview' ? '実行中…' : '強制プレビュー'}</button>
          </div>
        </div>
        <div style={{ borderTop: `1px solid ${OPS.hairline}`, marginTop: 14, paddingTop: 12, display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(170px,1fr))', gap: 10, fontSize: 11.5 }}>
          <StatusValue label="最終実行" value={auto?.last_run?.slice(0, 16).replace('T', ' ') ?? '未実行'} />
          <StatusValue label="結果" value={STATUS_LABEL[auto?.last_status ?? ''] ?? auto?.last_status ?? '—'} />
          <StatusValue label="監査" value={auto?.audit?.status === 'ok' ? 'OK' : `${auto?.audit?.issue_count ?? '—'} issues`} color={auto?.audit?.status === 'ok' ? OPS.green : OPS.amber} />
        </div>
        {preview && <div style={{ marginTop: 12, padding: 10, background: OPS.panelAlt, borderRadius: 5, color: OPS.sub, fontSize: 12 }}>プレビュー: {STATUS_LABEL[preview.status] ?? preview.status} · 適用候補 {preview.would_apply_count ?? 0}件{preview.blockers?.length ? ` · 停止理由 ${preview.blockers.join(', ')}` : ''}</div>}
      </Panel>

      {(auto?.recent_runs?.length ?? 0) > 0 && <Panel pad="14px 18px">
        <div style={{ fontFamily: OPS.mono, color: OPS.gold, fontSize: 11, letterSpacing: '.08em', marginBottom: 8 }}>RECENT RUNS</div>
        <div style={{ overflowX: 'auto' }}><table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.5 }}><tbody>
          {auto!.recent_runs!.slice(0, 6).map(run => <tr key={run.run_id} style={{ borderTop: `1px solid ${OPS.hairline}` }}>
            <td style={cell}>{run.started_at?.slice(5, 16).replace('T', ' ') ?? '—'}</td>
            <td style={cell}>{STATUS_LABEL[run.status] ?? run.status}</td>
            <td style={cell}>適用 {run.applied_count ?? 0}{run.would_apply_count != null ? ` / 候補 ${run.would_apply_count}` : ''}</td>
            <td style={{ ...cell, color: OPS.dim }}>{run.error ?? run.blockers?.join(', ') ?? Object.keys(run.changes ?? {}).join(', ')}</td>
            <td style={{ ...cell, textAlign: 'right' }}>{run.status === 'applied' && <button disabled={busy === run.run_id} onClick={() => rollback(run)} style={quietBtn}>取消</button>}</td>
          </tr>)}
        </tbody></table></div>
      </Panel>}

      {data && <>
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', margin: '20px 0' }}>{cats.map(category => <button key={category} onClick={() => setCat(category)} style={categoryButton(category === activeCat)}>{CAT_LABEL[category] ?? category}</button>)}</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>{params.map(param => {
          const dirty = editing[param.key] !== undefined
          const changed = param.default != null && param.value !== param.default
          const aiDiffers = param.ai_recommended != null && param.ai_recommended !== param.value
          const risk = auto?.risk_class?.[param.key]
          return <Panel key={param.key} pad="14px 18px">
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: param.desc ? 6 : 0, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 14, fontWeight: 600, color: OPS.text }}>{param.label ?? param.key}</span>
              <span style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.dim }}>{param.key}</span>
              {changed && <Chip color={OPS.amber} bg={OPS.amberBg} mono>変更済</Chip>}
              {auto?.allowlist?.includes(param.key) && <Chip color={risk === 'high' ? OPS.redSoft : risk === 'medium' ? OPS.amber : OPS.green} mono>AUTO {risk?.toUpperCase()}</Chip>}
              <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
                <input aria-label={`${param.label ?? param.key}の値`} value={dirty ? editing[param.key] : String(param.value)} onChange={event => setEditing(state => ({ ...state, [param.key]: event.target.value }))} style={inputStyle(dirty)} />
                {param.unit && <span style={{ fontSize: 11, color: OPS.dim, minWidth: 26 }}>{param.unit}</span>}
                {dirty && <button onClick={() => save(param.key)} disabled={busy === param.key} style={saveBtn}>{busy === param.key ? '…' : '保存'}</button>}
              </div>
            </div>
            {param.desc && <p style={{ fontSize: 12, color: OPS.dim, lineHeight: 1.6, margin: 0 }}>{param.desc}</p>}
            <div style={{ display: 'flex', gap: 16, marginTop: 8, fontFamily: OPS.mono, fontSize: 11, color: OPS.dim, alignItems: 'center', flexWrap: 'wrap' }}>
              {param.default != null && <span>既定 {param.default}</span>}{param.min != null && param.max != null && <span>範囲 {param.min}–{param.max}</span>}
              {aiDiffers && <span title={param.ai_rationale} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: OPS.blue }}>AI推奨 {param.ai_recommended}<button onClick={() => applyAi(param.key, param.ai_recommended!)} disabled={busy === param.key} style={aiBtn}>手動適用</button></span>}
              {param.last_changed && <span style={{ marginLeft: 'auto' }}>更新 {param.last_changed.slice(0, 16).replace('T', ' ')}</span>}
            </div>
          </Panel>
        })}</div>
      </>}
    </OpsPage>
  )
}

function StatusValue({ label, value, color = OPS.sub }: { label: string; value: string; color?: string }) { return <div><div style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 10 }}>{label}</div><div style={{ color, marginTop: 3 }}>{value}</div></div> }
const modeButton = (active: boolean): React.CSSProperties => ({ background: active ? OPS.goldBg : 'transparent', border: `1px solid ${active ? OPS.gold + '66' : OPS.hairline}`, borderRadius: 5, color: active ? OPS.gold : OPS.sub, fontFamily: OPS.mono, fontSize: 11, padding: '6px 9px', cursor: active ? 'default' : 'pointer' })
const categoryButton = (active: boolean): React.CSSProperties => ({ background: active ? OPS.goldBg : 'transparent', border: `1px solid ${active ? OPS.gold + '66' : OPS.hairline}`, borderRadius: 5, color: active ? OPS.gold : OPS.sub, fontSize: 12.5, padding: '5px 13px', cursor: 'pointer', fontFamily: OPS.sans })
const inputStyle = (dirty: boolean): React.CSSProperties => ({ width: 92, background: OPS.panelAlt, border: `1px solid ${dirty ? OPS.gold + '88' : OPS.border}`, borderRadius: 5, color: dirty ? OPS.gold : OPS.text, fontSize: 13, padding: '5px 8px', fontFamily: OPS.mono, textAlign: 'right', outline: 'none' })
const cell: React.CSSProperties = { padding: '7px 6px', color: OPS.sub, verticalAlign: 'top' }
const primaryBtn: React.CSSProperties = { background: OPS.goldBg, border: `1px solid ${OPS.gold}66`, borderRadius: 5, color: OPS.gold, fontSize: 11.5, fontWeight: 600, padding: '6px 10px', cursor: 'pointer', fontFamily: OPS.sans }
const quietBtn: React.CSSProperties = { background: 'transparent', border: `1px solid ${OPS.hairline}`, borderRadius: 4, color: OPS.sub, fontSize: 10.5, padding: '3px 7px', cursor: 'pointer' }
const saveBtn: React.CSSProperties = { ...primaryBtn, fontSize: 12, padding: '5px 12px' }
const aiBtn: React.CSSProperties = { background: OPS.blueBg, border: `1px solid ${OPS.blue}44`, borderRadius: 4, color: OPS.blue, fontSize: 10.5, padding: '2px 8px', cursor: 'pointer', fontFamily: OPS.sans }

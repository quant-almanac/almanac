'use client'

import { useState } from 'react'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS, TYPE_META } from '@/components/today/ops/tokens'
import { OpsPage, Panel, PanelTitle, Stat, Chip, Bar, Modal, Loading, Grid } from '@/components/today/ops/PageKit'

interface BLView { bull_view?: number; bear_view?: number; macro_view?: number; mean_view?: number; variance?: number; n_signals?: number; avg_confidence?: number }
interface Belief { id?: string; ticker?: string; theme?: string; conviction_score?: number; rationale?: string; source_agent?: string; evidence?: string | Record<string, unknown>; last_updated?: string }
interface RegimeConsensus {
  hmm_regime?: string; macro_score?: number; vix?: number; vix_scale?: string; spy_above?: boolean
  bull_count?: number; bear_count?: number; confidence?: number; direction?: string; conflicted?: boolean
}
interface UpgradesData {
  bl_views?: { views?: Record<string, BLView>; n_tickers?: number; bl_mode?: string; as_of?: string }
  beliefs?: { beliefs?: Belief[]; version?: string }
  regime_consensus?: RegimeConsensus
}

export default function StrategyPage() {
  const { data, isLoading } = useSWR<UpgradesData>('/api/ai-upgrades', fetcher, { refreshInterval: 300000 })
  const [openBelief, setOpenBelief] = useState<Belief | null>(null)

  const rc = data?.regime_consensus
  const blViews = Object.entries(data?.bl_views?.views ?? {}).sort((a, b) => (b[1].mean_view ?? 0) - (a[1].mean_view ?? 0))
  const beliefs = [...(data?.beliefs?.beliefs ?? [])].sort((a, b) => (b.conviction_score ?? 0) - (a.conviction_score ?? 0))

  return (
    <OpsPage
      en="STRATEGY"
      title="戦略エンジン"
      subtitle="レジーム・コンセンサス、Black-Litterman ビュー、FinCon の信念（conviction）。AI が今どの前提で相場を捉えているかの内部状態。"
    >
      {isLoading && <Loading />}
      {data && (
        <>
          {/* レジーム・コンセンサス */}
          {rc && (
            <Panel pad="18px 20px" style={{ borderLeft: `3px solid ${rc.direction === '強気' ? OPS.green : rc.direction === '弱気' ? OPS.vermilion : OPS.amber}` }}>
              <PanelTitle right={rc.conflicted ? '⚠ 対立あり' : '合意'}>レジーム・コンセンサス</PanelTitle>
              <Grid cols={5} gap={12}>
                <Stat label="方向" value={rc.direction ?? '—'} color={rc.direction === '強気' ? OPS.green : rc.direction === '弱気' ? OPS.vermilion : OPS.amber} />
                <Stat label="HMM レジーム" value={rc.hmm_regime ?? '—'} />
                <Stat label="確信度" value={rc.confidence != null ? `${Math.round(rc.confidence * 100)}` : '—'} unit="%" />
                <Stat label="強気 / 弱気" value={`${rc.bull_count ?? 0} / ${rc.bear_count ?? 0}`} sub="シグナル数" />
                <Stat label="VIX" value={rc.vix?.toFixed(1) ?? '—'} sub={`スケール ${rc.vix_scale ?? '—'}`} />
              </Grid>
            </Panel>
          )}

          {/* BL views */}
          {blViews.length > 0 && (
            <div style={{ marginTop: 22 }}>
              <Panel pad="16px 18px">
                <PanelTitle right={`${data.bl_views?.n_tickers ?? blViews.length} 銘柄 · mode ${data.bl_views?.bl_mode}`}>Black-Litterman ビュー</PanelTitle>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {blViews.map(([tk, v]) => {
                    const mean = (v.mean_view ?? 0) * 100
                    return (
                      <div key={tk} style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 12.5 }}>
                        <span style={{ fontFamily: OPS.mono, fontWeight: 500, color: OPS.text, minWidth: 60 }}>{tk}</span>
                        <div style={{ flex: 1, position: 'relative', height: 6, background: OPS.hairline, borderRadius: 3 }}>
                          <div className="ops-bar-fill" style={{ position: 'absolute', left: '50%', width: `${Math.min(50, Math.abs(mean) * 2)}%`, transform: mean < 0 ? 'translateX(-100%)' : 'none', height: '100%', background: mean >= 0 ? OPS.green : OPS.vermilion, borderRadius: 3 }} />
                          <div style={{ position: 'absolute', left: '50%', top: -2, width: 1, height: 10, background: OPS.border }} />
                        </div>
                        <span style={{ fontFamily: OPS.mono, color: mean >= 0 ? OPS.green : OPS.redSoft, minWidth: 56, textAlign: 'right' }}>
                          {mean >= 0 ? '+' : ''}{mean.toFixed(1)}%
                        </span>
                        <span style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.dim, minWidth: 70, textAlign: 'right' }}>
                          確信 {v.avg_confidence ?? '—'}%
                        </span>
                      </div>
                    )
                  })}
                </div>
                <p style={{ fontSize: 11, color: OPS.dim, margin: '12px 0 0' }}>期待超過リターン（bull/bear/macro ビューの加重平均）。中央=0%。</p>
              </Panel>
            </div>
          )}

          {/* FinCon beliefs */}
          {beliefs.length > 0 && (
            <div style={{ marginTop: 22 }}>
              <PanelTitle right={`${beliefs.length} 件 · クリックで根拠`}>FinCon 信念（conviction）</PanelTitle>
              <Grid minmax={260} gap={10}>
                {beliefs.map((b, i) => {
                  const t = b.theme ? TYPE_META[b.theme] : null
                  const conv = b.conviction_score ?? 0
                  return (
                    <Panel key={b.id ?? i} hover onClick={() => setOpenBelief(b)} pad="12px 14px">
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                        <span style={{ fontFamily: OPS.mono, fontWeight: 600, color: OPS.text }}>{b.ticker}</span>
                        {b.theme && <Chip color={t?.color ?? OPS.sub} bg={OPS.dimBg} mono>{t?.label ?? b.theme}</Chip>}
                        <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, fontSize: 15, color: conv >= 70 ? OPS.gold : conv >= 50 ? OPS.sub : OPS.dim }}>{conv}</span>
                      </div>
                      <Bar pct={conv} color={conv >= 70 ? OPS.gold : conv >= 50 ? OPS.blue : OPS.dim} height={5} />
                      <div style={{ fontSize: 11, color: OPS.dim, marginTop: 8, overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                        {typeof b.evidence === 'string' ? b.evidence : b.rationale?.slice(0, 60)}
                      </div>
                    </Panel>
                  )
                })}
              </Grid>
            </div>
          )}
        </>
      )}

      <Modal open={!!openBelief} onClose={() => setOpenBelief(null)} width={600}>
        {openBelief && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
              <span style={{ fontFamily: OPS.mono, fontSize: 18, fontWeight: 700, color: OPS.gold }}>{openBelief.ticker}</span>
              {openBelief.theme && <Chip color={(openBelief.theme && TYPE_META[openBelief.theme]?.color) ?? OPS.sub} bg={OPS.dimBg} mono>{TYPE_META[openBelief.theme ?? '']?.label ?? openBelief.theme}</Chip>}
              <span style={{ marginLeft: 'auto', fontFamily: OPS.mono, fontSize: 20, color: OPS.gold }}>{openBelief.conviction_score}</span>
            </div>
            <div style={{ marginBottom: 14 }}>
              <Bar pct={openBelief.conviction_score ?? 0} color={OPS.gold} height={6} />
            </div>
            <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.dim, marginBottom: 14 }}>
              {openBelief.source_agent} · 更新 {(openBelief.last_updated ?? '').slice(0, 16).replace('T', ' ')}
            </div>
            {openBelief.rationale && (
              <div style={{ marginBottom: 12 }}>
                <div style={{ fontFamily: OPS.mono, fontSize: 11, color: OPS.gold, letterSpacing: '0.1em', marginBottom: 5 }}>根拠</div>
                <p style={{ fontSize: 13, color: OPS.sub, lineHeight: 1.8, margin: 0 }}>{openBelief.rationale}</p>
              </div>
            )}
          </>
        )}
      </Modal>
    </OpsPage>
  )
}

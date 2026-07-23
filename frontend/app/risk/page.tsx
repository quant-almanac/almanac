'use client'

import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, PanelTitle, Stat, Chip, Loading, Grid } from '@/components/today/ops/PageKit'
import DCAladderPanel from '@/components/DCAladderPanel'

interface RiskData {
  var_95?: number
  cvar_95?: number
  current_dd?: number
  max_dd?: number
  drawdown_series?: number[]
  sample_size?: number
  currency_exposure?: { total_jpy?: number; foreign_value_jpy?: number; foreign_ratio?: number }
  behavioral_bias?: { position_scale?: number; bias_type?: string; confidence_damper?: number }
  macro?: { vix?: number; vix_status?: string }
}

export default function RiskPage() {
  const { data, isLoading } = useSWR<RiskData>('/api/risk', fetcher, { refreshInterval: 300000 })

  return (
    <OpsPage
      en="RISK"
      title="リスク・ダッシュボード"
      subtitle="Cornish-Fisher VaR / CVaR、ドローダウン、通貨エクスポージャ、行動バイアス補正を一望する守りの計器盤。"
      right={data?.macro?.vix != null && <Chip color={OPS.blue} bg={OPS.blueBg} mono>VIX {data.macro.vix.toFixed(1)}</Chip>}
      widthMode="wide"
    >
      {isLoading && <Loading />}
      {data && (
        <>
          <Grid cols={4} gap={12}>
            <Stat label="VaR 95%（日次）" value={data.var_95?.toFixed(2) ?? '—'} unit="%" color={OPS.amber} sub={`sample ${data.sample_size ?? '—'}日`} />
            <Stat label="CVaR 95%（テール損失）" value={data.cvar_95?.toFixed(2) ?? '—'} unit="%" color={OPS.redSoft} />
            <Stat
              label="現在ドローダウン"
              value={data.current_dd?.toFixed(2) ?? '—'}
              unit="%"
              color={data.current_dd != null && data.current_dd <= -8 ? OPS.redSoft : OPS.text}
            />
            <Stat label="最大DD（観測期間）" value={data.max_dd?.toFixed(2) ?? '—'} unit="%" />
          </Grid>

          {data.drawdown_series && data.drawdown_series.length > 1 && (
            <div style={{ marginTop: 22 }}>
              <Panel pad="16px 18px">
                <PanelTitle right={`${data.drawdown_series.length}日`}>ドローダウン推移</PanelTitle>
                <DDChart series={data.drawdown_series} />
              </Panel>
            </div>
          )}

          <Grid cols={2} gap={16}>
            <div style={{ marginTop: 16 }}>
              <Panel pad="16px 18px">
                <PanelTitle>通貨エクスポージャ</PanelTitle>
                {data.currency_exposure && (
                  <>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }}>
                      <span style={{ fontFamily: OPS.mono, fontSize: 22, color: OPS.blue }}>
                        {((data.currency_exposure.foreign_ratio ?? 0) * 100).toFixed(1)}%
                      </span>
                      <span style={{ fontSize: 12, color: OPS.dim }}>外貨比率</span>
                    </div>
                    <div style={{ display: 'flex', height: 8, borderRadius: 4, overflow: 'hidden', marginBottom: 8 }}>
                      <div style={{ width: `${(data.currency_exposure.foreign_ratio ?? 0) * 100}%`, background: OPS.blue }} />
                      <div style={{ flex: 1, background: OPS.gold }} />
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: OPS.mono, fontSize: 11.5, color: OPS.sub }}>
                      <span style={{ color: OPS.blue }}>外貨 ¥{Math.round((data.currency_exposure.foreign_value_jpy ?? 0) / 10000).toLocaleString()}万</span>
                      <span style={{ color: OPS.gold }}>円資産</span>
                    </div>
                  </>
                )}
              </Panel>
            </div>

            <div style={{ marginTop: 16 }}>
              <Panel pad="16px 18px">
                <PanelTitle>行動ガード補正</PanelTitle>
                {data.behavioral_bias && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                      <span style={{ fontFamily: OPS.mono, fontSize: 22, color: OPS.gold }}>
                        ×{data.behavioral_bias.position_scale ?? 1}
                      </span>
                      <span style={{ fontSize: 12, color: OPS.dim }}>ポジションスケール</span>
                    </div>
                    {data.behavioral_bias.bias_type && (
                      <div>
                        <Chip color={OPS.amber} bg={OPS.amberBg}>{data.behavioral_bias.bias_type} 検知</Chip>
                      </div>
                    )}
                    {data.behavioral_bias.confidence_damper != null && (
                      <div style={{ fontFamily: OPS.mono, fontSize: 12, color: OPS.sub }}>
                        確信度ダンパー {data.behavioral_bias.confidence_damper}
                      </div>
                    )}
                    <p style={{ fontSize: 11.5, color: OPS.dim, lineHeight: 1.6, margin: 0 }}>
                      過信・狼狽を検知するとポジションサイズを自動的に縮小する行動ファイナンスの補正。
                    </p>
                  </div>
                )}
              </Panel>
            </div>
          </Grid>
          <div style={{ marginTop: 16 }}><Panel pad="16px 18px"><PanelTitle>DCA LADDER · 底打ち買い下がり</PanelTitle><DCAladderPanel /></Panel></div>
        </>
      )}
    </OpsPage>
  )
}

function DDChart({ series }: { series: number[] }) {
  const W = 900
  const H = 140
  const PAD = { l: 4, r: 44, t: 10, b: 16 }
  const min = Math.min(...series, 0)
  const max = 0
  const range = min - max || -1
  const toX = (i: number) => PAD.l + (i / (series.length - 1)) * (W - PAD.l - PAD.r)
  const toY = (v: number) => PAD.t + ((v - max) / range) * (H - PAD.t - PAD.b)
  const line = series.map((v, i) => `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join('')
  const area = `${line}L${toX(series.length - 1).toFixed(1)},${toY(0)}L${toX(0).toFixed(1)},${toY(0)}Z`
  const cur = series[series.length - 1]
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }} aria-label="ドローダウン推移">
      <line x1={PAD.l} y1={toY(0)} x2={W - PAD.r} y2={toY(0)} stroke={OPS.border} strokeWidth={1} />
      <text x={W - PAD.r + 5} y={toY(0) + 3} fontSize={10} fill={OPS.dim} fontFamily={OPS.mono}>0%</text>
      <text x={W - PAD.r + 5} y={toY(min) + 3} fontSize={10} fill={OPS.redSoft} fontFamily={OPS.mono}>{min.toFixed(1)}%</text>
      <path d={area} fill={OPS.redSoft} opacity={0.12} />
      <path d={line} stroke={OPS.redSoft} strokeWidth={1.5} fill="none" />
      <circle cx={toX(series.length - 1)} cy={toY(cur)} r={2.6} fill={OPS.redSoft} />
    </svg>
  )
}

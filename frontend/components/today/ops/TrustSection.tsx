'use client'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS, STATUS_META, TYPE_META, fmtJpy } from './tokens'
import { SectionHead } from './Shell'
import type { Scorecard, Allocation } from './types'

/**
 * 三、成績と資金はどうなっているか — 自己計測の成績表と資金・リスク状態を1面に。
 */
export default function TrustSection({
  scorecard,
  allocation,
}: {
  scorecard: Scorecard
  allocation: Allocation
}) {
  const { data: risk } = useSWR<RiskData>('/api/risk', fetcher, { refreshInterval: 300000 })
  const main = scorecard.rows[0]
  const c = allocation.currency
  const hotVol = Object.entries(allocation.ginn_vol)
    .filter(([, v]) => v >= 80)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)

  return (
    <section>
      <SectionHead
        no="06"
        en="LEDGER"
        jp="成績・資金"
        note={`自己計測 · ${scorecard.horizon_days ?? '—'}日ホライズン`}
      />

      {/* 成績の大きな数字 */}
      {main ? (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12 }}>
            <BigNum label="判定対象" value={main.n != null ? `${main.n}` : '—'} unit="件" />
            <BigNum
              label="勝率"
              value={main.win_rate != null ? `${Math.round(main.win_rate * 100)}` : '—'}
              unit="%"
            />
            <BigNum
              label="超過リターン"
              value={
                main.excess_bps != null
                  ? `${main.excess_bps >= 0 ? '+' : ''}${main.excess_bps.toFixed(1)}`
                  : '—'
              }
              unit="bps"
              color={main.excess_bps != null && main.excess_bps >= 0 ? OPS.green : OPS.redSoft}
            />
            <BigNum label="ペイオフ比" value={main.payoff != null ? main.payoff.toFixed(2) : '—'} unit="" />
          </div>
          <p style={{ fontSize: 12, color: OPS.dim, margin: '8px 0 0' }}>
            {main.agent} / {main.role} · 実測 {main.measured_n ?? 0} 件
            {!main.measured && ' · 計測数不足のため重み未適用'} · 実測値・無加工
          </p>
        </>
      ) : (
        <p style={{ fontSize: 12, color: OPS.dim }}>計測データがまだありません。</p>
      )}

      {/* リスク指標（/api/risk 統合） */}
      {risk && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginTop: 18 }}>
            <BigNum label="VaR 95%（日次）" value={risk.var_95 != null ? risk.var_95.toFixed(2) : '—'} unit="%" />
            <BigNum label="CVaR 95%" value={risk.cvar_95 != null ? risk.cvar_95.toFixed(2) : '—'} unit="%" />
            <BigNum
              label="現在ドローダウン"
              value={risk.current_dd != null ? risk.current_dd.toFixed(2) : '—'}
              unit="%"
              color={risk.current_dd != null && risk.current_dd <= -8 ? OPS.redSoft : OPS.text}
            />
            <BigNum
              label="最大ドローダウン（90日）"
              value={risk.max_dd != null ? risk.max_dd.toFixed(2) : '—'}
              unit="%"
            />
          </div>
          {risk.behavioral_bias?.bias_type && (
            <p style={{ fontSize: 12, color: OPS.dim, margin: '8px 0 0' }}>
              行動ガード: {risk.behavioral_bias.bias_type} 検知 · ポジションスケール ×
              {risk.behavioral_bias.position_scale ?? 1}
            </p>
          )}
        </>
      )}

      <div
        style={{
          display: 'grid',
          // minmax(0,1fr): nowrap な約定詳細がトラックの auto 最小幅を押し広げるのを防ぐ。
          // 狭幅では auto-fit で1カラムに折り返す。
          gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 340px), 1fr))',
          gap: 28,
          marginTop: 24,
        }}
      >
        {/* 左: 執行の記録 */}
        <div>
          <ColTitle>執行の記録</ColTitle>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 14px', fontFamily: OPS.mono, fontSize: 12, color: OPS.sub, marginBottom: 10 }}>
            {Object.entries(scorecard.status_counts).map(([k, v]) => {
              const m = STATUS_META[k] ?? { label: k, color: OPS.sub }
              return (
                <span key={k}>
                  <span style={{ color: m.color }}>●</span> {m.label} {v}
                </span>
              )
            })}
          </div>
          <div>
            {scorecard.recent_fills.map((f, i) => {
              const type = f.action_type ? TYPE_META[f.action_type] : null
              return (
                <div
                  key={i}
                  style={{
                    display: 'flex',
                    alignItems: 'baseline',
                    gap: 10,
                    padding: '6px 0',
                    borderTop: `1px solid ${OPS.hairline}`,
                    fontSize: 12.5,
                  }}
                >
                  <span style={{ fontFamily: OPS.mono, color: OPS.dim, minWidth: 72 }}>{fmtDate(f.filled_at)}</span>
                  <span style={{ fontFamily: OPS.mono, color: OPS.text, fontWeight: 500, minWidth: 50 }}>
                    {f.ticker}
                  </span>
                  {type && <span style={{ color: type.color, minWidth: 52, flexShrink: 0 }}>{type.label}</span>}
                  <span
                    style={{
                      color: OPS.sub,
                      flex: 1,
                      minWidth: 0,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {f.detail}
                  </span>
                </div>
              )
            })}
            {scorecard.recent_fills.length === 0 && (
              <p style={{ fontSize: 12, color: OPS.dim }}>約定履歴なし</p>
            )}
          </div>
        </div>

        {/* 右: 資金と守り */}
        <div>
          <ColTitle>資金と守り</ColTitle>

          {c.current_usd_pct != null && c.usd_target_pct != null && (
            <div style={{ marginBottom: 14 }}>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  fontFamily: OPS.mono,
                  fontSize: 12,
                  marginBottom: 5,
                }}
              >
                <span style={{ color: OPS.text }}>USD {c.current_usd_pct.toFixed(1)}%</span>
                <div style={{ flex: 1, height: 5, background: OPS.hairline, borderRadius: 3, position: 'relative' }}>
                  <div
                    style={{
                      width: `${Math.min(100, c.current_usd_pct)}%`,
                      height: '100%',
                      background: OPS.blue,
                      borderRadius: 3,
                    }}
                  />
                  <div
                    title={`目標 ${c.usd_target_pct}%`}
                    style={{
                      position: 'absolute',
                      left: `${c.usd_target_pct}%`,
                      top: -3,
                      width: 2,
                      height: 11,
                      background: OPS.gold,
                    }}
                  />
                </div>
                <span style={{ color: OPS.gold }}>目標 {c.usd_target_pct}%</span>
              </div>
              {c.review_triggers.length > 0 && (
                <p style={{ fontSize: 11.5, color: OPS.dim, margin: 0, lineHeight: 1.6 }}>
                  見直しトリガー: {c.review_triggers.join(' / ')}
                </p>
              )}
            </div>
          )}

          {/* NISA 枠残 */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 20px', fontFamily: OPS.mono, fontSize: 12.5, color: OPS.sub, marginBottom: 14 }}>
            <span>
              <span style={{ color: OPS.dim }}>NISA 本人</span> 成長 {fmtJpy(allocation.nisa.husband.growth_remaining)} / 積立{' '}
              {fmtJpy(allocation.nisa.husband.tsumitate_remaining)}
            </span>
            <span>
              <span style={{ color: OPS.dim }}>妻</span> 成長 {fmtJpy(allocation.nisa.wife.growth_remaining)} / 積立{' '}
              {fmtJpy(allocation.nisa.wife.tsumitate_remaining)}
            </span>
          </div>
          <p style={{ fontSize: 11.5, color: OPS.dim, margin: '0 0 14px', lineHeight: 1.6 }}>
            NISA基準日 {allocation.nisa.husband.baseline ?? '—'}
            {allocation.nisa.husband.age_days != null && ` · ${allocation.nisa.husband.age_days}日前`}
            {(allocation.nisa.husband.unattributed_count ?? 0) + (allocation.nisa.wife.unattributed_count ?? 0) > 0 && (
              ` · 未帰属 ${(allocation.nisa.husband.unattributed_count ?? 0) + (allocation.nisa.wife.unattributed_count ?? 0)}件`
            )}
          </p>

          {/* リスク警告（上位3） */}
          {allocation.risk_warnings.slice(0, 3).map((w, i) => (
            <p key={i} style={{ fontSize: 12, color: OPS.sub, lineHeight: 1.65, margin: '0 0 6px', display: 'flex', gap: 7 }}>
              <span style={{ color: OPS.redSoft, flexShrink: 0 }}>▪</span>
              <span>{w}</span>
            </p>
          ))}
          {allocation.risk_warnings.length > 3 && (
            <p style={{ fontSize: 11.5, color: OPS.dim, margin: '0 0 6px' }}>
              ほか {allocation.risk_warnings.length - 3} 件のリスク警告（全文はレポート参照）
            </p>
          )}

          {/* ストップロス + 高ボラ */}
          <p style={{ fontSize: 12, color: OPS.dim, margin: '10px 0 0', lineHeight: 1.7 }}>
            ストップロス監視 {allocation.stop_loss_alerts.length} 銘柄
            {hotVol.length > 0 && (
              <>
                {' '}· 高ボラ警戒{' '}
                <span style={{ fontFamily: OPS.mono, color: OPS.redSoft }}>
                  {hotVol.map(([t]) => t).join(' ')}
                </span>
              </>
            )}
            {allocation.margin_health && (
              <>
                {' '}· 信用{' '}
                <span style={{ color: allocation.margin_health === 'safe' ? OPS.green : OPS.amber }}>
                  {allocation.margin_health}
                </span>
              </>
            )}
          </p>
        </div>
      </div>
    </section>
  )
}

function BigNum({
  label,
  value,
  unit,
  color,
}: {
  label: string
  value: string
  unit: string
  color?: string
}) {
  return (
    <div>
      <div style={{ fontSize: 12, color: OPS.dim, marginBottom: 4 }}>{label}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
        <span
          style={{
            fontFamily: OPS.mono,
            fontSize: 30,
            fontWeight: 500,
            color: color ?? OPS.text,
            letterSpacing: '-0.02em',
            lineHeight: 1,
          }}
        >
          {value}
        </span>
        {unit && <span style={{ fontSize: 12, color: OPS.dim }}>{unit}</span>}
      </div>
    </div>
  )
}

function ColTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3
      style={{
        fontFamily: OPS.display,
        fontSize: 14,
        fontWeight: 600,
        color: OPS.text,
        margin: '0 0 10px',
        letterSpacing: '0.04em',
      }}
    >
      {children}
    </h3>
  )
}

function fmtDate(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s.slice(5, 16)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

interface RiskData {
  var_95?: number
  cvar_95?: number
  current_dd?: number
  max_dd?: number
  behavioral_bias?: { bias_type?: string; position_scale?: number }
}

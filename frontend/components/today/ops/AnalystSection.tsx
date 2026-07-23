'use client'
import { useState } from 'react'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import { OPS } from './tokens'
import { SectionHead } from './Shell'
import type { TierReport, RedTeamAttack } from './types'

const TIER_TABS: { key: string; label: string }[] = [
  { key: 'synthesis', label: '総合 (Opus)' },
  { key: 'long', label: 'Long' },
  { key: 'medium', label: 'Medium' },
  { key: 'margin_long', label: '信用買い' },
  { key: 'short_selling', label: '空売り' },
  { key: 'short_positions', label: 'ショート玉' },
]

const FIELD_LABEL: Record<string, string> = {
  health: '健全性',
  health_reason: '健全性の理由',
  summary: 'サマリー',
  overall_stance: 'スタンス',
  stance_reason: 'スタンス理由',
  weekly_theme: '今週のテーマ',
  nisa_strategy: 'NISA戦略',
  news_impact: 'ニュース影響',
  signals_quality: 'シグナル品質',
  risk_warnings: 'リスク警告',
  stop_loss_alerts: 'ストップロス',
  hold_notes: '保有銘柄ノート',
  new_candidates: '新規候補',
  new_entries: '新規エントリー',
  profit_taking: '利確方針',
  opportunity_highlights: '注目機会',
  high_return_opportunity: '高リターン機会',
  medium_high_return_strategy: '中期高リターン戦略',
  watchlist_alert: 'ウォッチリスト警戒',
  geopolitical_note: '地政学ノート',
  crisis_strategy: '危機時戦略',
  loss_management: '損失管理',
  recovery_scenario: '回復シナリオ',
  optimization_insight: '最適化インサイト',
  rebalance_summary: 'リバランス概要',
  short_not_recommended: '空売り不実施の理由',
  margin_health: '信用健全性',
  margin_summary: '信用サマリー',
  model_used: '使用モデル',
}
const FIELD_ORDER = Object.keys(FIELD_LABEL)

/**
 * ANALYST — 分析全文。折りたたみから昇格した常設セクション。
 * ティア別レポート + Red Team 対案 + スクリーニング結果を1つのタブ群で。
 */
export default function AnalystSection({
  report,
  attacks,
}: {
  report: Record<string, TierReport>
  attacks: RedTeamAttack[]
}) {
  const tabs = [
    ...TIER_TABS.filter(t => report[t.key] && Object.keys(report[t.key]).length > 0),
    ...(attacks.length > 0 ? [{ key: '_redteam', label: `Red Team 対案 ${attacks.length}` }] : []),
    { key: '_screening', label: 'スクリーニング' },
  ]
  const [active, setActive] = useState(tabs[0]?.key ?? 'synthesis')

  return (
    <section>
      <SectionHead no="04" en="ANALYST" jp="分析全文" note="Sonnet 並列分析 + Opus 合成の原文" />

      <div style={{ display: 'flex', gap: 4, marginBottom: 16, flexWrap: 'wrap' }}>
        {tabs.map(t => {
          const on = t.key === active
          return (
            <button
              key={t.key}
              onClick={() => setActive(t.key)}
              style={{
                background: on ? OPS.goldBg : 'transparent',
                border: `1px solid ${on ? OPS.gold + '66' : OPS.hairline}`,
                borderRadius: 5,
                color: on ? OPS.gold : OPS.sub,
                fontSize: 13,
                padding: '5px 14px',
                cursor: 'pointer',
                fontFamily: OPS.sans,
                transition: 'border-color .15s ease, color .15s ease',
              }}
            >
              {t.label}
            </button>
          )
        })}
      </div>

      {active === '_redteam' ? (
        <AttackGrid attacks={attacks} />
      ) : active === '_screening' ? (
        <ScreeningTab />
      ) : (
        <TierView tier={report[active] ?? {}} />
      )}
    </section>
  )
}

/* ── ティア別レポート ───────────────────────────────────── */

function TierView({ tier }: { tier: TierReport }) {
  const fields = FIELD_ORDER.filter(k => {
    const v = tier[k]
    if (v == null || v === '') return false
    if (Array.isArray(v) && v.length === 0) return false
    return true
  })
  if (fields.length === 0) {
    return <p style={{ fontSize: 13, color: OPS.dim }}>このレーンの分析はありません。</p>
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {fields.map(k => (
        <div key={k}>
          <div
            style={{
              fontSize: 12,
              color: OPS.gold,
              fontFamily: OPS.mono,
              letterSpacing: '0.12em',
              marginBottom: 5,
              fontWeight: 600,
            }}
          >
            {FIELD_LABEL[k]}
          </div>
          <ValueView value={tier[k]} />
        </div>
      ))}
    </div>
  )
}

function ValueView({ value }: { value: unknown }) {
  if (typeof value === 'string' || typeof value === 'number') {
    return <div style={{ fontSize: 13.5, color: OPS.sub, lineHeight: 1.85 }}>{String(value)}</div>
  }
  if (Array.isArray(value)) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {value.map((item, i) => (
          <div key={i} style={{ fontSize: 13, color: OPS.sub, lineHeight: 1.75, display: 'flex', gap: 9 }}>
            <span style={{ color: OPS.dim, flexShrink: 0 }}>·</span>
            <ItemView item={item} />
          </div>
        ))}
      </div>
    )
  }
  if (value && typeof value === 'object') {
    return (
      <div style={{ fontSize: 13, color: OPS.sub, lineHeight: 1.75 }}>
        {Object.entries(value as Record<string, unknown>)
          .filter(([, v]) => typeof v === 'string' || typeof v === 'number')
          .map(([k, v]) => (
            <div key={k}>
              <span style={{ color: OPS.dim, fontFamily: OPS.mono, fontSize: 12 }}>{k}: </span>
              {String(v)}
            </div>
          ))}
      </div>
    )
  }
  return null
}

function ItemView({ item }: { item: unknown }) {
  if (typeof item === 'string') return <span>{item}</span>
  if (item && typeof item === 'object') {
    const o = item as Record<string, unknown>
    const ticker = typeof o.ticker === 'string' ? o.ticker : null
    const reason = typeof o.reason === 'string' ? o.reason : null
    const score = typeof o.score === 'number' ? o.score : null
    const rest = !ticker && !reason ? JSON.stringify(o) : null
    return (
      <span>
        {ticker && (
          <span style={{ fontFamily: OPS.mono, color: OPS.text, fontWeight: 500, marginRight: 6 }}>{ticker}</span>
        )}
        {score != null && (
          <span style={{ fontFamily: OPS.mono, color: OPS.gold, marginRight: 6 }}>score {score}</span>
        )}
        {reason}
        {rest}
      </span>
    )
  }
  return <span>{String(item)}</span>
}

/* ── Red Team 対案 ──────────────────────────────────────── */

function AttackGrid({ attacks }: { attacks: RedTeamAttack[] }) {
  return (
    <>
      <p style={{ fontSize: 12.5, color: OPS.dim, margin: '0 0 12px' }}>
        Haiku 並列生成の攻撃案。“違う私ならこう動く”の対案で、採否は SIGNAL MAP の判定列を参照。
      </p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 10 }}>
        {attacks.map((a, i) => (
          <div
            key={i}
            className="ops-card"
            style={{
              background: OPS.panel,
              border: `1px solid ${OPS.hairline}`,
              borderRadius: 8,
              padding: '11px 13px',
              fontSize: 12.5,
              lineHeight: 1.7,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 3 }}>
              <span style={{ fontFamily: OPS.mono, color: OPS.text, fontWeight: 500 }}>{a.ticker}</span>
              {a.expected_return_pct != null && (
                <span style={{ fontFamily: OPS.mono, color: OPS.green }}>+{a.expected_return_pct}%期待</span>
              )}
              {a.model && (
                <span style={{ marginLeft: 'auto', color: OPS.dim, fontFamily: OPS.mono, fontSize: 11 }}>
                  {a.model}
                </span>
              )}
            </div>
            <div style={{ color: OPS.sub }}>{a.action}</div>
            <div style={{ color: OPS.dim, marginTop: 2 }}>{a.rationale}</div>
            {a.risk_note && <div style={{ color: OPS.redSoft, marginTop: 2 }}>⚠ {a.risk_note}</div>}
          </div>
        ))}
      </div>
    </>
  )
}

/* ── スクリーニング（/api/screening 統合）──────────────────── */

interface ScreenItem {
  ticker?: string
  name?: string
  sector?: string
  currency?: string
  price?: number
  roe?: number
  eps_growth?: number
  rev_growth?: number
  net_margin?: number
}

interface ScreeningData {
  long_term?: {
    passed?: ScreenItem[]
    total_screened?: number
    rejected_count?: number
    as_of?: string
  }
  optimization?: { recommended?: string; regime?: string; as_of?: string }
  short_term?: { candidates?: unknown[]; regime?: string; vix_blocked?: boolean; as_of?: string }
}

function ScreeningTab() {
  const { data, isLoading } = useSWR<ScreeningData>('/api/screening', fetcher)
  if (isLoading) return <p style={{ fontSize: 13, color: OPS.dim }}>スクリーニング結果を取得中…</p>
  const lt = data?.long_term
  const passed = lt?.passed ?? []

  return (
    <div>
      <p style={{ fontSize: 12.5, color: OPS.dim, margin: '0 0 12px', fontFamily: OPS.mono }}>
        長期スクリーニング {lt?.as_of ?? '—'} · 対象 {lt?.total_screened ?? '—'} → 通過 {passed.length} / 却下{' '}
        {lt?.rejected_count ?? '—'}
        {data?.optimization?.recommended && (
          <> · 最適化推奨 {data.optimization.recommended}（{data.optimization.regime}）</>
        )}
        {data?.short_term && <> · 短期候補 {data.short_term.candidates?.length ?? 0} 件</>}
      </p>
      {passed.length === 0 ? (
        <p style={{ fontSize: 13, color: OPS.dim }}>通過銘柄なし。</p>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ color: OPS.dim, fontSize: 12, textAlign: 'left' }}>
              <th style={TH}>銘柄</th>
              <th style={TH}>セクター</th>
              <th style={{ ...TH, textAlign: 'right' }}>株価</th>
              <th style={{ ...TH, textAlign: 'right' }}>ROE</th>
              <th style={{ ...TH, textAlign: 'right' }}>EPS成長</th>
              <th style={{ ...TH, textAlign: 'right' }}>売上成長</th>
              <th style={{ ...TH, textAlign: 'right' }}>純利益率</th>
            </tr>
          </thead>
          <tbody>
            {passed.map((p, i) => (
              <tr key={`${p.ticker}-${i}`} className="ops-row" style={{ borderTop: `1px solid ${OPS.hairline}` }}>
                <td style={TD}>
                  <span style={{ fontFamily: OPS.mono, fontWeight: 500, color: OPS.text }}>{p.ticker}</span>
                  <span style={{ color: OPS.dim, fontSize: 11.5, marginLeft: 8 }}>{p.name}</span>
                </td>
                <td style={{ ...TD, color: OPS.sub, fontSize: 12 }}>{p.sector}</td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: OPS.mono, color: OPS.sub }}>
                  {p.price != null ? p.price.toLocaleString() : '—'}
                </td>
                <PctCell v={p.roe} />
                <PctCell v={p.eps_growth} />
                <PctCell v={p.rev_growth} />
                <PctCell v={p.net_margin} />
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function PctCell({ v }: { v?: number }) {
  if (v == null) return <td style={{ ...TD, textAlign: 'right', color: OPS.dim }}>—</td>
  const pct = v * 100
  return (
    <td
      style={{
        ...TD,
        textAlign: 'right',
        fontFamily: OPS.mono,
        color: pct >= 0 ? OPS.green : OPS.redSoft,
      }}
    >
      {pct.toFixed(1)}%
    </td>
  )
}

const TH: React.CSSProperties = { padding: '5px 8px', fontWeight: 400 }
const TD: React.CSSProperties = { padding: '7px 8px', verticalAlign: 'middle' }

'use client'

import { useState } from 'react'
import useSWR from 'swr'
import { fetcher } from '@/lib/api'
import type { MarginData, MarginPosition } from '@/lib/api'
import Link from 'next/link'
import { OpsPage, Panel, PanelTitle } from '@/components/today/ops/PageKit'
import { OPS } from '@/components/today/ops/tokens'

const MARGIN_STATUS_COLOR: Record<string, string> = {
  safe: OPS.green, caution: OPS.amber, warning: OPS.vermilion, emergency: OPS.vermilion,
}
const MARGIN_STATUS_LABEL: Record<string, string> = {
  safe: '安全', caution: '注意', warning: '警戒', emergency: '緊急',
}

// ── 建玉・証拠金パネル ───────────────────────────────
function MarginPositionPanel() {
  const { data, isLoading } = useSWR<MarginData>('/api/margin', fetcher, { revalidateOnFocus: false })

  if (isLoading) return <p style={{ color: OPS.sub, fontSize: 14 }}>建玉データ読み込み中…</p>

  const margin = data ?? {
    open_positions: [], closed_positions: [], collateral: 0,
    maintenance_ratio: Infinity, margin_status: 'safe' as const,
    total_unrealized: 0, total_realized: 0, expiry_alerts: [],
    fx_usdjpy: 150, as_of: '',
  }

  const statusColor = MARGIN_STATUS_COLOR[margin.margin_status] ?? OPS.green
  const statusLabel = MARGIN_STATUS_LABEL[margin.margin_status] ?? '安全'
  const ratioDisplay = margin.maintenance_ratio == null || margin.maintenance_ratio > 9999
    ? '∞'
    : `${margin.maintenance_ratio.toFixed(1)}%`

  return (
    <div>
      {/* 証拠金サマリー */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginBottom: 20 }}>
        {[
          { label: '証拠金維持率', value: ratioDisplay, color: statusColor },
          { label: 'ステータス', value: statusLabel, color: statusColor },
          { label: '委託保証金', value: `¥${margin.collateral.toLocaleString('ja-JP')}`, color: OPS.text },
          { label: '含み損益合計', value: `${margin.total_unrealized >= 0 ? '+' : ''}¥${Math.round(margin.total_unrealized).toLocaleString('ja-JP')}`, color: margin.total_unrealized >= 0 ? OPS.green : OPS.vermilion },
        ].map(({ label, value, color }) => (
          <div key={label} style={{ background: OPS.panelAlt, border: `1px solid ${OPS.border}`, borderRadius: 10, padding: '12px 16px' }}>
            <p style={{ color: OPS.sub, fontSize: 14, marginBottom: 4 }}>{label}</p>
            <p style={{ color, fontWeight: 700, fontSize: 18 }}>{value}</p>
          </div>
        ))}
      </div>

      {/* 期日アラート */}
      {margin.expiry_alerts.length > 0 && (
        <div style={{ marginBottom: 16, padding: 12, borderRadius: 8, background: OPS.vermilionBg, border: `1px solid ${OPS.vermilion}33` }}>
          <p style={{ color: OPS.vermilion, fontSize: 14, fontWeight: 600, marginBottom: 6 }}>⚠️ 期日アラート</p>
          {margin.expiry_alerts.map((a, i) => (
            <p key={i} style={{ color: OPS.sub, fontSize: 14 }}>
              {a.ticker} ({a.side === 'long' ? '買い' : '売り'}) — 期日 {a.expiry}（残 {a.days_left} 日）
            </p>
          ))}
        </div>
      )}

      {/* 建玉一覧 */}
      <div>
        <p style={{ color: OPS.gold, fontFamily: OPS.mono, fontSize: 12, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
          オープン建玉（{margin.open_positions.length}件）
        </p>
        {margin.open_positions.length === 0 ? (
          <p style={{ color: OPS.sub, fontSize: 14 }}>建玉なし</p>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', fontSize: 14, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: `1px solid ${OPS.border}` }}>
                  {['ティッカー', '方向', '株数', '建値', '現在値', '含み損益', '損益%', '期日'].map(h => (
                    <th key={h} style={{ textAlign: 'left', padding: '8px 12px 8px 0', color: OPS.sub, fontSize: 12, fontWeight: 600 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {margin.open_positions.map((pos: MarginPosition, i: number) => (
                  <tr
                    key={pos.id ?? i}
                    style={{ borderBottom: `1px solid ${OPS.hairline}` }}
                  >
                    <td style={{ padding: '8px 12px 8px 0', color: OPS.gold, fontFamily: OPS.mono, fontWeight: 700 }}>{pos.ticker}</td>
                    <td style={{ padding: '8px 12px 8px 0' }}>
                      <span style={{
                        fontSize: 14, fontWeight: 600, padding: '2px 7px', borderRadius: 4,
                        background: pos.side === 'long' ? OPS.greenBg : OPS.vermilionBg,
                        color: pos.side === 'long' ? OPS.green : OPS.vermilion,
                      }}>
                        {pos.side === 'long' ? '信用買い' : '空売り'}
                      </span>
                    </td>
                    <td style={{ padding: '8px 12px 8px 0', color: OPS.text }}>{pos.shares}</td>
                    <td style={{ padding: '8px 12px 8px 0', color: OPS.sub }}>
                      {pos.currency === 'JPY' ? `¥${pos.entry_price.toLocaleString('ja-JP')}` : `$${pos.entry_price.toFixed(2)}`}
                    </td>
                    <td style={{ padding: '8px 12px 8px 0', color: OPS.text }}>
                      {pos.current_price != null
                        ? pos.currency === 'JPY' ? `¥${pos.current_price.toLocaleString('ja-JP')}` : `$${pos.current_price.toFixed(2)}`
                        : '-'}
                    </td>
                    <td style={{ padding: '8px 12px 8px 0', color: (pos.unrealized_pnl_jpy ?? 0) >= 0 ? OPS.green : OPS.vermilion, fontWeight: 600 }}>
                      {pos.unrealized_pnl_jpy != null
                        ? `${pos.unrealized_pnl_jpy >= 0 ? '+' : ''}¥${Math.round(pos.unrealized_pnl_jpy).toLocaleString('ja-JP')}`
                        : '-'}
                    </td>
                    <td style={{ padding: '8px 12px 8px 0', color: (pos.pnl_pct ?? 0) >= 0 ? OPS.green : OPS.vermilion }}>
                      {pos.pnl_pct != null ? `${pos.pnl_pct >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(2)}%` : '-'}
                    </td>
                    <td style={{ padding: '8px 0', color: OPS.sub, fontSize: 14 }}>{pos.expiry ?? '無期限'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {margin.as_of && <p style={{ color: OPS.sub, fontSize: 14, marginTop: 10 }}>更新: {margin.as_of}</p>}
      </div>
    </div>
  )
}

// ── 信用買い候補パネル ───────────────────────────────
function MarginLongPanel() {
  const { data, isLoading } = useSWR('/api/screening', fetcher, { revalidateOnFocus: false })
  const marginLong = data?.margin_long

  if (isLoading) return <p style={{ color: OPS.sub, fontSize: 14 }}>データ読み込み中…</p>
  if (!marginLong) return <p style={{ color: OPS.sub, fontSize: 14 }}>データなし（margin_long_candidates.json 未生成）</p>
  if (marginLong.error && (!marginLong.candidates || marginLong.candidates.length === 0)) {
    return <p style={{ color: OPS.amber, fontSize: 14 }}>⚠️ {marginLong.error}</p>
  }

  const candidates: Record<string, unknown>[] = marginLong.candidates ?? []

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div>
          <span style={{ color: OPS.green, fontSize: 14, fontWeight: 600 }}>{candidates.length}銘柄</span>
          <span style={{ color: OPS.sub, fontSize: 14, marginLeft: 8 }}>信用買い候補</span>
        </div>
        {marginLong.as_of && <p style={{ color: OPS.sub, fontSize: 14 }}>更新: {marginLong.as_of}</p>}
      </div>

      {candidates.length === 0 ? (
        <p style={{ color: OPS.sub, fontSize: 14 }}>現在の候補なし</p>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', fontSize: 14, borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${OPS.border}` }}>
                {['ティッカー', '銘柄名', 'セクター', 'スコア', '理由', 'AI相談'].map(h => (
                  <th key={h} style={{ textAlign: 'left', padding: '8px 12px 8px 0', color: OPS.sub, fontSize: 12, fontWeight: 600 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {candidates.map((c, i) => (
                <tr
                  key={i}
                  style={{ borderBottom: `1px solid ${OPS.hairline}` }}
                >
                  <td style={{ padding: '8px 12px 8px 0', color: OPS.green, fontFamily: OPS.mono, fontWeight: 700 }}>{String(c.ticker ?? '-')}</td>
                  <td style={{ padding: '8px 12px 8px 0', color: OPS.text, maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {String(c.name ?? '-')}
                  </td>
                  <td style={{ padding: '8px 12px 8px 0' }}>
                    <span style={{ fontSize: 14, padding: '2px 7px', borderRadius: 4, background: OPS.greenBg, color: OPS.green }}>
                      {String(c.sector ?? '-')}
                    </span>
                  </td>
                  <td style={{ padding: '8px 12px 8px 0', color: OPS.amber, fontWeight: 700 }}>
                    {c.score != null ? Number(c.score).toFixed(0) : '-'}
                  </td>
                  <td style={{ padding: '8px 12px 8px 0', color: OPS.sub, maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {String(c.reason ?? '-')}
                  </td>
                  <td style={{ padding: '8px 0' }}>
                    <Link
                      href={`/decision?case=B&ticker=${encodeURIComponent(String(c.ticker ?? ''))}`}
                      style={{ textDecoration: 'none' }}
                    >
                      <span style={{
                        fontSize: 14, padding: '3px 8px', borderRadius: 6,
                        background: OPS.greenBg, border: `1px solid ${OPS.green}4d`,
                        color: OPS.green, cursor: 'pointer', whiteSpace: 'nowrap',
                      }}>
                        🤖 相談
                      </span>
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── 空売り候補パネル ─────────────────────────────────
function ShortCandidatePanel() {
  const { data, isLoading } = useSWR('/api/screening', fetcher, { revalidateOnFocus: false })
  const shortData = data?.short_term

  if (isLoading) return <p style={{ color: OPS.sub, fontSize: 14 }}>データ読み込み中…</p>
  if (!shortData) return <p style={{ color: OPS.sub, fontSize: 14 }}>データなし（short_candidates.json 未生成）</p>

  const candidates: Record<string, unknown>[] = shortData.candidates ?? []

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div>
          <span style={{ color: OPS.vermilion, fontSize: 14, fontWeight: 600 }}>{candidates.length}銘柄</span>
          <span style={{ color: OPS.sub, fontSize: 14, marginLeft: 8 }}>空売り候補</span>
          {shortData.regime && (
            <span style={{ marginLeft: 10, fontSize: 14, color: OPS.gold, background: OPS.goldBg, padding: '2px 8px', borderRadius: 4 }}>
              レジーム: {shortData.regime}
            </span>
          )}
        </div>
        {shortData.as_of && <p style={{ color: OPS.sub, fontSize: 14 }}>更新: {shortData.as_of}</p>}
      </div>

      {candidates.length === 0 ? (
        <p style={{ color: OPS.sub, fontSize: 14 }}>現在の候補なし（またはファイル未生成）</p>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', fontSize: 14, borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${OPS.border}` }}>
                {['ティッカー', 'RSI', 'MA50比', '理由', 'セクター', 'AI相談'].map(h => (
                  <th key={h} style={{ textAlign: 'left', padding: '8px 12px 8px 0', color: OPS.sub, fontSize: 12, fontWeight: 600 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {candidates.map((c, i) => (
                <tr
                  key={i}
                  style={{ borderBottom: `1px solid ${OPS.hairline}` }}
                >
                  <td style={{ padding: '8px 12px 8px 0', color: OPS.vermilion, fontFamily: OPS.mono, fontWeight: 700 }}>{String(c.ticker ?? '-')}</td>
                  <td style={{ padding: '8px 12px 8px 0', color: Number(c.rsi) >= 70 ? OPS.vermilion : OPS.sub }}>
                    {c.rsi != null ? Number(c.rsi).toFixed(1) : '-'}
                  </td>
                  <td style={{ padding: '8px 12px 8px 0', color: OPS.amber }}>
                    {c.ma50_pct != null ? `+${(Number(c.ma50_pct) * 100).toFixed(1)}%` : '-'}
                  </td>
                  <td style={{ padding: '8px 12px 8px 0', color: OPS.sub, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {String(c.reason ?? '-')}
                  </td>
                  <td style={{ padding: '8px 12px 8px 0', color: OPS.sub, fontSize: 14 }}>{String(c.sector ?? '-')}</td>
                  <td style={{ padding: '8px 0' }}>
                    <Link
                      href={`/decision?case=A&ticker=${encodeURIComponent(String(c.ticker ?? ''))}`}
                      style={{ textDecoration: 'none' }}
                    >
                      <span style={{
                        fontSize: 14, padding: '3px 8px', borderRadius: 6,
                        background: OPS.vermilionBg, border: `1px solid ${OPS.vermilion}4d`,
                        color: OPS.vermilion, cursor: 'pointer', whiteSpace: 'nowrap',
                      }}>
                        🤖 相談
                      </span>
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── メインページ ─────────────────────────────────────
type Tab = 'positions' | 'margin_long' | 'short'

export default function MarginPage() {
  const [activeTab, setActiveTab] = useState<Tab>('positions')

  const tabs: { key: Tab; label: string; icon: string; color: string }[] = [
    { key: 'positions',   label: '建玉・証拠金管理', icon: '📋', color: OPS.gold },
    { key: 'margin_long', label: '信用買い候補',      icon: '📈', color: OPS.green },
    { key: 'short',       label: '空売り候補',        icon: '📉', color: OPS.vermilion },
  ]

  const tabStyle = (tab: Tab) => {
    const t = tabs.find(x => x.key === tab)!
    const active = activeTab === tab
    return {
      padding: '7px 18px', borderRadius: 20, fontSize: 14, cursor: 'pointer', border: 'none',
      fontWeight: active ? 600 : 400,
      background: active ? `${t.color}20` : 'transparent',
      color: active ? t.color : OPS.sub,
      outline: active ? `1px solid ${t.color}40` : '1px solid transparent',
    }
  }

  return (
    <OpsPage en="MARGIN" title="信用・空売り" subtitle="建玉管理・証拠金監視・信用買い候補・空売り候補。" widthMode="wide">

      {/* タブ */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 20, flexWrap: 'wrap' }}>
        {tabs.map(t => (
          <button key={t.key} onClick={() => setActiveTab(t.key)} style={tabStyle(t.key)}>
            {t.icon} {t.label}
          </button>
        ))}
      </div>

      <Panel pad="16px 18px">
        <PanelTitle>{activeTab === 'positions' ? '建玉・証拠金管理' : activeTab === 'margin_long' ? '信用買い候補' : '空売り候補'}</PanelTitle>
        {activeTab === 'positions' && <MarginPositionPanel />}
        {activeTab === 'margin_long' && <MarginLongPanel />}
        {activeTab === 'short' && <ShortCandidatePanel />}
      </Panel>
    </OpsPage>
  )
}

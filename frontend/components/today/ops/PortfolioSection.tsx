'use client'
import { useState } from 'react'
import { OPS, fmtJpy } from './tokens'
import { SectionHead } from './Shell'
import Sparkline from './Sparkline'
import type { HoldingIntel, TodayPortfolioSnapshot } from './types'

interface Position {
  ticker?: string
  name?: string
  currency?: string
  shares?: number
  current_price?: number
  value_jpy?: number
  unrealized_jpy?: number
  unrealized_pct?: number
  investment_type?: string
  account?: string
}

const TIER_COLOR: Record<string, string> = {
  long: OPS.blue,
  medium: OPS.amber,
  swing: OPS.green,
}

/**
 * PORTFOLIO — 保有一覧（Home 統合）。
 * 行クリックで「その銘柄について AI が考えていること」を展開:
 * 保有ノート・ストップロス・GINN ボラ・30日チャート。
 */
export default function PortfolioSection({
  intel,
  holdingsCharts,
  portfolio,
}: {
  intel: Record<string, HoldingIntel>
  holdingsCharts?: Record<string, { d: string; c: number }[]>
  portfolio: TodayPortfolioSnapshot
}) {
  const [showAll, setShowAll] = useState(false)
  const [openKey, setOpenKey] = useState<string | null>(null)

  const positions = [...(portfolio?.positions ?? [])].sort((a, b) => (b.value_jpy ?? 0) - (a.value_jpy ?? 0))
  const total = portfolio?.total_jpy ?? 0
  const cash = portfolio?.cash_total_jpy ?? portfolio?.cash_jpy ?? 0
  const visible = showAll ? positions : positions.slice(0, 12)

  return (
    <section>
      <SectionHead
        no="05"
        en="PORTFOLIO"
        jp="保有"
        note={
          total
            ? `総資産 ¥${Math.round(total).toLocaleString()} · 現金 ${fmtJpy(cash)}（${((cash / total) * 100).toFixed(1)}%）· ${positions.length} 銘柄 · 行クリックで AI 見解`
            : ''
        }
      />

      {positions.length === 0 ? (
        <p style={{ fontSize: 13, color: OPS.dim }}>保有データ取得中…</p>
      ) : (
        <>
          <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', minWidth: 560, borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ color: OPS.dim, fontSize: 12, textAlign: 'left' }}>
                <th style={{ ...TH, width: 20 }}></th>
                <th style={TH}>銘柄</th>
                <th style={TH}>種別</th>
                <th style={TH}>口座</th>
                <th style={{ ...TH, textAlign: 'right' }}>数量</th>
                <th style={{ ...TH, textAlign: 'right' }}>評価額</th>
                <th style={{ ...TH, textAlign: 'right' }}>比率</th>
                <th style={{ ...TH, textAlign: 'right' }}>損益</th>
                <th style={{ ...TH, width: '16%' }}></th>
              </tr>
            </thead>
            <tbody>
              {visible.map((p, i) => {
                const key = `${p.ticker}-${p.account}-${i}`
                const t = p.ticker ?? ''
                const info = intel[t]
                const series = holdingsCharts?.[t]
                const hasIntel = Boolean(info?.note || info?.stop_loss || info?.ginn_vol != null || series)
                const open = openKey === key
                const w = total ? ((p.value_jpy ?? 0) / total) * 100 : 0
                const pnl = (p.unrealized_pct ?? 0) * 100
                const tierColor = TIER_COLOR[p.investment_type ?? ''] ?? OPS.dim
                return (
                  <RowPair
                    key={key}
                    p={p}
                    w={w}
                    pnl={pnl}
                    tierColor={tierColor}
                    info={info}
                    series={series}
                    hasIntel={hasIntel}
                    open={open}
                    onToggle={() => setOpenKey(open ? null : key)}
                  />
                )
              })}
            </tbody>
          </table>
          </div>
          {positions.length > 12 && (
            <button
              onClick={() => setShowAll(!showAll)}
              style={{
                background: 'none',
                border: 'none',
                padding: '10px 0 0',
                cursor: 'pointer',
                fontSize: 12.5,
                color: OPS.gold,
                fontFamily: OPS.mono,
              }}
            >
              {showAll ? '▴ 折りたたむ' : `▾ 残り ${positions.length - 12} 銘柄`}
            </button>
          )}
        </>
      )}
    </section>
  )
}

function RowPair({
  p,
  w,
  pnl,
  tierColor,
  info,
  series,
  hasIntel,
  open,
  onToggle,
}: {
  p: Position
  w: number
  pnl: number
  tierColor: string
  info?: HoldingIntel
  series?: { d: string; c: number }[]
  hasIntel: boolean
  open: boolean
  onToggle: () => void
}) {
  return (
    <>
      <tr
        className="ops-row"
        role={hasIntel ? 'button' : undefined}
        tabIndex={hasIntel ? 0 : undefined}
        aria-expanded={hasIntel ? open : undefined}
        onKeyDown={hasIntel ? event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onToggle() } } : undefined}
        onClick={hasIntel ? onToggle : undefined}
        style={{
          borderTop: `1px solid ${OPS.hairline}`,
          cursor: hasIntel ? 'pointer' : 'default',
          background: open ? 'rgba(201,167,93,0.05)' : undefined,
        }}
      >
        <td style={{ ...TD, color: OPS.dim, fontSize: 11 }}>{hasIntel ? (open ? '▾' : '▸') : ''}</td>
        <td style={TD}>
          <span style={{ fontFamily: OPS.mono, fontWeight: 500, color: OPS.text }}>{p.ticker}</span>
          <span style={{ color: OPS.dim, fontSize: 11.5, marginLeft: 8 }}>{p.name}</span>
          {info?.ginn_vol != null && info.ginn_vol >= 80 && (
            <span
              title={`GINN 予測ボラ percentile ${info.ginn_vol.toFixed(0)}`}
              style={{ color: OPS.redSoft, fontSize: 11, fontFamily: OPS.mono, marginLeft: 8 }}
            >
              vol{info.ginn_vol.toFixed(0)}
            </span>
          )}
        </td>
        <td style={{ ...TD, color: tierColor, fontSize: 12 }}>{p.investment_type}</td>
        <td style={{ ...TD, color: OPS.dim, fontSize: 11.5 }}>{p.account}</td>
        <td style={{ ...TD, textAlign: 'right', fontFamily: OPS.mono, color: OPS.sub }}>
          {p.shares != null ? p.shares.toLocaleString() : '—'}
        </td>
        <td style={{ ...TD, textAlign: 'right', fontFamily: OPS.mono, color: OPS.text }}>{fmtJpy(p.value_jpy)}</td>
        <td style={{ ...TD, textAlign: 'right', fontFamily: OPS.mono, color: OPS.sub }}>{w.toFixed(1)}%</td>
        <td
          style={{
            ...TD,
            textAlign: 'right',
            fontFamily: OPS.mono,
            fontWeight: 500,
            color: pnl >= 0 ? OPS.green : OPS.redSoft,
          }}
        >
          {pnl >= 0 ? '+' : ''}
          {pnl.toFixed(1)}%
        </td>
        <td style={{ ...TD, paddingLeft: 12 }}>
          <div style={{ height: 4, background: OPS.hairline, borderRadius: 2, overflow: 'hidden' }}>
            <div
              style={{
                width: `${Math.min(100, w * 5)}%`,
                height: '100%',
                background: tierColor,
                opacity: 0.7,
              }}
            />
          </div>
        </td>
      </tr>

      {open && (
        <tr style={{ background: OPS.inset }}>
          <td colSpan={9} style={{ padding: '14px 18px', borderLeft: `2px solid ${OPS.gold}88` }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 280px', gap: 24, alignItems: 'start' }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {info?.note ? (
                  <p style={{ fontSize: 13, color: OPS.sub, lineHeight: 1.85, margin: 0 }}>
                    <span
                      style={{
                        fontFamily: OPS.mono,
                        fontSize: 11,
                        color: OPS.gold,
                        letterSpacing: '0.12em',
                        marginRight: 10,
                        fontWeight: 600,
                      }}
                    >
                      AI 保有ノート
                    </span>
                    {info.note}
                  </p>
                ) : (
                  <p style={{ fontSize: 12, color: OPS.dim, margin: 0 }}>
                    今回の分析に個別ノートなし（問題があれば hold_notes に言及される）。
                  </p>
                )}
                {info?.stop_loss && (
                  <p style={{ fontSize: 12.5, color: OPS.amber, lineHeight: 1.7, margin: 0 }}>
                    <span style={{ fontFamily: OPS.mono, fontSize: 11, letterSpacing: '0.12em', marginRight: 10, fontWeight: 600 }}>
                      ストップロス
                    </span>
                    {info.stop_loss}
                  </p>
                )}
                {info?.ginn_vol != null && (
                  <p style={{ fontSize: 12, color: OPS.dim, margin: 0, fontFamily: OPS.mono }}>
                    GINN 予測ボラ percentile: {info.ginn_vol.toFixed(1)}
                    {info.ginn_vol >= 80 && <span style={{ color: OPS.redSoft }}> — 高ボラ警戒</span>}
                  </p>
                )}
              </div>
              <div>
                {series && series.length > 1 ? (
                  <>
                    <Sparkline series={series} height={64} />
                    <div style={{ fontSize: 10.5, color: OPS.dim, fontFamily: OPS.mono, marginTop: 3, textAlign: 'right' }}>
                      30日 · 終値 {series[series.length - 1].c.toLocaleString()}
                    </div>
                  </>
                ) : (
                  <div style={{ fontSize: 11, color: OPS.dim }}>価格系列なし（投信・MMF等）</div>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

const TH: React.CSSProperties = { padding: '5px 8px', fontWeight: 400 }
const TD: React.CSSProperties = { padding: '7px 8px', verticalAlign: 'middle' }

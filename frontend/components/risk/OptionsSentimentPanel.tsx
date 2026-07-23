'use client'

import useSWR from 'swr'
import { fetcher } from '@/lib/api'

interface OptionSignal {
  ticker: string
  expiry: string | null
  last_price: number | null
  atm_iv: number | null
  iv_rank: number | null      // null = 履歴不足
  skew_25d: number | null
  pcr_oi: number | null
  pcr_volume: number | null
  fetched_at: string | null
}

interface OptionsSentimentResponse {
  signals: OptionSignal[]
  as_of: string | null
}

function ivRankColor(rank: number | null): string {
  if (rank == null) return '#475569'
  if (rank > 70) return '#F87171'   // 過熱
  if (rank > 50) return '#FBBF24'
  if (rank < 30) return '#34D399'   // 平穏
  return '#A8B2C8'
}

function pcrColor(pcr: number | null): string {
  if (pcr == null) return '#475569'
  if (pcr > 1.2) return '#34D399'   // ベア過剰 → コントラリアン買い
  if (pcr < 0.5) return '#F87171'   // ブル過剰 → 警戒
  return '#A8B2C8'
}

export default function OptionsSentimentPanel() {
  const { data, isLoading } = useSWR<OptionsSentimentResponse>('/api/options_sentiment', fetcher, { refreshInterval: 60 * 60 * 1000 })

  const signals = data?.signals ?? []
  if (isLoading) {
    return (
      <div className="card mb-6">
        <h2 className="text-sm font-semibold uppercase tracking-widest mb-3" style={{ color: '#7E8BA8' }}>
          📊 オプション市場センチメント
        </h2>
        <p className="text-base" style={{ color: '#7E8BA8' }}>読み込み中…</p>
      </div>
    )
  }
  if (!signals.length) {
    return (
      <div className="card mb-6">
        <h2 className="text-sm font-semibold uppercase tracking-widest mb-3" style={{ color: '#7E8BA8' }}>
          📊 オプション市場センチメント
        </h2>
        <p className="text-base" style={{ color: '#7E8BA8' }}>
          データなし — <code>options_fetcher.py refresh</code> で取得してください。
        </p>
      </div>
    )
  }

  return (
    <div className="card mb-6">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <h2 className="text-sm font-semibold uppercase tracking-widest" style={{ color: '#7E8BA8' }}>
          📊 オプション市場センチメント
        </h2>
        {data?.as_of && (
          <span style={{ fontSize: 12, color: '#7E8BA8' }}>更新 {data.as_of.slice(0, 16)}</span>
        )}
      </div>

      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ color: '#7E8BA8', borderBottom: '1px solid #232839' }}>
              <th style={{ textAlign: 'left', padding: '6px 8px' }}>Ticker</th>
              <th style={{ textAlign: 'right', padding: '6px 8px' }}>ATM IV</th>
              <th style={{ textAlign: 'right', padding: '6px 8px' }}>IV Rank</th>
              <th style={{ textAlign: 'right', padding: '6px 8px' }}>25Δ Skew</th>
              <th style={{ textAlign: 'right', padding: '6px 8px' }}>PCR (OI)</th>
              <th style={{ textAlign: 'right', padding: '6px 8px' }}>PCR (Vol)</th>
              <th style={{ textAlign: 'left', padding: '6px 8px' }}>Expiry</th>
            </tr>
          </thead>
          <tbody>
            {signals.map(s => (
              <tr key={s.ticker} style={{ borderBottom: '1px solid #1A1E2C' }}>
                <td style={{ padding: '6px 8px', color: '#E4E8EF', fontWeight: 600 }}>{s.ticker}</td>
                <td style={{ padding: '6px 8px', textAlign: 'right', fontFamily: 'monospace', color: '#A8B2C8' }}>
                  {s.atm_iv != null ? (s.atm_iv * 100).toFixed(1) + '%' : '—'}
                </td>
                <td style={{ padding: '6px 8px', textAlign: 'right', fontFamily: 'monospace', color: ivRankColor(s.iv_rank), fontWeight: 700 }}>
                  {s.iv_rank != null ? s.iv_rank.toFixed(0) : <span style={{ color: '#475569' }}>履歴不足</span>}
                </td>
                <td style={{ padding: '6px 8px', textAlign: 'right', fontFamily: 'monospace', color: s.skew_25d != null && s.skew_25d > 0.05 ? '#FBBF24' : '#A8B2C8' }}>
                  {s.skew_25d != null ? s.skew_25d.toFixed(3) : '—'}
                </td>
                <td style={{ padding: '6px 8px', textAlign: 'right', fontFamily: 'monospace', color: pcrColor(s.pcr_oi) }}>
                  {s.pcr_oi != null ? s.pcr_oi.toFixed(2) : '—'}
                </td>
                <td style={{ padding: '6px 8px', textAlign: 'right', fontFamily: 'monospace', color: pcrColor(s.pcr_volume) }}>
                  {s.pcr_volume != null ? s.pcr_volume.toFixed(2) : '—'}
                </td>
                <td style={{ padding: '6px 8px', color: '#7E8BA8', fontSize: 12 }}>{s.expiry ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 12, display: 'flex', gap: 12, fontSize: 12, color: '#7E8BA8', flexWrap: 'wrap' }}>
        <span>🔴 IVR&gt;70 過熱</span>
        <span>🟢 IVR&lt;30 平穏</span>
        <span>🟢 PCR&gt;1.2 ベア過剰→反発期待</span>
        <span>🔴 PCR&lt;0.5 ブル過剰→警戒</span>
      </div>
    </div>
  )
}

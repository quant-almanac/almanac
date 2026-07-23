'use client'

import useSWR from 'swr'
import { fetcher } from '@/lib/api'

interface FactorRow {
  // factor_attribution.json の行スキーマ（ticker 別 OLS 結果）
  ticker?: string
  alpha?: number
  alpha_t?: number
  r_squared?: number
  betas?: Record<string, number>
  t_stats?: Record<string, number>
  factors?: Record<string, { beta?: number; t_stat?: number; economic_rationale?: boolean }>
  months?: number
}

interface FactorExposureResponse {
  positions?: FactorRow[]
  rows?: FactorRow[]
  portfolio_betas?: Record<string, number>
  as_of?: string
  // factor_attribution.py の出力が dict 形式の場合
  [key: string]: unknown
}

const FACTOR_LABELS: Record<string, string> = {
  MKT: '市場',
  SMB: 'サイズ',
  HML: 'バリュー',
  MOM: 'モメンタム',
  QMJ: 'クオリティ',
  LVOL: '低ボラ',
  BAB: 'BAB',
  FX: '為替',
}

const FACTOR_RATIONALE: Record<string, boolean> = {
  // 経済的因果ストーリーが学術的に支持されている因子
  MKT: true, SMB: true, HML: true, MOM: true, QMJ: true, LVOL: true, BAB: true, FX: true,
}

function betaColor(beta: number | undefined, t_stat: number | undefined): string {
  if (beta == null) return '#475569'
  // t-stat 1.96 未満はグレー（統計的有意性なし）
  if (t_stat == null || Math.abs(t_stat) < 1.96) return '#64748B'
  if (beta > 0.5) return '#34D399'
  if (beta > 0) return '#86EFAC'
  if (beta < -0.5) return '#F87171'
  return '#FBA74A'
}

function isFactorRowLike(value: unknown): value is FactorRow {
  if (!value || typeof value !== 'object') return false
  return 'betas' in value || 'factors' in value
}

export default function FactorExposurePanel() {
  const { data, isLoading } = useSWR<FactorExposureResponse>('/api/factor_exposure', fetcher, { refreshInterval: 24 * 60 * 60 * 1000 })

  if (isLoading) {
    return (
      <div className="card mb-6">
        <h2 className="text-sm font-semibold uppercase tracking-widest mb-3" style={{ color: '#7E8BA8' }}>
          🧬 ファクター暴露（MOM / BAB / QMJ）
        </h2>
        <p className="text-base" style={{ color: '#7E8BA8' }}>読み込み中…</p>
      </div>
    )
  }

  // factor_attribution.py の出力が ticker をキーにした dict の場合に対応
  let rows: FactorRow[] = []
  if (data) {
    if (Array.isArray(data.positions)) {
      rows = data.positions
    } else if (Array.isArray(data.rows)) {
      rows = data.rows
    } else {
      // dict[ticker -> result] 形式
      for (const [k, v] of Object.entries(data)) {
        if (k === 'as_of' || k === 'portfolio_betas') continue
        if (isFactorRowLike(v)) {
          rows.push({ ticker: k, ...v })
        }
      }
    }
  }

  if (!rows.length) {
    return (
      <div className="card mb-6">
        <h2 className="text-sm font-semibold uppercase tracking-widest mb-3" style={{ color: '#7E8BA8' }}>
          🧬 ファクター暴露（MOM / BAB / QMJ）
        </h2>
        <p className="text-base" style={{ color: '#7E8BA8' }}>
          データなし — <code>python factor_attribution.py run 36</code> で生成してください。
        </p>
      </div>
    )
  }

  // 表示する factor 列を集約
  const allFactors = new Set<string>()
  for (const r of rows) {
    if (r.betas) for (const k of Object.keys(r.betas)) allFactors.add(k)
    if (r.factors) for (const k of Object.keys(r.factors)) allFactors.add(k)
  }
  const factorOrder = ['MKT', 'MOM', 'BAB', 'QMJ', 'HML', 'SMB', 'LVOL', 'FX'].filter(f => allFactors.has(f))
  for (const f of allFactors) if (!factorOrder.includes(f)) factorOrder.push(f)

  function getBeta(r: FactorRow, f: string): { beta?: number; t?: number } {
    if (r.factors && r.factors[f]) {
      return { beta: r.factors[f].beta, t: r.factors[f].t_stat }
    }
    return { beta: r.betas?.[f], t: r.t_stats?.[f] }
  }

  return (
    <div className="card mb-6">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <h2 className="text-sm font-semibold uppercase tracking-widest" style={{ color: '#7E8BA8' }}>
          🧬 ファクター暴露（OLS 月次回帰）
        </h2>
        {data?.as_of && <span style={{ fontSize: 12, color: '#7E8BA8' }}>更新 {data.as_of.slice(0, 10)}</span>}
      </div>

      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ color: '#7E8BA8', borderBottom: '1px solid #232839' }}>
              <th style={{ textAlign: 'left', padding: '6px 8px' }}>Ticker</th>
              {factorOrder.map(f => (
                <th key={f} style={{ textAlign: 'right', padding: '6px 8px' }}>
                  {FACTOR_LABELS[f] ?? f}
                  {!FACTOR_RATIONALE[f] && <span title="経済的因果ストーリーなし" style={{ color: '#FBBF24', marginLeft: 3 }}>⚠</span>}
                </th>
              ))}
              <th style={{ textAlign: 'right', padding: '6px 8px' }}>R²</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.ticker ?? `row-${i}`} style={{ borderBottom: '1px solid #1A1E2C' }}>
                <td style={{ padding: '6px 8px', color: '#E4E8EF', fontWeight: 600 }}>{r.ticker}</td>
                {factorOrder.map(f => {
                  const { beta, t } = getBeta(r, f)
                  const sig = t != null && Math.abs(t) >= 1.96
                  return (
                    <td key={f} style={{ padding: '6px 8px', textAlign: 'right', fontFamily: 'monospace', color: betaColor(beta, t), opacity: sig ? 1 : 0.55 }}>
                      {beta != null ? beta.toFixed(2) : '—'}
                      {t != null && (
                        <span style={{ fontSize: 11, color: '#7E8BA8', marginLeft: 3 }}>(t={t.toFixed(1)})</span>
                      )}
                    </td>
                  )
                })}
                <td style={{ padding: '6px 8px', textAlign: 'right', fontFamily: 'monospace', color: '#A8B2C8' }}>
                  {r.r_squared != null ? r.r_squared.toFixed(2) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 12, fontSize: 12, color: '#7E8BA8' }}>
        <span style={{ marginRight: 12 }}>🟢 β &gt; 0.5（強い正暴露）</span>
        <span style={{ marginRight: 12 }}>🔴 β &lt; -0.5（強い負暴露）</span>
        <span>不透明（薄字）= |t-stat| &lt; 1.96 で統計的非有意</span>
      </div>
    </div>
  )
}

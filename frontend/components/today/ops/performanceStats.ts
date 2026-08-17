/**
 * 成績チャートの統計。純粋関数のみ。
 *
 * TWR と P&L は「似たグラフが2つある」ように見えて別物なので、
 * 何が違うのかを数字で言えるようにする。
 *
 *   TWR … %。入出金調整済み。ベンチマークと比べた「腕前」
 *   P&L … 円。入出金差し引き済み(2026-08-16以降)。実際に増えた「金額」
 *
 * どちらも積立の影響を除いてあるので、方向が食い違ったら本当に
 * 「勝率は高いが金額は負け」のような実態を表している。
 */

export type PnlPoint = { d: string; v: number }

export type PerformanceStats = {
  /** 期間の最終累積損益(円)。 */
  total: number
  /** 日次のプラス日数 / 判定できた日数。 */
  winDays: number
  totalDays: number
  winRate: number | null
  /** 累積の最大落ち込み(円、正の数)。 */
  maxDrawdown: number
  best: PnlPoint | null
  worst: PnlPoint | null
  /** 直近5日の増減(円)。 */
  last5: number | null
}

/** 累積系列から日次差分を作る。 */
export function dailyDeltas(series: PnlPoint[]): PnlPoint[] {
  const out: PnlPoint[] = []
  for (let i = 1; i < series.length; i += 1) {
    out.push({ d: series[i].d, v: series[i].v - series[i - 1].v })
  }
  return out
}

export function computePerformanceStats(series: PnlPoint[] | null | undefined): PerformanceStats | null {
  const points = (series ?? []).filter(p => p && Number.isFinite(p.v))
  if (points.length < 2) return null

  const deltas = dailyDeltas(points)
  const winDays = deltas.filter(p => p.v > 0).length
  const decided = deltas.filter(p => p.v !== 0).length

  let peak = points[0].v
  let maxDrawdown = 0
  for (const p of points) {
    if (p.v > peak) peak = p.v
    const dd = peak - p.v
    if (dd > maxDrawdown) maxDrawdown = dd
  }

  let best: PnlPoint | null = null
  let worst: PnlPoint | null = null
  for (const p of deltas) {
    if (!best || p.v > best.v) best = p
    if (!worst || p.v < worst.v) worst = p
  }

  const last = points[points.length - 1].v
  const fiveAgo = points.length >= 6 ? points[points.length - 6].v : null

  return {
    total: last,
    winDays,
    totalDays: deltas.length,
    // 引き分け(±0)は勝率の母数から外す。0円の日を負けに数えない。
    winRate: decided > 0 ? Math.round((winDays / decided) * 1000) / 10 : null,
    maxDrawdown: Math.round(maxDrawdown),
    best,
    worst,
    last5: fiveAgo == null ? null : last - fiveAgo,
  }
}

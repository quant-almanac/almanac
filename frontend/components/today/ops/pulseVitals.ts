/**
 * MARKET PULSE — 「市場の鼓動」を心電図として鳴らすための純粋関数群。
 *
 * これは価格の折れ線ではない。市場と自分の資産が今どれだけ張り詰めているかを
 * 合成スコアにし、心拍数として表す。リスクが高いほど拍が速くなる。
 *
 * 重要 — これは「表示専用の体感指標」であって、売買判断には一切使わない。
 * 実際の売買停止は behavioral_guard の閾値が唯一の権威で、ここはその状態を
 * 読むだけ。閾値の5つ目の定義場所を作らないため、自前の停止判定は持たない。
 */

/** 心拍数の下限・上限(bpm)。安静時〜頻脈の範囲に対応させる。 */
export const BPM_MIN = 48
export const BPM_MAX = 132

/** VIXのアンカー。市場慣行の目安(12=凪 / 20=平常 / 30=ストレス / 45=パニック)。 */
const VIX_ANCHORS: Array<[vix: number, score: number]> = [
  [12, 0], [20, 35], [30, 70], [45, 100],
]

/** 月間ドローダウンをスコア100に張り付かせる大きさ。live の stage1 停止水準に合わせる。 */
const MONTHLY_DD_FULL_SCALE = 0.10

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n))
const round1 = (n: number) => Math.round(n * 10) / 10

/** アンカー間を線形補間する。範囲外は端で頭打ち。 */
function interpolate(value: number, anchors: Array<[number, number]>): number {
  const [firstX, firstY] = anchors[0]
  if (value <= firstX) return firstY
  for (let i = 0; i < anchors.length - 1; i += 1) {
    const [x0, y0] = anchors[i]
    const [x1, y1] = anchors[i + 1]
    if (value <= x1) return y0 + ((value - x0) / (x1 - x0)) * (y1 - y0)
  }
  return anchors[anchors.length - 1][1]
}

/** 市場側の張り詰め具合 0..100。VIX が主。 */
/**
 * 日次終値から年率換算の実現ボラティリティ(%)を出す。
 *
 * VIX は年率換算のインプライドボラティリティ(%)なので、実現ボラを同じ
 * 年率%に直せば同一のアンカーで採点できる。日本株には VIX に相当する
 * 指数が手元のデータに無いため、価格系列から測るのが唯一の正直な手段。
 * 予測(implied)と実績(realized)は別物だが、単位と桁は揃う。
 */
export function realizedVolAnnualized(
  history: Array<{ close?: number | null }> | null | undefined,
): number | null {
  const closes = (history ?? [])
    .map(row => Number(row?.close))
    .filter(n => Number.isFinite(n) && n > 0)
  if (closes.length < 6) return null // 標本が少なすぎる推定は出さない
  const returns: number[] = []
  for (let i = 1; i < closes.length; i += 1) returns.push(Math.log(closes[i] / closes[i - 1]))
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length
  const variance = returns.reduce((a, r) => a + (r - mean) ** 2, 0) / (returns.length - 1)
  return round1(Math.sqrt(variance) * Math.sqrt(252) * 100)
}

export type MarketInputs = {
  /** 日本株の年率実現ボラ(%)。取れなければ null。 */
  japanVol?: number | null
  /** ポートフォリオの日本株比率 0..1。市場の重みづけに使う。 */
  japanWeight?: number | null
}

/**
 * 市場側の危険度 0..100。
 *
 * VIX だけで採点していたが、ポートフォリオの約3割は日本株で、その値動きが
 * 一切入っていなかった (2026-08-19)。「市場」は自分が晒されている市場の
 * ことなので、日米それぞれのボラを保有比率で重みづけて混ぜる。
 * 日本株データが無い日は従来どおり VIX だけで採点する —— 欠測を0として
 * 混ぜると、日本株が静かだったことにされてしまう。
 */
export function marketRiskScore(
  vix: number | null | undefined,
  inputs: MarketInputs = {},
): number | null {
  const usScore = vix != null && Number.isFinite(vix)
    ? clamp(interpolate(vix, VIX_ANCHORS), 0, 100)
    : null
  const jpVol = inputs.japanVol
  const jpScore = jpVol != null && Number.isFinite(jpVol)
    ? clamp(interpolate(jpVol, VIX_ANCHORS), 0, 100)
    : null

  if (usScore == null && jpScore == null) return null
  if (jpScore == null) return round1(usScore as number)
  if (usScore == null) return round1(jpScore)

  const rawWeight = inputs.japanWeight
  const jpWeight = rawWeight != null && Number.isFinite(rawWeight)
    ? clamp(rawWeight, 0, 1)
    : 0.5
  return round1(usScore * (1 - jpWeight) + jpScore * jpWeight)
}

export type GuardState = {
  new_entry_allowed?: boolean
  trading_allowed?: boolean
  alerts?: unknown[]
  daily_pnl_pct?: number | null
  monthly_pnl_pct?: number | null
}

/**
 * 自分側の危険度 0..100。
 * 月間ドローダウンの大きさに、ガードが実際に締まっている度合いを加算する。
 * ガードの状態はシステム自身の停止判断そのものなので、閾値を再計算せずそのまま使う。
 */
export function ownRiskScore(guard: GuardState | null | undefined): number | null {
  if (!guard) return null
  const monthly = guard.monthly_pnl_pct
  const hasMonthly = monthly != null && Number.isFinite(monthly)
  // 含み益方向(正)はリスクではないので0。負のときだけ大きさを使う。
  const ddScore = hasMonthly
    ? clamp((Math.max(0, -(monthly as number)) / MONTHLY_DD_FULL_SCALE) * 100, 0, 100)
    : 0

  let penalty = 0
  if (guard.new_entry_allowed === false) penalty += 25
  if (guard.trading_allowed === false) penalty += 45
  penalty += Math.min(24, (guard.alerts?.length ?? 0) * 8)

  if (!hasMonthly && penalty === 0) return null
  return round1(clamp(ddScore + penalty, 0, 100))
}

export type Vitals = {
  score: number
  bpm: number
  market: number | null
  own: number | null
  /** 速さの主因。合成だと「なぜ速いか」が消えるため、支配側を明示する。 */
  driver: 'market' | 'own' | 'balanced'
  state: '平静' | 'やや緊張' | '緊張' | '警戒'
}

const MARKET_WEIGHT = 0.55
const OWN_WEIGHT = 0.45

/** 市場側と自分側を合成して心拍を決める。片方しか無ければ、あるほうを使う。 */
export function computeVitals(market: number | null, own: number | null): Vitals | null {
  if (market == null && own == null) return null

  let score: number
  if (market != null && own != null) {
    score = market * MARKET_WEIGHT + own * OWN_WEIGHT
  } else {
    score = (market ?? own) as number
  }
  score = round1(clamp(score, 0, 100))

  // 寄与の大きい側を主因とする。差が小さければ balanced。
  let driver: Vitals['driver'] = 'balanced'
  if (market != null && own != null) {
    const mContrib = market * MARKET_WEIGHT
    const oContrib = own * OWN_WEIGHT
    const gap = Math.abs(mContrib - oContrib)
    if (gap >= 8) driver = mContrib > oContrib ? 'market' : 'own'
  } else if (market != null) driver = 'market'
  else driver = 'own'

  const state: Vitals['state'] =
    score < 25 ? '平静' : score < 50 ? 'やや緊張' : score < 75 ? '緊張' : '警戒'

  return { score, bpm: Math.round(bpmFor(score)), market, own, driver, state }
}

/** スコア 0..100 を心拍数へ。線形で十分読める。 */
export function bpmFor(score: number): number {
  return BPM_MIN + (clamp(score, 0, 100) / 100) * (BPM_MAX - BPM_MIN)
}

/** 1拍の秒数。CSSアニメーションの duration に使う。 */
export function beatSeconds(bpm: number): number {
  return 60 / Math.max(1, bpm)
}

/**
 * 心電図1拍分のSVGパス。幅 w / 高さ h の矩形に、基線を中央にして描く。
 * P波・QRS・T波の順。QRSだけ直線で鋭く、P/Tは丸みを持たせる。
 */
export function ecgCyclePath(w: number, h: number, amplitude = 0.78): string {
  const mid = h / 2
  const a = (h / 2) * amplitude
  const x = (f: number) => round1(f * w)
  const y = (f: number) => round1(mid - f * a)

  return [
    `M0,${mid}`,
    `L${x(0.10)},${mid}`,
    // P波(丸い小山)
    `Q${x(0.15)},${y(0.16)} ${x(0.21)},${mid}`,
    `L${x(0.31)},${mid}`,
    // QRS群(鋭い)
    `L${x(0.34)},${y(-0.10)}`,
    `L${x(0.39)},${y(1)}`,
    `L${x(0.44)},${y(-0.34)}`,
    `L${x(0.48)},${mid}`,
    `L${x(0.60)},${mid}`,
    // T波(丸いなだらかな山)
    `Q${x(0.69)},${y(0.30)} ${x(0.79)},${mid}`,
    `L${w},${mid}`,
  ].join(' ')
}

/** データが無いときの平坦線(フラットライン)。合成波は作らない。 */
export function flatlinePath(w: number, h: number): string {
  return `M0,${h / 2} L${w},${h / 2}`
}

/**
 * 1拍分の心電図を敷き詰め用の背景画像(data URI)にする。
 * 背景 repeat-x + transform で流すと、コンテナ幅を測らずに等速で走らせられる。
 */
export function ecgTileDataUri(opts: {
  width: number
  height: number
  color: string
  flat?: boolean
  strokeWidth?: number
}): string {
  const { width, height, color, flat = false, strokeWidth = 1.7 } = opts
  const d = flat ? flatlinePath(width, height) : ecgCyclePath(width, height)
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">` +
    `<path d="${d}" fill="none" stroke="${color}" stroke-width="${strokeWidth}" ` +
    `stroke-linecap="round" stroke-linejoin="round"/></svg>`
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`
}

export type Relation = {
  key: 'calm' | 'market_led' | 'own_led' | 'both_tense' | 'normal' | 'partial'
  label: string
  detail: string
}

/** どちらが先行しているとみなすかの差(スコア点)。これ未満は拮抗扱い。 */
const LEAD_GAP = 20

/**
 * 自分と市場の「組み合わせ」に名前を付ける。
 *
 * 2つを平均した合成スコアは、市場だけ荒れている場合と自分だけ痛んでいる場合を
 * 同じ数字に潰してしまう — 一番知りたい違いが消える。平均する代わりに、
 * どちらが先行しているかを言葉にする。
 */
export function relationVerdict(own: number | null, market: number | null): Relation | null {
  if (own == null && market == null) return null
  if (own == null || market == null) {
    const known = (own ?? market) as number
    const side = own == null ? '市場' : '自分の資産'
    return {
      key: 'partial',
      label: '片側のみ',
      detail: `${side}だけで判断中（${Math.round(known)}）。もう片方は取得できていない`,
    }
  }

  if (own >= 50 && market >= 50) {
    return { key: 'both_tense', label: '同時警戒', detail: '相場も資産も張り詰めている。守りを優先する場面' }
  }
  const gap = own - market
  if (gap >= LEAD_GAP) {
    return { key: 'own_led', label: '自分先行', detail: '相場は静かなのに資産が痛んでいる。個別要因を疑う' }
  }
  if (-gap >= LEAD_GAP) {
    return { key: 'market_led', label: '市場先行', detail: '相場は荒れているが資産は耐えている' }
  }
  if (own < 25 && market < 25) {
    return { key: 'calm', label: '静穏', detail: '相場も資産も落ち着いている' }
  }
  return { key: 'normal', label: '平常', detail: '相場と資産が同じ程度に動いている' }
}

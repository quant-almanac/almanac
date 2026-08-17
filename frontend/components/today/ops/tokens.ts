/**
 * ALMANAC Ops — オブシディアン・コンソール デザイントークン (v8)
 * 黒曜石ベース + 朱/金アクセント。ブランドは明朝、本文はゴシック、数字は等幅で統一。
 *
 * v8 の方針: 「明るさの段差」と「輪郭」でメリハリを作る。
 *
 * v7 は面を持ち上げる代わりに 上端ハイライト + 天面グラデ + 多重の落ち影 を
 * 全ての面に敷いた。これはベベル/エンボス調＝古い見た目になり、しかも地が
 * 暗いままなので結局読みにくかった。v8 は逆に、
 *   - 地と面の明度差そのものを大きく取る（影ではなく明るさで階層を作る）
 *   - 輪郭は 1px の見える境界線で切る
 *   - 影は「浮いているもの」(モーダル・ポップオーバー) だけに限定する
 *   - 数字やラベルの発光 (text-shadow) は使わない
 * 面の階層: bg < panel < panelAlt < raised（sunken は溝のみ）
 */

export const OPS = {
  // 面 — 影ではなく明度で段差を作る。ここが v8 の要
  bg: '#031426',       // ページ地
  sunken: '#04111E',   // 溝（メータートラック・入力欄）
  inset: '#04111E',    // sunken の別名（既存呼び出し互換）
  panel: '#071C2E',    // 基本カード
  panelAlt: '#0C263B', // 段上げ（スタット枠・カード内カード）
  raised: '#12334C',   // チップ・hover

  border: '#27445A',   // はっきり見える境界線
  hairline: '#17364C', // 内部の仕切り

  // 文字
  text: '#F6F4EF',
  sub: '#C6C1B2',
  dim: '#A09B8D',

  // アクセント
  vermilion: '#F0655A', // 朱 — 新規・要注目
  gold: '#E2C078',      // 金 — ラベル・焦点
  green: '#5FD3A0',     // 約定・正常
  amber: '#EDB44E',     // 指値中・注意
  blue: '#8FAAE0',      // 監視・情報
  redSoft: '#F0958B',   // 警告テキスト

  orchid: '#C89ED6',    // 決算イベント用（朱と区別）
  orchidBg: 'rgba(200, 158, 214, 0.16)',

  // 淡色背景（チップ用）
  vermilionBg: 'rgba(240, 101, 90, 0.16)',
  goldBg: 'rgba(226, 192, 120, 0.14)',
  greenBg: 'rgba(95, 211, 160, 0.14)',
  amberBg: 'rgba(237, 180, 78, 0.16)',
  blueBg: 'rgba(143, 170, 224, 0.16)',
  dimBg: 'rgba(198, 193, 178, 0.10)',

  // 明色の「今日の判断」面。暗色面のアクセントを文字に再利用しない。
  paper: '#F2EDE0',
  paperText: '#1E2229',
  paperSub: '#57544C',
  paperBorder: '#D6CDB8',
  paperControlBorder: '#8E826A',
  paperGreenInk: '#0F6B4A',
  paperAmberInk: '#8A5510',
  paperVermilionInk: '#A82B1C',
  paperBlueInk: '#2E4E8F',
  paperSealInk: '#C0392B',

  // 影 — 実際に浮いているものにだけ。面の装飾には使わない
  shadow: '0 10px 30px -12px rgba(0,0,0,.7)',
  shadowOverlay: '0 24px 64px -20px rgba(0,0,0,.8)',

  // フォント
  mono: "var(--font-almanac-mono), 'SF Mono', ui-monospace, monospace",
  sans: "var(--font-almanac-sans), 'Hiragino Sans', 'Yu Gothic', sans-serif",
  brand: "var(--font-almanac-brand), 'Times New Roman', serif",
  display: "var(--font-almanac-display), 'Hiragino Mincho ProN', 'YuMincho', serif",
} as const

/** 発注ボードの状態 → ランプ色/ラベル */
export const STATUS_META: Record<string, { label: string; color: string; bg: string }> = {
  pending: { label: '未発注', color: OPS.vermilion, bg: OPS.vermilionBg },
  proposed: { label: '提案', color: OPS.vermilion, bg: OPS.vermilionBg },
  placed: { label: '指値中', color: OPS.amber, bg: OPS.amberBg },
  filled: { label: '約定', color: OPS.green, bg: OPS.greenBg },
  cancelled: { label: '取消', color: OPS.dim, bg: OPS.dimBg },
  expired: { label: '期限切れ', color: OPS.dim, bg: OPS.dimBg },
  reprice_required: { label: '再評価待ち', color: OPS.amber, bg: OPS.amberBg },
}

/** アクション種別 → 表示 */
export const TYPE_META: Record<string, { label: string; color: string; bg: string }> = {
  buy: { label: '買い', color: OPS.green, bg: OPS.greenBg },
  add: { label: '買い増し', color: OPS.green, bg: OPS.greenBg },
  trim: { label: '部分利確', color: OPS.amber, bg: OPS.amberBg },
  sell: { label: '売り', color: OPS.vermilion, bg: OPS.vermilionBg },
  hold: { label: '保持', color: OPS.blue, bg: OPS.blueBg },
  hedge: { label: 'ヘッジ', color: OPS.blue, bg: OPS.blueBg },
}

export const STANCE_LABEL: Record<string, string> = {
  aggressive: '攻め',
  moderately_aggressive: 'やや攻め',
  neutral: '中立',
  moderately_defensive: 'やや守り',
  defensive: '守り',
}

export const URGENCY_COLOR: Record<string, string> = {
  high: OPS.vermilion,
  medium: OPS.amber,
  low: OPS.green,
}

/** ORDERS ⇔ SIGNAL MAP を結ぶ連番グリフ。カードとドットで同じ番号を共有する。 */
const RANK_GLYPH = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩', '⑪', '⑫']
export function rankGlyph(i: number): string {
  return RANK_GLYPH[i] ?? `#${i + 1}`
}

/**
 * board 各行の象限ラベル。SignalMap の分割線（確信度50 / 影響yMax÷2）と同一ロジック。
 * confidence_pct / impact_nav_pct が無い行は null。
 */
export function quadrantLabels(
  board: { confidence_pct?: number; impact_nav_pct?: number | null }[]
): (string | null)[] {
  const impacts = board
    .map(b => b.impact_nav_pct)
    .filter((v): v is number => v != null)
  const yMax = Math.max(0.8, ...(impacts.length ? impacts : [0.8])) * 1.35
  const mid = yMax / 2
  return board.map(b => {
    if (b.confidence_pct == null || b.impact_nav_pct == null) return null
    const hiConf = b.confidence_pct >= 50
    const hiImp = b.impact_nav_pct >= mid
    if (hiConf && hiImp) return '主戦場'
    if (!hiConf && hiImp) return '慎重に観察'
    if (hiConf && !hiImp) return '流し見'
    return '優先度低'
  })
}

export const QUADRANT_COLOR: Record<string, string> = {
  主戦場: OPS.gold,
  慎重に観察: OPS.amber,
  流し見: OPS.blue,
  優先度低: OPS.dim,
}

export function fmtJpy(v: number | null | undefined): string {
  if (v == null) return '—'
  if (Math.abs(v) >= 10000) return `¥${Math.round(v / 10000).toLocaleString()}万`
  return `¥${Math.round(v).toLocaleString()}`
}

export function fmtAge(hours: number | null | undefined): string {
  if (hours == null) return '—'
  if (hours < 1) return `${Math.round(hours * 60)}分前`
  if (hours < 48) return `${Math.round(hours)}時間前`
  return `${Math.round(hours / 24)}日前`
}

/** expiry_at までの残り。過去なら null を返す */
export function remainingLabel(expiryAt: string | null | undefined): { label: string; over: boolean } | null {
  if (!expiryAt) return null
  const diffMs = new Date(expiryAt).getTime() - Date.now()
  if (Number.isNaN(diffMs)) return null
  if (diffMs <= 0) return { label: '期限超過', over: true }
  const totalMin = Math.floor(diffMs / 60000)
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  return { label: h > 0 ? `残 ${h}:${String(m).padStart(2, '0')}` : `残 ${m}分`, over: false }
}

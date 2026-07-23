/**
 * ALMANAC Ops — オブシディアン・コンソール デザイントークン (v5)
 * 黒曜石ベース + 朱/金アクセント。数字は等幅、セクション見出しはドットマトリクス（発車標）。
 */

export const OPS = {
  // 面
  bg: '#0B0D12',
  panel: '#12151C',
  panelAlt: '#161A22',
  inset: '#0E1117',
  border: '#232833',
  hairline: '#1B202A',

  // 文字
  text: '#E9E7DF',
  sub: '#9C9889',
  dim: '#807C73', // 旧 #6C6960 (3.5:1) → 4.7:1。補助テキストのAA確保。sub との階層差は維持


  // アクセント
  vermilion: '#E0483C', // 朱 — 新規・要注目
  gold: '#C9A75D',      // 金 — ラベル・焦点
  green: '#57BE92',     // 約定・正常
  amber: '#D9A13B',     // 指値中・注意
  blue: '#6E8CC3',      // 監視・情報
  redSoft: '#E08379',   // 警告テキスト

  orchid: '#B98CC9',    // 決算イベント用（朱と区別）
  orchidBg: 'rgba(185, 140, 201, 0.12)',

  // 淡色背景（チップ用）
  vermilionBg: 'rgba(224, 72, 60, 0.12)',
  goldBg: 'rgba(201, 167, 93, 0.10)',
  greenBg: 'rgba(87, 190, 146, 0.10)',
  amberBg: 'rgba(217, 161, 59, 0.12)',
  blueBg: 'rgba(110, 140, 195, 0.12)',
  dimBg: 'rgba(156, 152, 137, 0.08)',

  // フォント
  mono: "'JetBrains Mono', 'SF Mono', ui-monospace, monospace",
  sans: "'Noto Sans JP', 'Hiragino Sans', 'Yu Gothic', sans-serif",
  dot: "'DotGothic16', 'Noto Sans JP', monospace", // 発車標ドットマトリクス
  display: "'Shippori Mincho', 'Hiragino Mincho ProN', 'YuMincho', serif", // 朝刊の大見出し
} as const

/** 発注ボードの状態 → ランプ色/ラベル */
export const STATUS_META: Record<string, { label: string; color: string; bg: string }> = {
  pending: { label: '未発注', color: OPS.vermilion, bg: OPS.vermilionBg },
  proposed: { label: '提案', color: OPS.vermilion, bg: OPS.vermilionBg },
  placed: { label: '指値中', color: OPS.amber, bg: OPS.amberBg },
  filled: { label: '約定', color: OPS.green, bg: OPS.greenBg },
  cancelled: { label: '取消', color: OPS.dim, bg: OPS.dimBg },
  expired: { label: '期限切れ', color: OPS.dim, bg: OPS.dimBg },
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

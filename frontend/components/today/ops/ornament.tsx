'use client'

import type { CSSProperties, ReactNode } from 'react'
import { OPS } from './tokens'

/**
 * ALMANAC 装飾体系 — 「相場暦」を実際に暦らしく見せるための意匠。
 *
 * v8 までは面と輪郭を整えただけで、ブランド（相場暦・黒曜石・朱と金・明朝）が
 * 見た目に一切現れていなかった。明朝体はアプリ全体で1箇所しか使われておらず、
 * 結果として「無難なダークダッシュボード」に留まっていた。
 *
 * v9 は印刷物としての暦を意匠の出発点にする:
 *   - 二十四節気を実際に表示する（暦の中核。意味のある装飾）
 *   - 見出しと数字は明朝／Cormorant。等幅は数値の桁揃えだけに使う
 *   - 罫線は二重罫。隅は trim mark（トンボ）で紙面の体裁を作る
 *   - 「今日」「発動中」は朱印で示す
 *   - 地には極薄の紙目。単色の板ではなく刷り物の質感にする
 * 影やベベルは引き続き使わない。装飾は全て線・文字・余白で作る。
 */

/* ── 二十四節気 ───────────────────────────────────────
   節気の開始日は年により±1日ずれるが、表示上の暦注なので概算日で扱う。
   月/日は「その節気が始まる日」。 */
const SOLAR_TERMS: Array<{ m: number; d: number; name: string; yomi: string; note: string }> = [
  { m: 1, d: 6, name: '小寒', yomi: 'shōkan', note: '寒の入り' },
  { m: 1, d: 20, name: '大寒', yomi: 'daikan', note: '一年で最も寒い頃' },
  { m: 2, d: 4, name: '立春', yomi: 'risshun', note: '暦の上の春' },
  { m: 2, d: 19, name: '雨水', yomi: 'usui', note: '雪が雨に変わる' },
  { m: 3, d: 6, name: '啓蟄', yomi: 'keichitsu', note: '虫が動き出す' },
  { m: 3, d: 21, name: '春分', yomi: 'shunbun', note: '昼夜が等しい' },
  { m: 4, d: 5, name: '清明', yomi: 'seimei', note: '万物が清く明らか' },
  { m: 4, d: 20, name: '穀雨', yomi: 'kokuu', note: '穀物を潤す雨' },
  { m: 5, d: 6, name: '立夏', yomi: 'rikka', note: '暦の上の夏' },
  { m: 5, d: 21, name: '小満', yomi: 'shōman', note: '草木が茂る' },
  { m: 6, d: 6, name: '芒種', yomi: 'bōshu', note: '種蒔きの頃' },
  { m: 6, d: 21, name: '夏至', yomi: 'geshi', note: '昼が最も長い' },
  { m: 7, d: 7, name: '小暑', yomi: 'shōsho', note: '暑さが本格化' },
  { m: 7, d: 23, name: '大暑', yomi: 'taisho', note: '一年で最も暑い頃' },
  { m: 8, d: 8, name: '立秋', yomi: 'risshū', note: '暦の上の秋' },
  { m: 8, d: 23, name: '処暑', yomi: 'shosho', note: '暑さが収まる' },
  { m: 9, d: 8, name: '白露', yomi: 'hakuro', note: '露が結ぶ' },
  { m: 9, d: 23, name: '秋分', yomi: 'shūbun', note: '昼夜が等しい' },
  { m: 10, d: 8, name: '寒露', yomi: 'kanro', note: '冷たい露' },
  { m: 10, d: 24, name: '霜降', yomi: 'sōkō', note: '霜が降りる' },
  { m: 11, d: 7, name: '立冬', yomi: 'rittō', note: '暦の上の冬' },
  { m: 11, d: 22, name: '小雪', yomi: 'shōsetsu', note: '雪が降り始める' },
  { m: 12, d: 7, name: '大雪', yomi: 'taisetsu', note: '雪が本降りに' },
  { m: 12, d: 22, name: '冬至', yomi: 'tōji', note: '夜が最も長い' },
]

export interface SolarTerm { name: string; yomi: string; note: string; dayIndex: number }

/** 指定日が属する節気と、その節気に入って何日目かを返す。 */
export function solarTerm(date: Date): SolarTerm {
  const year = date.getFullYear()
  const stamp = (m: number, d: number, y = year) => new Date(y, m - 1, d).getTime()
  const now = new Date(year, date.getMonth(), date.getDate()).getTime()

  let current = SOLAR_TERMS[SOLAR_TERMS.length - 1]
  let start = stamp(current.m, current.d, year - 1)
  for (const term of SOLAR_TERMS) {
    const begin = stamp(term.m, term.d)
    if (begin <= now) {
      current = term
      start = begin
    }
  }
  return { ...current, dayIndex: Math.floor((now - start) / 86400000) + 1 }
}

/* ── 朱印 ─────────────────────────────────────────────
   和文書の「今ここ」を示す標。縦書き2文字で捺したように少し傾ける。 */
export function Seal({ label, size = 36, color = OPS.vermilion, tilt = -5 }: {
  label: string
  size?: number
  color?: string
  tilt?: number
}) {
  return (
    <span
      aria-hidden
      style={{
        display: 'inline-grid',
        placeItems: 'center',
        width: size,
        height: size,
        flexShrink: 0,
        border: `1.5px solid ${color}`,
        borderRadius: 3,
        background: `${color}1f`,
        color,
        fontFamily: OPS.display,
        fontSize: size * 0.33,
        fontWeight: 600,
        lineHeight: 1.06,
        letterSpacing: '.04em',
        writingMode: 'vertical-rl',
        transform: `rotate(${tilt}deg)`,
      }}
    >
      {label}
    </span>
  )
}

/* ── 二重罫 ───────────────────────────────────────────
   印刷物の区切り。太1px + 細1pxの対で、線一本より紙面らしくなる。 */
export function DoubleRule({ tone = OPS.gold, fade = true }: { tone?: string; fade?: boolean }) {
  const grad = (a: string, b: string) =>
    fade ? `linear-gradient(90deg, ${a}, ${b} 34%, transparent 92%)` : `linear-gradient(90deg, ${a}, ${b})`
  return (
    <div aria-hidden style={{ marginTop: 9 }}>
      <div style={{ height: 1, background: grad(tone, `${tone}55`) }} />
      <div style={{ height: 1, marginTop: 2, background: grad(`${tone}66`, `${OPS.hairline}`) }} />
    </div>
  )
}

/* ── トンボ（隅の見当線）─────────────────────────────
   主要な面の四隅に短い罫を置くと、囲むだけの枠より「版面」に見える。 */
export function TrimMarks({ tone = OPS.gold, inset = -1, len = 11 }: { tone?: string; inset?: number; len?: number }) {
  const bar = (style: CSSProperties): CSSProperties => ({ position: 'absolute', background: tone, ...style })
  return (
    <span aria-hidden style={{ position: 'absolute', inset, pointerEvents: 'none' }}>
      <i style={bar({ left: 0, top: 0, width: len, height: 1 })} />
      <i style={bar({ left: 0, top: 0, width: 1, height: len })} />
      <i style={bar({ right: 0, top: 0, width: len, height: 1 })} />
      <i style={bar({ right: 0, top: 0, width: 1, height: len })} />
      <i style={bar({ left: 0, bottom: 0, width: len, height: 1 })} />
      <i style={bar({ left: 0, bottom: 0, width: 1, height: len })} />
      <i style={bar({ right: 0, bottom: 0, width: len, height: 1 })} />
      <i style={bar({ right: 0, bottom: 0, width: 1, height: len })} />
    </span>
  )
}

/** 縦書きの傍注。暦の余白に入る細い注記。 */
export function VerticalNote({ children, tone = OPS.dim }: { children: ReactNode; tone?: string }) {
  return (
    <span
      style={{
        writingMode: 'vertical-rl',
        fontFamily: OPS.display,
        fontSize: 11,
        letterSpacing: '.22em',
        color: tone,
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  )
}

/* ── 紙目 ─────────────────────────────────────────────
   極薄のノイズ。単色の板ではなく刷り物の地に見せる。外部リクエスト無しの data URI。 */
const GRAIN = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='.55'/%3E%3C/svg%3E"

export const ORNAMENT_CSS = `
/* 紙目: 地を材質にする。グラデーションではなく粒子なので立体感の演出にはならない */
body::after {
  content: '';
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  opacity: .035;
  background-image: url("${GRAIN}");
}
body > * { position: relative; z-index: 1; }

/* 暦注（節気）の見出し。詰めた明朝で暦らしさを出す */
.almanac-term { font-family: ${OPS.display}; letter-spacing: .16em; }
.almanac-term-name { font-size: 20px; font-weight: 600; color: ${OPS.gold}; }

/* 見出しの欧文は Cormorant。等幅は数値専用に戻す */
.ops-latin { font-family: ${OPS.brand}; letter-spacing: .3em; font-weight: 600; }
`

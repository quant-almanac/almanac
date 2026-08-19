'use client'

import { AnimatePresence, motion } from 'framer-motion'
import { OPS } from './tokens'

/**
 * 選択ハイライトの共有アニメーション。発注行・レビュー行・判断地図の点・
 * ActionSection/OrderMap のレーンで同じ視覚言語(金のリングが一度だけ広がって消える)を使う。
 *
 * active が true になった瞬間だけ再生し、true のまま留まっても再生し続けない
 * (旧 OrderMap の SMIL `repeatCount="indefinite"` のような常時ループを避ける)。
 * MotionConfig(reducedMotion="user") 配下で使う前提のため、reduced-motion時は
 * Framer Motion 側が自動的にアニメーションを止める。
 */

const RING_TRANSITION = { duration: 0.6, ease: 'easeOut' as const }

/** HTML用。position:relative な親の中に置く。 */
export function SelectionPulse({ active, color = OPS.gold }: { active: boolean; color?: string }) {
  return (
    <AnimatePresence>
      {active && (
        <motion.span
          key="pulse"
          aria-hidden="true"
          initial={{ opacity: 0.55, scale: 0.7 }}
          animate={{ opacity: 0, scale: 1.6 }}
          exit={{ opacity: 0 }}
          transition={RING_TRANSITION}
          style={{
            position: 'absolute', inset: 0, borderRadius: 'inherit',
            border: `1.5px solid ${color}`, pointerEvents: 'none',
          }}
        />
      )}
    </AnimatePresence>
  )
}

/** SVG用。<g> の中に他の <circle> と並べて置く。 */
export function SelectionPulseSvg({ active, cx, cy, r, color = OPS.gold }: {
  active: boolean; cx: number; cy: number; r: number; color?: string
}) {
  return (
    <AnimatePresence>
      {active && (
        <motion.circle
          key="pulse-svg"
          aria-hidden="true"
          cx={cx} cy={cy}
          fill="none" stroke={color} strokeWidth={1.5}
          initial={{ r: r * 0.75, opacity: 0.7 }}
          animate={{ r: r * 1.5, opacity: 0 }}
          exit={{ opacity: 0 }}
          transition={RING_TRANSITION}
        />
      )}
    </AnimatePresence>
  )
}

'use client'

interface RegimeBadgeProps {
  spyAbove: boolean
  nkAbove: boolean
  regime?: string
}

function getRegimeLabel(spyAbove: boolean, nkAbove: boolean, regime?: string): { label: string; color: string } {
  if (regime) {
    if (regime.includes('強気') || regime.startsWith('A_')) return { label: regime, color: '#34D399' }
    if (regime.includes('弱気') || regime.startsWith('C_')) return { label: regime, color: '#F87171' }
    return { label: regime, color: '#FBBF24' }
  }
  if (spyAbove && nkAbove) return { label: '強気', color: '#34D399' }
  if (!spyAbove && !nkAbove) return { label: '弱気', color: '#F87171' }
  return { label: '中立', color: '#FBBF24' }
}

export default function RegimeBadge({ spyAbove, nkAbove, regime }: RegimeBadgeProps) {
  const { label, color } = getRegimeLabel(spyAbove, nkAbove, regime)

  return (
    <span
      className="animate-pulse-glow inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold"
      style={{
        color,
        background: `${color}18`,
        border: `1px solid ${color}40`,
      }}
    >
      <span
        className="w-1.5 h-1.5 rounded-full"
        style={{ background: color }}
      />
      {label}
    </span>
  )
}

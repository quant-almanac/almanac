'use client'

// AI テキスト中のティッカーシンボルをインラインバッジで表示するコンポーネント

export const TICKER_SKIP = new Set([
  'AI', 'USD', 'JPY', 'RSI', 'VIX', 'MA', 'OK', 'IT',
  'ROE', 'EPS', 'FCF', 'PEG', 'ETF', 'GDP', 'BOJ', 'FED',
  'IPO', 'OR', 'AT', 'IN', 'BE', 'ON', 'IF', 'BY', 'NO',
])

export function isRealTicker(word: string): boolean {
  return /^[A-Z]{2,5}$/.test(word) && !TICKER_SKIP.has(word)
}

interface TickerBadgeProps {
  ticker: string
}

export default function TickerBadge({ ticker }: TickerBadgeProps) {
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '1px 7px',
        marginInline: '2px',
        borderRadius: 5,
        background: 'rgba(124,92,252,0.14)',
        border: '1px solid rgba(124,92,252,0.38)',
        color: '#9B85FD',
        fontSize: '0.88em',
        fontWeight: 700,
        fontFamily: '"SF Mono", "Fira Code", monospace',
        verticalAlign: 'middle',
        letterSpacing: '0.03em',
        lineHeight: 1.6,
        whiteSpace: 'nowrap',
      }}
    >
      {ticker}
    </span>
  )
}

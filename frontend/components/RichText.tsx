'use client'

// テキストを受け取り、ティッカーシンボルを TickerBadge に変換して返す

import { Fragment } from 'react'
import TickerBadge, { isRealTicker } from './TickerBadge'

interface RichTextProps {
  text: string
  style?: React.CSSProperties
  className?: string
}

export default function RichText({ text, style, className }: RichTextProps) {
  // \b([A-Z]{2,5})\b で単語境界付き大文字シーケンスを分割
  // parts[0], parts[2], parts[4]... → 通常テキスト
  // parts[1], parts[3], parts[5]... → キャプチャグループ（ティッカー候補）
  const parts = text.split(/\b([A-Z]{2,5})\b/)

  return (
    <span className={className} style={style}>
      {parts.map((part, i) => {
        if (i % 2 === 1 && isRealTicker(part)) {
          return <TickerBadge key={i} ticker={part} />
        }
        return <Fragment key={i}>{part}</Fragment>
      })}
    </span>
  )
}

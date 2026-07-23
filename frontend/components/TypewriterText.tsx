'use client'

import { useState, useEffect, useRef } from 'react'
import { eventBus } from '@/lib/eventBus'
import { TICKER_SKIP } from './TickerBadge'

interface TypewriterTextProps {
  text: string
  delay?: number   // ms before starting
  speed?: number   // ms per character
  className?: string
  style?: React.CSSProperties
  emitTickers?: boolean  // ティッカー発見時に ticker-focus を emit するか
}

export default function TypewriterText({
  text,
  delay = 0,
  speed = 25,
  className,
  style,
  emitTickers = true,
}: TypewriterTextProps) {
  const [displayed, setDisplayed] = useState('')
  const prevText = useRef('')

  useEffect(() => {
    if (!text) return

    // テキストが変わったらリセット
    if (text !== prevText.current) {
      prevText.current = text
      setDisplayed('')
    }

    let i = 0
    let interval: ReturnType<typeof setInterval>

    const timeout = setTimeout(() => {
      interval = setInterval(() => {
        i++
        const slice = text.slice(0, i)
        setDisplayed(slice)

        // ティッカー完成検知：現在の末尾が大文字列で、次の文字が非大文字か末尾
        if (emitTickers) {
          const nextChar = text[i]  // まだ明かされていない次の文字
          const isWordBoundary = nextChar === undefined || /[^A-Z]/.test(nextChar)
          if (isWordBoundary) {
            const match = slice.match(/([A-Z]{2,5})$/)
            if (match && !TICKER_SKIP.has(match[1])) {
              eventBus.emit<string>('ticker-focus', match[1])
            }
          }
        }

        if (i >= text.length) clearInterval(interval)
      }, speed)
    }, delay)

    return () => {
      clearTimeout(timeout)
      clearInterval(interval)
    }
  }, [text, delay, speed, emitTickers])

  const done = displayed.length >= text.length

  return (
    <span className={className} style={style}>
      {displayed}
      {!done && (
        <span
          style={{
            borderRight: '2px solid currentColor',
            marginLeft: 1,
            animation: 'blink 0.7s step-end infinite',
            display: 'inline-block',
            width: 0,
            verticalAlign: 'text-bottom',
          }}
        />
      )}
    </span>
  )
}

'use client'

/**
 * Fix 3B (2026-04-25): Next.js App Router Error Boundary
 *
 * 本番ビルドで発生したクライアント例外を「Application error」で隠さず、
 * メッセージ + スタックを画面に表示することで原因切り分けを即時可能にする。
 *
 * Next.js 14 App Router の規約により app/error.tsx は自動的に
 * (segment 全体の) Error Boundary として機能する。
 */
import { useEffect } from 'react'

export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    // 本番でもブラウザコンソールに必ずログ
    console.error('[app/error]', error)
  }, [error])

  return (
    <div style={{ padding: 40, color: '#E4E8EF', maxWidth: 880, margin: '40px auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <span style={{ fontSize: 28 }}>⚠️</span>
        <h2 style={{ margin: 0, fontSize: 22, fontWeight: 800 }}>エラーが発生しました</h2>
      </div>
      <p style={{ color: '#A8B2C8', fontSize: 15, lineHeight: 1.7, marginBottom: 16 }}>
        画面の描画中に例外が投げられました。下記スタックトレースを開発者に共有してください。
        {error.digest && (
          <span style={{ display: 'block', marginTop: 4, fontSize: 13, color: '#7E8BA8' }}>
            digest: <code>{error.digest}</code>
          </span>
        )}
      </p>
      <pre style={{
        color: '#FCA5A5',
        background: '#0F1219',
        border: '1px solid rgba(248,113,113,0.3)',
        padding: 16,
        borderRadius: 8,
        fontSize: 13,
        lineHeight: 1.6,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        overflowX: 'auto',
        maxHeight: 360,
      }}>
        {error.message || '(no message)'}{'\n\n'}{error.stack ?? '(no stack)'}
      </pre>
      <div style={{ marginTop: 16, display: 'flex', gap: 10 }}>
        <button
          onClick={reset}
          style={{
            padding: '8px 18px',
            borderRadius: 8,
            cursor: 'pointer',
            background: 'rgba(124,92,252,0.18)',
            border: '1px solid rgba(124,92,252,0.4)',
            color: '#9B85FD',
            fontSize: 14,
            fontWeight: 600,
          }}
        >
          🔄 再試行
        </button>
        <button
          onClick={() => { if (typeof window !== 'undefined') window.location.href = '/' }}
          style={{
            padding: '8px 18px',
            borderRadius: 8,
            cursor: 'pointer',
            background: '#1A1E2C',
            border: '1px solid #232839',
            color: '#A8B2C8',
            fontSize: 14,
          }}
        >
          🏠 ホームに戻る
        </button>
      </div>
    </div>
  )
}

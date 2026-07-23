import type { Metadata } from 'next'
import './globals.css'
import ClientLayout from '@/components/ClientLayout'

export const metadata: Metadata = {
  title: 'ALMANAC Console',
  description: 'AI-powered portfolio intelligence',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ja" suppressHydrationWarning>
      <head>
        {/* 黒曜石コンソールの書体: 明朝(見出し) / ゴシック(本文) / 等幅(数字) / ドット(発車標) */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@400;500;600;700;800&family=Noto+Sans+JP:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=DotGothic16&display=swap"
          rel="stylesheet"
        />
      </head>
      <body style={{ margin: 0, background: '#0B0D12', color: '#E9E7DF', minHeight: '100vh' }}>
        <ClientLayout>{children}</ClientLayout>
      </body>
    </html>
  )
}

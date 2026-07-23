'use client'

import TopNav from './TopNav'
import SecondaryNav from './SecondaryNav'

export default function ClientLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <TopNav />
      <SecondaryNav />
      <main style={{
        minHeight: 'calc(100vh - 56px)',
        position: 'relative',
        zIndex: 1,
        padding: 'clamp(16px, 3vw, 32px) clamp(16px, 3vw, 36px)',
      }}>
        {children}
      </main>
    </>
  )
}

'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { OPS } from '@/components/today/ops/tokens'

type NavEntry = { href: string; label: string; ai?: boolean }

const GROUPS: Array<{ matches: string[]; items: NavEntry[] }> = [
  {
    matches: ['/strategy', '/scenarios', '/screening', '/disclosures', '/decision', '/agent', '/performance'],
    items: [
      { href: '/strategy', label: '戦略' },
      { href: '/scenarios', label: 'シナリオ' },
      { href: '/screening', label: 'スクリーニング' },
      { href: '/disclosures', label: '開示AI' },
      { href: '/decision', label: 'AI判断', ai: true },
      { href: '/agent', label: 'AI分析', ai: true },
      { href: '/performance', label: '検証' },
    ],
  },
  {
    matches: ['/portfolio', '/nisa', '/risk', '/margin', '/cash', '/admin'],
    items: [
      { href: '/portfolio', label: 'ポートフォリオ' },
      { href: '/nisa', label: 'NISA' },
      { href: '/risk', label: 'リスク' },
      { href: '/margin', label: '信用' },
      { href: '/cash', label: '入出金' },
      { href: '/admin', label: '積立・持株会' },
    ],
  },
  { matches: ['/executions', '/history'], items: [{ href: '/executions', label: '執行台帳' }] },
  { matches: ['/tuning'], items: [{ href: '/tuning', label: 'チューニング' }] },
  { matches: ['/design'], items: [{ href: '/design', label: '稼働状況' }] },
]

export default function SecondaryNav() {
  const pathname = usePathname()
  const group = GROUPS.find(candidate => candidate.matches.some(prefix => pathname.startsWith(prefix)))
  if (!group) return null

  return (
    <nav aria-label="セカンダリナビゲーション" style={{ position: 'relative', zIndex: 2, borderBottom: `1px solid ${OPS.hairline}`, background: OPS.bg, padding: '0 clamp(16px, 3vw, 36px)', overflowX: 'auto' }}>
      <div style={{ display: 'flex', gap: 2, minWidth: 'max-content', height: 36, alignItems: 'stretch' }}>
        {group.items.map(item => {
          const active = pathname.startsWith(item.href)
          return (
            <Link key={item.href} href={item.href} style={{ position: 'relative', display: 'inline-flex', alignItems: 'center', gap: 5, textDecoration: 'none', color: active ? OPS.gold : OPS.dim, background: 'transparent', border: 'none', borderBottom: `2px solid ${active ? OPS.gold : 'transparent'}`, padding: '0 10px', fontFamily: OPS.mono, fontSize: 11.5, fontWeight: active ? 600 : 400, whiteSpace: 'nowrap' }}>
              {item.ai && <span style={{ color: OPS.amber, fontFamily: OPS.mono, fontSize: 10 }}>⚡</span>}
              {item.label}
            </Link>
          )
        })}
      </div>
    </nav>
  )
}

'use client'

import Image from 'next/image'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { useEffect, useRef, useState, useCallback } from 'react'
import useSWR from 'swr'
import { fetcher, type DashboardData } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'

type NavItem = { href: string; label: string; icon: string; matches: string[] }

const NAV: NavItem[] = [
  { href: '/', label: '市場判断', icon: '◆', matches: ['/', '/today'] },
  { href: '/portfolio', label: '資産', icon: '◉', matches: ['/portfolio', '/nisa', '/risk', '/margin', '/cash', '/admin'] },
  { href: '/executions', label: '履歴', icon: '▤', matches: ['/executions', '/history'] },
  { href: '/strategy', label: 'リサーチ', icon: '◈', matches: ['/strategy', '/scenarios', '/screening', '/disclosures', '/decision', '/agent', '/performance'] },
  { href: '/tuning', label: '設定', icon: '⚙', matches: ['/tuning'] },
  { href: '/design', label: 'システム', icon: '◌', matches: ['/design'] },
]

function isActive(item: NavItem, pathname: string): boolean {
  return item.href === '/'
    ? pathname === '/' || pathname.startsWith('/today')
    : item.matches.some(prefix => pathname.startsWith(prefix))
}

export default function TopNav() {
  const pathname = usePathname()
  const { data: dash } = useSWR<DashboardData>('/api/dashboard', fetcher, {
    refreshInterval: 60000,
    revalidateOnFocus: false,
  })

  const navRef = useRef<HTMLElement>(null)
  const linkRefs = useRef<Record<string, HTMLAnchorElement | null>>({})
  const [indicator, setIndicator] = useState<{ left: number; width: number; ready: boolean }>({
    left: 0,
    width: 0,
    ready: false,
  })

  const activeHref = NAV.find(n => isActive(n, pathname))?.href ?? '/'

  const measure = useCallback(() => {
    const el = linkRefs.current[activeHref]
    const nav = navRef.current
    if (!el || !nav) return
    const elRect = el.getBoundingClientRect()
    const navRect = nav.getBoundingClientRect()
    setIndicator({
      left: elRect.left - navRect.left + nav.scrollLeft,
      width: elRect.width,
      ready: true,
    })
  }, [activeHref])

  useEffect(() => {
    const raf = requestAnimationFrame(measure)
    return () => cancelAnimationFrame(raf)
  }, [measure, dash])

  useEffect(() => {
    window.addEventListener('resize', measure)
    const nav = navRef.current
    nav?.addEventListener('scroll', measure)
    return () => {
      window.removeEventListener('resize', measure)
      nav?.removeEventListener('scroll', measure)
    }
  }, [measure])

  const total = dash?.portfolio_total
  const totalM = total != null ? (total / 1_000_000).toFixed(1) : null
  const health = dash?.data_health
  const healthLabel = !health ? '確認中' : (health.missing_count ?? 0) > 0 ? '障害' : health.ok ? '正常' : 'データ遅延'
  const healthColor = !health ? OPS.dim : (health.missing_count ?? 0) > 0 ? OPS.vermilion : health.ok ? OPS.green : OPS.amber
  const healthBg = !health ? 'transparent' : health.ok ? OPS.greenBg : (health.missing_count ?? 0) > 0 ? OPS.vermilionBg : OPS.amberBg

  return (
    <header
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 50,
        height: 72,
        background: 'rgba(4,15,29,0.96)',
        backdropFilter: 'blur(20px) saturate(1.1)',
        WebkitBackdropFilter: 'blur(20px) saturate(1.1)',
        borderBottom: `1px solid ${OPS.hairline}`,
        display: 'flex',
        alignItems: 'center',
        padding: '0 28px',
      }}
    >
      {/* ── Wordmark ── */}
      <Link
        href="/"
        style={{
          textDecoration: 'none',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          marginRight: 42,
          flexShrink: 0,
        }}
      >
        <div style={{ width: 42, height: 42, flexShrink: 0, position: 'relative', borderRadius: 6, overflow: 'hidden' }}>
          {/* almanac-mark.png は黒背景+白マーク。透過版が無いため mix-blend-mode:screen で
              黒を消し、ヘッダーの地(OPS.bg)に馴染ませる — 黒地の画像そのものは変更しない。 */}
          {/* alt="" — 隣の "ALMANAC" 文字と同じリンク内にあるため、装飾として扱う
              (alt を付けるとスクリーンリーダーが "ALMANAC ALMANAC" と二重に読み上げる)。 */}
          <Image src="/almanac-mark.png" alt="" fill sizes="42px" priority
            style={{ objectFit: 'contain', mixBlendMode: 'screen' }} />
        </div>
        <span
          className="hidden sm:inline"
          style={{
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            lineHeight: 1,
          }}
        >
          <strong style={{ fontFamily: OPS.brand, color: OPS.gold, fontSize: 27, fontWeight: 600, letterSpacing: '0.105em' }}>ALMANAC</strong>
          <small style={{ color: OPS.sub, fontFamily: OPS.display, fontSize: 8.5, letterSpacing: '0.2em', marginTop: 5 }}>MARKET OBSERVATORY</small>
        </span>
      </Link>

      {/* ── Nav ── */}
      <nav
        ref={navRef}
        style={{
          position: 'relative',
          display: 'flex',
          alignItems: 'center',
          gap: 0,
          flex: 1,
          overflowX: 'auto',
          height: '100%',
          msOverflowStyle: 'none',
          scrollbarWidth: 'none',
        }}
      >
        {NAV.map(({ href, label, icon, ...item }) => {
          const active = isActive({ href, label, icon, ...item }, pathname)
          return (
            <div key={href} style={{ display: 'flex', alignItems: 'center', height: '100%' }}>
              <Link
                ref={el => {
                  linkRefs.current[href] = el
                }}
                href={href}
                className="nav-link"
                data-active={active}
                style={{
                  textDecoration: 'none',
                  padding: '6px 17px',
                  fontFamily: OPS.display,
                  fontSize: 16,
                  fontWeight: active ? 600 : 400,
                  color: active ? OPS.gold : OPS.dim,
                  whiteSpace: 'nowrap',
                  flexShrink: 0,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 0,
                  letterSpacing: '0.05em',
                  transition: 'color .18s ease',
                  height: '100%',
                }}
              >
                <span className="hidden sm:inline">{label}</span>
              </Link>
            </div>
          )
        })}

        {/* sliding gold indicator */}
        <span
          aria-hidden
          style={{
            position: 'absolute',
            bottom: 0,
            left: indicator.left,
            width: indicator.width,
            height: 2,
            background: `linear-gradient(90deg, ${OPS.gold}, ${OPS.amber})`,
            boxShadow: `0 0 8px ${OPS.gold}88`,
            borderRadius: 2,
            transition: indicator.ready ? 'left .28s cubic-bezier(.4,0,.2,1), width .28s cubic-bezier(.4,0,.2,1)' : 'none',
            opacity: indicator.ready ? 1 : 0,
          }}
        />
      </nav>

      {/* ── Right: portfolio value + derived system health ── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0, marginLeft: 12 }}>
        <span className="hidden lg:inline" style={{ color: OPS.sub, fontFamily: OPS.display, fontSize: 12.5, letterSpacing: '.1em', paddingRight: 2 }}>市場観測所</span>
        {totalM != null && (
          <span
            className="hidden sm:inline"
            style={{
            fontFamily: OPS.display,
            fontSize: 15,
              fontWeight: 600,
              color: OPS.text,
              letterSpacing: '-0.01em',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            ¥{totalM}M
          </span>
        )}
        <span
          style={{
            fontFamily: OPS.mono,
            fontSize: 11,
            letterSpacing: '0.12em',
            padding: '3px 10px',
            borderRadius: 999,
            background: healthBg,
            color: healthColor,
            border: `1px solid ${healthColor}33`,
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            flexShrink: 0,
          }}
        >
          <span
            className="nav-live-dot"
            style={{
              width: 5,
              height: 5,
              borderRadius: '50%',
              background: healthColor,
              display: 'inline-block',
            }}
          />
          {healthLabel}
        </span>
      </div>
    </header>
  )
}

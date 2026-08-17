import Image from 'next/image'

import { OPS } from '@/components/today/ops/tokens'

export default function BrandLoader({ label = 'ALMANAC LOADING…' }: { label?: string }) {
  return (
    <div className="brand-loader" role="status" aria-live="polite" aria-label={label}>
      <div className="brand-loader-mark" aria-hidden>
        <Image src="/almanac-mark-primary.png" alt="" width={58} height={58} priority />
      </div>
      <div className="brand-loader-copy" aria-hidden>
        <div style={{ color: OPS.text, fontFamily: OPS.brand }}>ALMANAC</div>
        <span>{label.replace(/^ALMANAC\s*/i, '') || 'LOADING…'}</span>
      </div>
      <div className="brand-loader-track" aria-hidden><i /></div>
    </div>
  )
}

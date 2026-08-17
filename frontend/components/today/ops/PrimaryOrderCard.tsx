'use client'

import { OPS, TYPE_META, QUADRANT_COLOR, fmtJpy, remainingLabel } from './tokens'
import type { BoardRow } from './types'

/**
 * 最優先の発注1件を主役として組むカード。
 *
 * 一覧の1行に詰め込むと「何株を・いくらで・いくら動くのか」が読み取れないので、
 * ラベル付きの数値グリッドにして値そのものを主役にする。
 *
 * 操作は既存の記録モーダルへ寄せる。モックには 採用 / 保留 / 棄却 の3ボタンが
 * あるが、「保留」に対応する記録先がデータモデルに無く、3分岐を作ると
 * 押しても何も残らないボタンが生まれる。実際に記録できる導線だけを置く。
 */

/** amount_hint から数量だけ取り出す（例: "買い増し 12株 @¥2,185" → "12株"） */
function quantityOf(row: BoardRow): string | null {
  const m = /([\d,]+(?:\.\d+)?)\s*(株|口)/.exec(row.amount_hint ?? '')
  return m ? `${m[1]}${m[2]}` : null
}

function Field({ label, value, tone, note }: { label: string; value: string; tone?: string; note?: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
        <span aria-hidden style={{ color: OPS.gold, fontSize: 10 }}>›</span>
        <span style={{ fontFamily: OPS.sans, fontSize: 11.5, color: OPS.dim }}>{label}</span>
      </div>
      <div style={{ fontFamily: OPS.mono, fontSize: 21, fontWeight: 600, color: tone ?? OPS.text, marginTop: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {value}
      </div>
      {note && <div style={{ fontSize: 10.5, color: OPS.dim, marginTop: 2 }}>{note}</div>}
    </div>
  )
}

export default function PrimaryOrderCard({ row, quadrant, onOpen }: {
  row: BoardRow
  quadrant?: string | null
  onOpen: () => void
}) {
  const meta = TYPE_META[row.type ?? ''] ?? { label: row.action ?? '—', color: OPS.gold }
  const qty = quantityOf(row)
  const price = row.limit_price != null
    ? `${row.ticker?.endsWith('.T') ? '¥' : '$'}${row.limit_price.toLocaleString()}`
    : '成行'
  const remaining = remainingLabel(row.expiry_ends_at)

  return (
    <section
      className="primary-order"
      aria-label={`最優先の発注 ${row.ticker}`}
      style={{
        border: `1px solid ${OPS.border}`,
        borderLeft: `3px solid ${meta.color}`,
        borderRadius: 10,
        background: OPS.panel,
        padding: '15px 18px 14px',
      }}
    >
      <style dangerouslySetInnerHTML={{ __html: `
        .primary-order-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px 18px; margin-top:13px; }
        @container ops-content (max-width:720px) { .primary-order-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
      ` }} />

      <div style={{ display: 'flex', alignItems: 'baseline', gap: 11, flexWrap: 'wrap' }}>
        <span className="ops-latin" style={{ fontSize: 10, color: OPS.gold }}>TOP PRIORITY</span>
        <span style={{ fontFamily: OPS.mono, fontSize: 25, fontWeight: 700, color: OPS.text, letterSpacing: '-.01em' }}>{row.ticker}</span>
        <span style={{ fontFamily: OPS.display, fontSize: 19, fontWeight: 600, color: meta.color }}>{meta.label}</span>
        {quadrant && (
          <span style={{ marginLeft: 'auto', fontSize: 11.5, color: QUADRANT_COLOR[quadrant] ?? OPS.dim }}>◎ {quadrant}</span>
        )}
      </div>

      <div className="primary-order-grid">
        <Field label="注文数量" value={qty ?? '—'} note={qty ? undefined : '数量は記録時に確定'} />
        <Field label={row.order_type === 'market' ? '注文価格' : '指値 (Limit)'} value={price} tone={OPS.gold} />
        <Field label="確信度" value={row.confidence_pct != null ? `${row.confidence_pct}%` : '—'} />
        <Field label="見積金額" value={fmtJpy(row.estimated_notional_jpy)} />
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginTop: 14, paddingTop: 12, borderTop: `1px solid ${OPS.hairline}` }}>
        <button
          type="button"
          className="ops-btn"
          onClick={onOpen}
          style={{ background: OPS.greenBg, border: `1px solid ${OPS.green}77`, color: OPS.green, fontFamily: OPS.sans, fontSize: 13.5, fontWeight: 600, padding: '9px 18px' }}
        >
          ✓ 発注を記録する
        </button>
        {remaining && (
          <span style={{ fontFamily: OPS.mono, fontSize: 11.5, color: remaining.over ? OPS.vermilion : OPS.sub }}>
            {remaining.label}
          </span>
        )}
        {/* 最優先の1件こそ発注直前の注意が要る。一覧行から昇格させる際に落とさない */}
        {row.lifecycle.expiry_deferred_until_reprice && row.lifecycle.market_reprice_after && (
          <span style={{ fontFamily: OPS.mono, fontSize: 11.5, color: OPS.amber }}>次回朝分析で再評価</span>
        )}
        {row.market_quote_confirmation_required && (
          <span style={{ fontFamily: OPS.mono, fontSize: 11.5, color: OPS.amber }}>発注時に現在値確認</span>
        )}
        <span style={{ marginLeft: 'auto', fontSize: 11, color: OPS.dim }}>
          安全ゲート・予算ゲート通過済み
        </span>
      </div>
    </section>
  )
}

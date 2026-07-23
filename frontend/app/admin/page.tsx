'use client'

import { useState } from 'react'
import useSWR, { useSWRConfig } from 'swr'
import { fetcher, apiFetch } from '@/lib/api'
import { OPS } from '@/components/today/ops/tokens'
import { OpsPage, Panel, PanelTitle, Stat, Chip, Modal, Loading, Grid } from '@/components/today/ops/PageKit'

interface Purchase { date?: string; shares?: number; units?: number; price?: number; nav?: number; cost?: number; amount?: number; incentive?: number; type?: string }
interface Espp {
  ticker?: string; monthly_amount?: number; monthly_employee?: number; monthly_incentive?: number
  incentive_rate?: number; hold_limit_pct?: number; current_shares?: number; avg_cost?: number
  total_invested?: number; total_incentive?: number; last_purchase_date?: string; notes?: string
  purchase_history?: Purchase[]
}
interface CardPlan {
  plan_key?: string; monthly_amount?: number; fund?: string; broker?: string; card?: string
  point_rate?: number; current_units?: number; avg_nav?: number; total_invested?: number
  current_nav?: number; last_purchase_date?: string; notes?: string; purchase_history?: Purchase[]
}
interface AdminData { espp: Espp; credit_card: { husband?: CardPlan; wife?: CardPlan } }

function yen(v?: number): string { return v == null ? '—' : `¥${Math.round(v).toLocaleString()}` }

export default function AdminPage() {
  const { mutate } = useSWRConfig()
  const { data, isLoading } = useSWR<AdminData>('/api/admin', fetcher, { refreshInterval: 300000 })
  const [record, setRecord] = useState<'husband' | 'wife' | null>(null)
  const [historyOf, setHistoryOf] = useState<null | { title: string; rows: Purchase[] }>(null)

  return (
    <OpsPage
      en="AUTO-INVEST"
      title="積立・持株会"
      subtitle="持株会（9999.T）の集中度管理と、夫婦のクレカ積立（オルカン）の記録。毎月の定額拠出をここで追跡する。"
    >
      {isLoading && <Loading />}
      {data && (
        <>
          {/* 持株会 */}
          <Panel pad="18px 20px">
            <PanelTitle right={`${data.espp.ticker} · 更新 ${data.espp.last_purchase_date}`}>持株会</PanelTitle>
            <Grid cols={4} gap={12}>
              <Stat label="保有株数" value={data.espp.current_shares?.toFixed(1) ?? '—'} unit="株" color={OPS.gold} />
              <Stat label="平均取得単価" value={yen(data.espp.avg_cost)} />
              <Stat label="累計拠出" value={yen(data.espp.total_invested)} sub={`奨励金 ${yen(data.espp.total_incentive)}`} />
              <Stat label="月額拠出" value={yen(data.espp.monthly_amount)} sub={`本人 ${yen(data.espp.monthly_employee)} + 奨励 ${yen(data.espp.monthly_incentive)}`} />
            </Grid>
            <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginTop: 16 }}>
              <span style={{ fontSize: 12, color: OPS.sub }}>奨励金率 <span style={{ color: OPS.green, fontFamily: OPS.mono }}>{((data.espp.incentive_rate ?? 0) * 100).toFixed(0)}%</span></span>
              <span style={{ fontSize: 12, color: OPS.sub }}>集中度上限 <span style={{ color: OPS.amber, fontFamily: OPS.mono }}>{((data.espp.hold_limit_pct ?? 0) * 100).toFixed(0)}%</span></span>
              <button
                onClick={() => setHistoryOf({ title: '持株会 拠出履歴', rows: data.espp.purchase_history ?? [] })}
                style={linkBtn}
              >
                拠出履歴 {data.espp.purchase_history?.length ?? 0} 件 →
              </button>
            </div>
            {data.espp.notes && <p style={{ fontSize: 11.5, color: OPS.dim, lineHeight: 1.6, margin: '12px 0 0' }}>{data.espp.notes}</p>}
          </Panel>

          {/* クレカ積立 */}
          <div style={{ marginTop: 22 }}>
            <Grid cols={2} gap={16}>
              {(['husband', 'wife'] as const).map(k => {
                const plan = data.credit_card[k]
                if (!plan) return null
                const thisMonth = new Date().toISOString().slice(0, 7)
                const done = (plan.purchase_history ?? []).some(p => p.date?.startsWith(thisMonth))
                return (
                  <Panel key={k} pad="16px 18px">
                    <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}>
                      <span style={{ fontSize: 14, fontWeight: 600, color: OPS.text }}>
                        {k === 'husband' ? '本人' : '妻'} クレカ積立
                      </span>
                      <span style={{ marginLeft: 'auto' }}>
                        <Chip color={done ? OPS.green : OPS.amber} bg={done ? OPS.greenBg : OPS.amberBg} mono>
                          {done ? '今月完了' : '今月未記録'}
                        </Chip>
                      </span>
                    </div>
                    <div style={{ fontFamily: OPS.mono, fontSize: 22, color: OPS.gold, marginBottom: 2 }}>{yen(plan.monthly_amount)}<span style={{ fontSize: 12, color: OPS.dim }}>/月</span></div>
                    <div style={{ fontSize: 11.5, color: OPS.sub, lineHeight: 1.7, marginBottom: 12 }}>
                      {plan.fund}<br />
                      {plan.broker} · {plan.card} · 還元 {((plan.point_rate ?? 0) * 100).toFixed(1)}%
                    </div>
                    <div style={{ display: 'flex', gap: 18, fontFamily: OPS.mono, fontSize: 12, color: OPS.sub, marginBottom: 12 }}>
                      <span><span style={{ color: OPS.dim }}>累計</span> {yen(plan.total_invested)}</span>
                      <span><span style={{ color: OPS.dim }}>口数</span> {plan.current_units?.toFixed(1) ?? '0'}</span>
                    </div>
                    <div style={{ display: 'flex', gap: 8 }}>
                      <button onClick={() => setRecord(k)} style={primaryBtn}>積立を記録</button>
                      <button onClick={() => setHistoryOf({ title: `${k === 'husband' ? '本人' : '妻'} 積立履歴`, rows: plan.purchase_history ?? [] })} style={linkBtn}>
                        履歴 {plan.purchase_history?.length ?? 0} 件 →
                      </button>
                    </div>
                  </Panel>
                )
              })}
            </Grid>
          </div>
        </>
      )}

      <RecordForm person={record} onClose={() => setRecord(null)} onDone={() => mutate('/api/admin')} />

      <Modal open={!!historyOf} onClose={() => setHistoryOf(null)} width={560}>
        {historyOf && (
          <>
            <h2 style={{ fontSize: 17, fontWeight: 700, color: OPS.text, margin: '0 0 14px' }}>{historyOf.title}</h2>
            <div style={{ maxHeight: '60vh', overflowY: 'auto' }}>
              {historyOf.rows.slice().reverse().map((p, i) => (
                <div key={i} className="ops-row" style={{ display: 'flex', gap: 12, padding: '7px 4px', borderTop: `1px solid ${OPS.hairline}`, fontSize: 12.5, fontFamily: OPS.mono }}>
                  <span style={{ color: OPS.dim, minWidth: 82 }}>{p.date}</span>
                  <span style={{ color: OPS.text }}>{yen(p.cost ?? p.amount)}</span>
                  <span style={{ color: OPS.sub }}>{p.shares != null ? `${p.shares.toFixed(2)}株` : p.units != null ? `${p.units.toFixed(2)}口` : ''}</span>
                  <span style={{ color: OPS.dim }}>@ {p.price ?? p.nav ?? '—'}</span>
                  {p.incentive != null && p.incentive > 0 && <span style={{ color: OPS.green }}>+奨励 {yen(p.incentive)}</span>}
                  {p.type && <span style={{ marginLeft: 'auto', color: OPS.dim }}>{p.type}</span>}
                </div>
              ))}
              {historyOf.rows.length === 0 && <p style={{ fontSize: 12, color: OPS.dim }}>履歴なし</p>}
            </div>
          </>
        )}
      </Modal>
    </OpsPage>
  )
}

function RecordForm({ person, onClose, onDone }: { person: 'husband' | 'wife' | null; onClose: () => void; onDone: () => void }) {
  const [amount, setAmount] = useState('100000')
  const [nav, setNav] = useState('')
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10))
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState<{ ok: boolean; msg: string } | null>(null)

  async function submit() {
    const navNum = parseFloat(nav)
    if (!navNum || navNum <= 0) { setFlash({ ok: false, msg: 'NAVを入力してください' }); return }
    setBusy(true); setFlash(null)
    try {
      const res = await apiFetch('/api/admin/credit-card/purchase', {
        method: 'POST',
        body: JSON.stringify({ person, amount: parseInt(amount), nav: navNum, purchase_date: date }),
      })
      const json = await res.json().catch(() => ({}))
      if (!res.ok || json?.ok === false) {
        setFlash({ ok: false, msg: '失敗: ' + (json?.detail ?? res.statusText) })
      } else {
        setFlash({ ok: true, msg: '記録しました' })
        onDone()
        setTimeout(onClose, 1000)
      }
    } catch (e) {
      setFlash({ ok: false, msg: String(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={!!person} onClose={onClose} width={420}>
      <h2 style={{ fontSize: 17, fontWeight: 700, color: OPS.text, margin: '0 0 16px' }}>
        {person === 'husband' ? '本人' : '妻'} 積立を記録
      </h2>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <Field label="金額"><input value={amount} onChange={e => setAmount(e.target.value)} style={inputSt} /></Field>
        <Field label="約定 NAV"><input value={nav} onChange={e => setNav(e.target.value)} placeholder="例: 28500" style={inputSt} /></Field>
        <Field label="日付"><input type="date" value={date} onChange={e => setDate(e.target.value)} style={inputSt} /></Field>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 6 }}>
          <button onClick={submit} disabled={busy} style={{ ...primaryBtn, opacity: busy ? 0.5 : 1 }}>{busy ? '保存中…' : '記録する'}</button>
          {flash && <span style={{ fontSize: 12.5, color: flash.ok ? OPS.green : OPS.redSoft }}>{flash.msg}</span>}
        </div>
      </div>
    </Modal>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <span style={{ fontSize: 11, color: OPS.dim, fontFamily: OPS.mono }}>{label}</span>
      {children}
    </label>
  )
}

const primaryBtn: React.CSSProperties = { background: OPS.goldBg, border: `1px solid ${OPS.gold}66`, borderRadius: 6, color: OPS.gold, fontSize: 13, fontWeight: 600, padding: '7px 16px', cursor: 'pointer', fontFamily: OPS.sans }
const linkBtn: React.CSSProperties = { background: 'none', border: 'none', color: OPS.blue, fontSize: 12, fontFamily: OPS.mono, cursor: 'pointer', padding: 0 }
const inputSt: React.CSSProperties = { background: OPS.panelAlt, border: `1px solid ${OPS.border}`, borderRadius: 6, color: OPS.text, fontSize: 13, padding: '8px 10px', fontFamily: OPS.mono, outline: 'none', width: '100%', boxSizing: 'border-box' }

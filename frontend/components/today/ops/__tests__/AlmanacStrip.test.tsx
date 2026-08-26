import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AlmanacStrip, { jstHHMM, jstMinutesOfDay } from '../AlmanacStrip'
import type { AlmanacData, ExecutionPlan } from '../types'

const plan: ExecutionPlan = {
  status: 'active',
  age_hours: 3,
  horizon: { month: '2026-07', week_start: '2026-07-13', week_end: '2026-07-19' },
  budgets: {
    monthly_total_jpy: 300_000,
    weekly_normal_jpy: 50_000,
    weekly_opportunity_reserve_jpy: 20_000,
    weekly_defensive_reserve_jpy: 10_000,
    max_single_normal_action_jpy: 30_000,
    max_single_opportunity_action_jpy: 20_000,
    scheduled_contributions_remaining_jpy: 25_000,
  },
  consumption: {
    monthly_consumed_jpy: 60_000,
    monthly_remaining_jpy: 240_000,
    normal_plan_budget_consumed_pct: 20,
    remaining_normal_jpy: 40_000,
  },
  summary: { items_total: 2, active_items: 2, covered_items: 0, board_count: 3, plan_filtered_count: 1 },
  items: [
    { plan_item_id: 'wife-nisa', label: '妻NISAを優先', objective: 'wife_nisa_growth_capacity', status: 'active', priority: 1, normal_budget_jpy: 30_000, preferred_tickers: [], consumed_by_count: 0, source_reasons: [] },
    { plan_item_id: 'usd', label: 'USD比率を補正', objective: 'add_currency_usd', status: 'active', priority: 2, normal_budget_jpy: 20_000, preferred_tickers: [], consumed_by_count: 0, source_reasons: [] },
  ],
  today_decision: { code: 'wait_candidate', label: '候補待ち', reason: '条件待ちです。' },
  filtered_summary: {},
  filtered_examples: [],
  warnings: [],
  no_action_rationale: [],
}

const disabledPlan: ExecutionPlan = {
  ...plan,
  status: 'disabled',
  today_decision: { code: 'disabled', label: '計画レイヤー無効', reason: '最新の計画を生成できません。' },
}

const almanac: AlmanacData = {
  today: [],
  sessions: [
    { id: 'jpx-am', label: '東証 前場', market: 'JP', phase: 'regular', start: '09:00', end: '11:30', is_open_day: true },
    { id: 'jpx-pm', label: '東証 後場', market: 'JP', phase: 'regular', start: '12:30', end: '15:30', is_open_day: true },
    { id: 'us-pre', label: '米国 プレ', market: 'US', phase: 'pre', start: '17:00', end: '22:30', is_open_day: true },
    { id: 'us-regular', label: '米国 通常', market: 'US', phase: 'regular', start: '22:30', end: '05:00', is_open_day: true },
    { id: 'us-after', label: '米国 アフター', market: 'US', phase: 'after', start: '05:00', end: '09:00', is_open_day: true },
  ],
  upcoming: [
    { date: '2026-07-01', label: '古い予定', kind: 'earnings', ticker: 'OLD.T' },
    { date: '2026-07-08', label: '先週の予定', kind: 'earnings', ticker: 'LAST.T' },
    { date: '2026-07-20', label: 'NISA積立', kind: 'nisa' },
    { date: '2026-07-23', label: 'RTX 決算', kind: 'earnings', ticker: 'RTX' },
  ],
  past: [{ date: '2026-07-08', kind: 'trade', ticker: '1489.T', side: 'buy', detail: '100株買付' }],
  pnl_by_date: { '2026-07-08': 20_000 },
  notes: [],
  is_weekday: true,
  today_str: '2026-07-15',
}

describe('AlmanacStrip', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-15T12:00:00+09:00'))
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('fills the future-week plan column with one monthly stack', () => {
    render(<AlmanacStrip almanac={almanac} plan={plan} />)
    fireEvent.click(screen.getByRole('button', { name: '複数週を詳しく見る' }))

    expect(screen.getByRole('button', { name: '7/13–7/19の計画詳細' })).toBeInTheDocument()
    expect(screen.getByLabelText('各週の計画と結果')).toBeInTheDocument()
    // 週次カードは「先々週・先週・今週」の3つだけ。未来4週分の空の計画枠は出さず、
    // その領域は月次スタックが1ブロックで占める。
    expect(screen.getAllByLabelText(/の週次計画と結果$/)).toHaveLength(3)
    expect(screen.queryByLabelText(/に表示する月次計画$/)).not.toBeInTheDocument()
    expect(screen.queryByText('週次計画は未策定')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('週内の日次損益')).not.toBeInTheDocument()

    // 4項目は右カラムの1ブロックに縦積みされる
    const monthlyStack = screen.getByLabelText('今月の計画')
    expect(within(monthlyStack).getByText('今月の投資余力')).toBeInTheDocument()
    expect(within(monthlyStack).getByText('現在の配分設計')).toBeInTheDocument()
    expect(within(monthlyStack).getByText('優先配分キュー')).toBeInTheDocument()
    expect(within(monthlyStack).getByText('リスク境界')).toBeInTheDocument()
    expect(within(monthlyStack).getByText('¥30万')).toBeInTheDocument()
    expect(within(monthlyStack).getByText('残 ¥24万')).toBeInTheDocument()
    expect(within(monthlyStack).getByText('2026.07')).toBeInTheDocument()
    expect(screen.queryByText('PLAN PREVIEW')).not.toBeInTheDocument()
    expect(screen.queryByText('WEEK OUTLOOK')).not.toBeInTheDocument()
    expect(screen.queryByText('予定 2件')).not.toBeInTheDocument()
    expect(screen.getByText('●OLD.T')).toBeInTheDocument()
    expect(screen.getByText('●LAST.T')).toBeInTheDocument()
    expect(screen.getByText('●NISA積立')).toBeInTheDocument()
    expect(screen.getByText('●RTX')).toBeInTheDocument()
    expect(screen.getByText('表示範囲 先々週〜4週先')).toBeInTheDocument()
    expect(screen.getByText('1489.T')).toBeInTheDocument()
    expect(screen.getAllByText('+¥2万').length).toBeGreaterThan(0)
  })

  it('shows the active cross-midnight US session instead of only Tokyo hours', () => {
    vi.setSystemTime(new Date('2026-07-15T23:15:00+09:00'))
    render(<AlmanacStrip almanac={almanac} plan={plan} />)
    fireEvent.click(screen.getByRole('button', { name: '複数週を詳しく見る' }))

    expect(screen.getByText('米国 通常 取引中')).toBeInTheDocument()
    expect(screen.getByText('東証 前場')).toBeInTheDocument()
    expect(screen.getAllByText('米国 通常').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('本日の市場タイムライン')).toHaveTextContent('米国 プレ')
    expect(screen.getByLabelText('本日の市場タイムライン')).toHaveTextContent('米国 アフター')
  })

  it('does not present a disabled plan as active or actionable', () => {
    render(<AlmanacStrip almanac={almanac} plan={disabledPlan} />)
    fireEvent.click(screen.getByRole('button', { name: '複数週を詳しく見る' }))

    expect(screen.queryByRole('button', { name: '7/13–7/19の計画詳細' })).not.toBeInTheDocument()
    expect(screen.getAllByText('計画レイヤー無効').length).toBeGreaterThan(1)
    // 月次スタックは1つなので、参照不能の告知も1つ
    expect(screen.getAllByText('月次計画を参照できません')).toHaveLength(1)
  })

  it('labels trade-only weeks as P&L pending', () => {
    render(<AlmanacStrip almanac={{ ...almanac, pnl_by_date: {} }} plan={plan} />)
    fireEvent.click(screen.getByRole('button', { name: '複数週を詳しく見る' }))

    expect(screen.getByText('損益未集計')).toBeInTheDocument()
    expect(screen.queryByLabelText('週次損益 ¥0')).not.toBeInTheDocument()
  })
})

describe('JST time helpers', () => {
  // ⚠️ Date.getHours()/getMinutes() はブラウザ/実行ホストのローカル
  // タイムゾーンを使う。この画面は「JST」と明示ラベルしているのに、
  // 以前は host-local な getHours() で計算していたため、UTC 設定の
  // CI ランナーでは「23:15 JST」が「14:15」と表示され、それに連動する
  // セッション判定 (米国/東証どちらが取引中か) も丸ごとずれていた
  // (レビューが導入した CI 分離で初めて UTC ホスト上で実行され、
  // AlmanacStrip.test.tsx の cross-midnight セッションテストが失敗して発覚)。
  // 日本は DST が無いので UTC+9 固定オフセットとして扱ってよい。
  it('reads JST regardless of what host-local getHours() would return', () => {
    // 23:15 JST = 14:15 UTC。host-local な実装ならホストのタイムゾーン
    // 設定次第で結果が変わってしまう箇所。
    const at2315JST = new Date('2026-07-15T23:15:00+09:00')
    expect(jstHHMM(at2315JST)).toBe('23:15')
    expect(jstMinutesOfDay(at2315JST)).toBe(23 * 60 + 15)
  })

  it('handles the JST midnight rollover correctly', () => {
    const at0005JST = new Date('2026-07-16T00:05:00+09:00')
    expect(jstHHMM(at0005JST)).toBe('00:05')
    expect(jstMinutesOfDay(at0005JST)).toBe(5)
  })
})

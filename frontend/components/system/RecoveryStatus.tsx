import { OPS } from '@/components/today/ops/tokens'
import { Panel, PanelTitle } from '@/components/today/ops/PageKit'

export function recoveryState(value: unknown): 'clear' | 'pending' | 'unknown' {
  if (!value || typeof value !== 'object') return 'unknown'
  const row = value as Record<string, unknown>
  if (row.execution_authorized !== false || !Number.isSafeInteger(row.pending_count) || (row.pending_count as number) < 0) return 'unknown'
  if (row.status === 'clear' && row.pending_count === 0) return 'clear'
  if (row.status === 'needs_reconciliation' && (row.pending_count as number) > 0) return 'pending'
  return 'unknown'
}

export default function RecoveryStatus({ portfolio, brokerImport, unavailable = false }: {
  portfolio?: unknown; brokerImport?: unknown; unavailable?: boolean
}) {
  return <Panel>
    <PanelTitle>残高更新の復旧状態</PanelTitle>
    <div data-testid="recovery-status" aria-live="polite">
      {([['約定反映', portfolio], ['残高インポート', brokerImport]] as const).map(([label, value]) => {
        const state = unavailable ? 'unknown' : recoveryState(value)
        const count = state === 'pending' ? (value as { pending_count: number }).pending_count : null
        return <p key={label} style={{ color: state === 'clear' ? OPS.dim : OPS.amber }}>
          {label}: {state === 'clear' ? '未復旧記録なし' : state === 'pending' ? `要照合（未復旧 ${count}件）` : '確認不能（0件とはみなしません）'}
        </p>
      })}
      <p style={{ color: OPS.dim, fontSize: 12 }}>対象の記録についての診断です。発注許可・全残高の整合性を保証しません。</p>
      <p style={{ color: OPS.dim, fontSize: 12 }}>要照合時は原本・未完了記録・現在残高を照合してください。入出金の409応答は未適用です。原本を保持し、確認後に再試行してください。この画面からの強制解除はできません。</p>
    </div>
  </Panel>
}

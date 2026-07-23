// シンプルなクライアントサイド pub/sub シングルトン
// サーバーサイドでは何もしない（SSR安全）

type Callback<T = unknown> = (data: T) => void

class EventBus {
  private listeners = new Map<string, Set<Callback>>()

  on<T = unknown>(event: string, cb: Callback<T>): () => void {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set())
    this.listeners.get(event)!.add(cb as Callback)
    return () => this.listeners.get(event)?.delete(cb as Callback)
  }

  emit<T = unknown>(event: string, data: T): void {
    this.listeners.get(event)?.forEach(cb => cb(data))
  }
}

// ブラウザのみで動作するシングルトン
// Next.js の SSR でも import できるよう typeof window チェック
const _bus = typeof window !== 'undefined' ? new EventBus() : new EventBus()
export const eventBus = _bus

// イベント型定義
export type TickerFocusEvent = string  // ティッカーシンボル（例: "NVDA"）

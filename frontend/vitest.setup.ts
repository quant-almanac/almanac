import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})

// jsdom はレイアウトエンジンを持たないため ResizeObserver が未実装。
// コンテナ幅の実測に使うコンポーネントが素通りできるよう、
// 最小限の no-op を用意する。座標を伴う検証は各テストで個別にモックし直す。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver
}

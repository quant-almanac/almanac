import { fileURLToPath } from 'node:url'
import { configDefaults, defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    css: false,
    // ⚠️ .claude/worktrees/ は他の並行セッションの独立した git worktree
    // で、.gitignore 済みなので CI には決して含まれない。だがローカルでは
    // デフォルトの include glob がそこまで拾ってしまい、同名テストファイル
    // (例: AlmanacStrip.test.tsx) が二重に実行されて実際のテスト件数
    // (追跡対象のみ) と食い違って見えていた (レビューで指摘: ローカル
    // 192件 vs CI/追跡対象154件)。デフォルトの exclude に明示的に追加する。
    exclude: [...configDefaults.exclude, '.claude/**'],
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('.', import.meta.url)),
    },
  },
})

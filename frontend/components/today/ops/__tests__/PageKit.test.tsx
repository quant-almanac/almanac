import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Modal } from '../PageKit'

describe('Modal', () => {
  it('announces itself as a modal dialog and keeps Tab focus inside', async () => {
    const onClose = () => undefined
    render(<Modal open onClose={onClose}><button>操作</button></Modal>)

    const dialog = screen.getByRole('dialog')
    const close = screen.getByRole('button', { name: '閉じる' })
    const action = screen.getByRole('button', { name: '操作' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toHaveAttribute('aria-labelledby')
    await waitFor(() => expect(close).toHaveFocus())

    action.focus()
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(close).toHaveFocus()
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(action).toHaveFocus()
  })
})

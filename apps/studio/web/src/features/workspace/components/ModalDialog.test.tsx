import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ModalDialog } from './ModalDialog'

function DialogHarness({ onConfirm = vi.fn() }: { onConfirm?: (value?: string) => void | Promise<void> }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>重命名</button>
      <ModalDialog
        open={open}
        title="重命名会话"
        inputLabel="会话名称"
        initialValue="旧标题"
        confirmLabel="保存"
        onConfirm={onConfirm}
        onCancel={() => setOpen(false)}
      />
    </>
  )
}

function MenuDialogHarness() {
  const [menuOpen, setMenuOpen] = useState(false)
  const [dialogOpen, setDialogOpen] = useState(false)
  const menuTrigger = useRef<HTMLButtonElement>(null)
  return (
    <>
      <button ref={menuTrigger} type="button" onClick={() => setMenuOpen(true)}>管理会话</button>
      {menuOpen && (
        <button type="button" onClick={() => {
          setMenuOpen(false)
          setDialogOpen(true)
        }}>重命名</button>
      )}
      <ModalDialog
        open={dialogOpen}
        title="重命名会话"
        inputLabel="会话名称"
        initialValue="旧标题"
        confirmLabel="保存"
        restoreFocusTo={menuTrigger.current}
        onConfirm={vi.fn()}
        onCancel={() => setDialogOpen(false)}
      />
    </>
  )
}

describe('ModalDialog', () => {
  it('traps focus, closes on Escape, and restores focus to the opener', async () => {
    const user = userEvent.setup()
    render(<DialogHarness />)
    const opener = screen.getByRole('button', { name: '重命名' })

    await user.click(opener)
    const dialog = screen.getByRole('dialog', { name: '重命名会话' })
    const input = screen.getByRole('textbox', { name: '会话名称' })
    expect(dialog).toHaveClass('modal-dialog--action', 'has-input', 'is-default')
    expect(screen.getByText('会话名称')).toHaveClass('visually-hidden')
    expect(input).toHaveFocus()

    screen.getByRole('button', { name: '关闭对话框' }).focus()
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
    expect(screen.getByRole('button', { name: '保存' })).toHaveFocus()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it('submits without closing itself and keeps pending controls unavailable', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    const { rerender } = render(
      <ModalDialog
        open
        title="删除会话"
        description="此操作不可恢复。"
        confirmLabel="删除"
        tone="danger"
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(onConfirm).toHaveBeenCalledWith(undefined)
    expect(screen.getByRole('dialog')).toHaveClass('modal-dialog--action', 'is-danger')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    rerender(
      <ModalDialog
        open
        title="删除会话"
        confirmLabel="删除"
        inputLabel="会话名称"
        initialValue="待处理会话"
        isPending
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: '处理中…' })).toBeDisabled()
    expect(screen.getByLabelText('会话名称')).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('status', { name: '加载中' })).toBeInTheDocument()
  })

  it('focuses cancel first for a confirmation dialog without an input', () => {
    render(
      <ModalDialog
        open
        title="删除会话"
        confirmLabel="删除"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: '取消' })).toHaveFocus()
  })

  it('restores focus to an explicit stable trigger after a menu item unmounts', async () => {
    const user = userEvent.setup()
    render(<MenuDialogHarness />)
    const trigger = screen.getByRole('button', { name: '管理会话' })

    await user.click(trigger)
    await user.click(screen.getByRole('button', { name: '重命名' }))
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })

    await waitFor(() => expect(trigger).toHaveFocus())
  })
})

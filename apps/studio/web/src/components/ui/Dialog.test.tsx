import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { Dialog } from './Dialog'

function Example({
  intercept = false,
  locked = false,
}: {
  intercept?: boolean
  locked?: boolean
}) {
  const [open, setOpen] = useState(false)
  const first = useRef<HTMLInputElement>(null)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        打开
      </button>
      <Dialog
        open={open}
        title="示例"
        description="说明"
        onClose={() => setOpen(false)}
        closeDisabled={locked}
        initialFocusRef={first}
        headerActions={
          <button type="button" disabled>
            暂不可用
          </button>
        }
        onKeyDown={(event) => {
          if (intercept && event.key === 'Escape') event.preventDefault()
        }}
      >
        <input ref={first} aria-label="内容" />
        <button type="button">最后一个</button>
      </Dialog>
    </>
  )
}

describe('Dialog组合契约', () => {
  it('更新恢复入口不重置弹框内焦点，关闭使用最新入口', async () => {
    const user = userEvent.setup()
    const original = document.createElement('button')
    const visible = document.createElement('button')
    document.body.append(original, visible)
    const { rerender } = render(<Dialog open title="示例" restoreFocusTo={original} onClose={vi.fn()}>
      <input aria-label="内容" />
    </Dialog>)
    await user.tab()
    const input = screen.getByRole('textbox', { name: '内容' })
    expect(input).toHaveFocus()
    rerender(<Dialog open title="示例" restoreFocusTo={visible} onClose={vi.fn()}>
      <input aria-label="内容" />
    </Dialog>)
    expect(input).toHaveFocus()
    rerender(<Dialog open={false} title="示例" restoreFocusTo={visible} onClose={vi.fn()}>内容</Dialog>)
    await Promise.resolve()
    expect(visible).toHaveFocus()
    original.remove()
    visible.remove()
  })

  it('关闭流程已指定下一处焦点时，不抢回原入口', async () => {
    const user = userEvent.setup()
    function RedirectExample() {
      const [open, setOpen] = useState(false)
      const next = useRef<HTMLInputElement>(null)
      return <>
        <button type="button" onClick={() => setOpen(true)}>打开</button>
        <input ref={next} aria-label="下一步" />
        <Dialog open={open} title="示例" onClose={() => {
          next.current?.focus()
          setOpen(false)
        }}>内容</Dialog>
      </>
    }
    render(<RedirectExample />)
    await user.click(screen.getByRole('button', { name: '打开' }))
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '下一步' })).toHaveFocus()
  })

  it('默认关闭、指定初始焦点、跳过禁用控件、焦点循环及恢复', async () => {
    const user = userEvent.setup()
    render(<Example />)
    await user.click(screen.getByRole('button', { name: '打开' }))
    expect(screen.getByRole('textbox')).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '最后一个' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '关闭对话框' })).toHaveFocus()
    await user.tab({ shift: true })
    expect(screen.getByRole('button', { name: '最后一个' })).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开' })).toHaveFocus()
  })
  it('内容处理Escape不关闭外层，关闭按钮仍可用', async () => {
    const user = userEvent.setup()
    render(<Example intercept />)
    await user.click(screen.getByRole('button', { name: '打开' }))
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeVisible()
    await user.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
  it('关闭禁用期间Escape不触发关闭', async () => {
    const user = userEvent.setup()
    const close = vi.fn()
    render(
      <Dialog open title="保存中" closeDisabled onClose={close}>
        正在保存
      </Dialog>,
    )
    expect(screen.getByRole('button', { name: '关闭对话框' })).toBeDisabled()
    await user.keyboard('{Escape}')
    expect(close).not.toHaveBeenCalled()
  })
  it('负tabIndex的结果按钮不占用对话框的Tab循环边界', async () => {
    const user = userEvent.setup()
    render(<Dialog open title="搜索" onClose={vi.fn()}>
      <input aria-label="查询" />
      <button type="button">范围</button>
      <button type="button" tabIndex={-1}>结果</button>
    </Dialog>)
    await user.tab()
    expect(screen.getByRole('textbox', { name: '查询' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '范围' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '关闭对话框' })).toHaveFocus()
  })

})

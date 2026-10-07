import { createRef } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { DrawerHeader } from './DrawerHeader'
import { IconButton } from './IconButton'
import { RefreshCw } from 'lucide-react'

describe('DrawerHeader', () => {
  it('标题操作位于关闭前，全宽返回形态保留操作', () => {
    const refresh = vi.fn()
    const actions = <IconButton label="刷新文件" icon={<RefreshCw size={18} />} onClick={refresh} />
    const { rerender } = render(<DrawerHeader title="工作区" actions={actions} closeLabel="关闭工作区" onClose={vi.fn()} />)
    expect(screen.getAllByRole('button').map(button => button.getAttribute('aria-label'))).toEqual(['刷新文件', '关闭工作区'])
    fireEvent.click(screen.getByRole('button', { name: '刷新文件' }))
    expect(refresh).toHaveBeenCalledOnce()
    rerender(<DrawerHeader title="工作区" actions={actions} backLabel="返回对话" onBack={vi.fn()} />)
    expect(screen.getAllByRole('button').map(button => button.getAttribute('aria-label'))).toEqual(['返回对话', '刷新文件'])
  })
  it('返回操作位于标题前并支持聚焦，点击后返回原页面', () => {
    const backButton = createRef<HTMLButtonElement>()
    const onBack = vi.fn()
    render(<DrawerHeader ref={backButton} title="任务轨迹" backLabel="返回对话" onBack={onBack} />)
    const button = screen.getByRole('button', { name: '返回对话' })
    expect(backButton.current).toBe(button)
    button.focus()
    expect(button).toHaveFocus()
    fireEvent.click(button)
    expect(onBack).toHaveBeenCalledOnce()
  })

  it.each(['regular', 'compact'] as const)('%s 密度下呈现标题、辅助信息和可用的关闭操作', (density) => {
    const closeButton = createRef<HTMLButtonElement>()
    const onClose = vi.fn()
    render(
      <DrawerHeader
        ref={closeButton}
        density={density}
        title="任务轨迹"
        description="当前会话 · 2 组"
        closeLabel="关闭任务轨迹"
        onClose={onClose}
      />,
    )

    expect(screen.getByRole('heading', { name: '任务轨迹' })).toBeInTheDocument()
    expect(screen.getByText('当前会话 · 2 组')).toBeInTheDocument()
    expect(closeButton.current).toBe(screen.getByRole('button', { name: '关闭任务轨迹' }))
    fireEvent.click(closeButton.current!)
    expect(onClose).toHaveBeenCalledOnce()
  })

})

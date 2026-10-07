import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Drawer } from './Drawer'

const props = { id: 'files', title: '工作区', closeLabel: '关闭工作区', backLabel: '返回对话' }
describe('Drawer', () => {
  it('关闭后退出辅助技术，打开显示标题、描述、业务内容和调宽操作', () => {
    const close = vi.fn()
    const { rerender } = render(<Drawer {...props} open={false} onClose={close}>内容</Drawer>)
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument()
    rerender(<Drawer {...props} open description="项目 A" onClose={close} resizeHandle={<div role="separator" />}>内容</Drawer>)
    expect(screen.getByRole('complementary', { name: '工作区' })).not.toHaveAttribute('inert')
    expect(screen.getByText('项目 A')).toBeVisible()
    expect(screen.getByRole('separator')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '关闭工作区' }))
    expect(close).toHaveBeenCalledOnce()
  })
  it('全宽详情聚焦返回入口，Escape 关闭；已有弹窗优先处理 Escape', () => {
    const close = vi.fn()
    const { rerender } = render(<Drawer {...props} open fullPage onClose={close} resizeHandle={<div role="separator" />}>内容</Drawer>)
    expect(screen.getByRole('button', { name: '返回对话' })).toHaveFocus()
    expect(screen.queryByRole('separator')).not.toBeInTheDocument()
    rerender(<><Drawer {...props} open fullPage onClose={close}>内容</Drawer><div role="dialog" aria-modal="true" /></>)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(close).not.toHaveBeenCalled()
    rerender(<Drawer {...props} open fullPage onClose={close}>内容</Drawer>)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(close).toHaveBeenCalledOnce()
  })
  it('打开侧栏聚焦关闭入口，内容更新保持当前键盘焦点', () => {
    const { rerender } = render(<Drawer {...props} open onClose={vi.fn()}><button type="button">文件</button></Drawer>)
    expect(screen.getByRole('button', { name: '关闭工作区' })).toHaveFocus()
    screen.getByRole('button', { name: '文件' }).focus()
    rerender(<Drawer {...props} open description="已刷新" onClose={vi.fn()}><button type="button">文件</button></Drawer>)
    expect(screen.getByRole('button', { name: '文件' })).toHaveFocus()
  })
})

import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { MarkdownContent } from '../components/MarkdownContent'
import { DiagramError, renderDiagram } from './renderDiagram'
import type { RenderedDiagram } from './renderDiagram'
import { Dialog } from '../../../components/ui'

vi.mock('./renderDiagram', async importOriginal => ({
  ...await importOriginal<typeof import('./renderDiagram')>(), renderDiagram: vi.fn(),
}))
const image: RenderedDiagram = { url: 'data:image/svg+xml,test', width: 300, height: 100, description: '测试流程' }
const source = 'flowchart LR\n  A[输入] --> B[输出]'
const markdown = (value = source) => `\`\`\`mermaid\n${value}\n\`\`\``
beforeEach(() => {
  vi.mocked(renderDiagram).mockReset().mockResolvedValue(image)
})

it('默认成图，键盘切换源码并复制原始内容', async () => {
  const user = userEvent.setup()
  const write = vi.spyOn(navigator.clipboard, 'writeText')
  render(<MarkdownContent content={markdown()} />)
  expect(await screen.findByRole('img', { name: '测试流程' })).toBeVisible()
  screen.getByRole('tab', { name: '图表' }).focus()
  await user.keyboard('{ArrowRight}')
  expect(screen.getByRole('tab', { name: '源码' })).toHaveFocus()
  expect(screen.getByRole('tabpanel', { name: '源码' }).textContent).toBe(source)
  await user.click(screen.getByRole('button', { name: '复制源码' }))
  expect(write).toHaveBeenCalledWith(source)
})

it('流式结束后才成图，更新状态不重建源码页签', async () => {
  const user = userEvent.setup()
  const { rerender } = render(<MarkdownContent content={markdown('flowchart LR\nA[')} isStreaming />)
  expect(screen.getByRole('status')).toHaveTextContent('图表生成中')
  expect(renderDiagram).not.toHaveBeenCalled()
  await user.click(screen.getByRole('tab', { name: '源码' }))
  rerender(<MarkdownContent content={markdown()} />)
  await waitFor(() => expect(renderDiagram).toHaveBeenCalledTimes(1))
  expect(screen.getByRole('tab', { name: '源码' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('tabpanel', { name: '源码' }).textContent).toBe(source)
})

it('语法错误保留源码且不影响相邻 Markdown', async () => {
  vi.mocked(renderDiagram).mockRejectedValue(new DiagramError('syntax'))
  const user = userEvent.setup()
  render(<MarkdownContent content={`${markdown()}\n\n正文仍可阅读`} />)
  await waitFor(() => expect(screen.queryByRole('tab', { name: '图表' })).toBeNull())
  expect(screen.getByText('正文仍可阅读')).toBeVisible()
  expect(screen.getByRole('figure', { name: 'Mermaid 图表' }).querySelector('code')?.textContent).toBe(source)
  await user.click(screen.getByRole('button', { name: '复制源码' }))
})

it('旧内容的迟到结果不能覆盖新图表', async () => {
  let finish!: (value: RenderedDiagram) => void
  vi.mocked(renderDiagram).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const { rerender } = render(<MarkdownContent content={markdown()} />)
  rerender(<MarkdownContent content={markdown('flowchart LR\nC-->D')} />)
  await screen.findByRole('img', { name: '测试流程' })
  await act(async () => finish({ ...image, description: '过期图表' }))
  expect(screen.queryByRole('img', { name: '过期图表' })).toBeNull()
})

it('资源加载失败可以重试', async () => {
  vi.mocked(renderDiagram).mockRejectedValueOnce(new DiagramError('load'))
  const user = userEvent.setup()
  render(<MarkdownContent content={markdown()} />)
  await user.click(await screen.findByRole('button', { name: '重试' }))
  expect(await screen.findByRole('img', { name: '测试流程' })).toBeVisible()
})

it('全屏缩放、返回及 Escape 恢复入口焦点', async () => {
  const user = userEvent.setup()
  render(<MarkdownContent content={markdown()} />)
  await screen.findByRole('img', { name: '测试流程' })
  const expand = screen.getByRole('button', { name: '放大图表' })
  await user.click(expand)
  const viewer = screen.getByRole('dialog', { name: '图表' })
  expect(within(viewer).getByRole('button', { name: '返回原内容' })).toHaveFocus()
  await user.click(within(viewer).getByRole('button', { name: '放大' }))
  expect(within(viewer).getByRole('button', { name: '重置为 100%' })).toHaveTextContent('125%')
  await user.click(within(viewer).getByRole('button', { name: '返回原内容' }))
  await waitFor(() => expect(expand).toHaveFocus())
  await user.click(expand)
  await user.keyboard('{Escape}')
  await waitFor(() => expect(expand).toHaveFocus())
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('已有弹窗复用一个焦点范围，返回后恢复原内容且不关闭宿主', async () => {
  const user = userEvent.setup()
  const close = vi.fn()
  render(<Dialog open title="文档预览" onClose={close}>
    <input aria-label="保留的输入" defaultValue="草稿" />
    <MarkdownContent content={markdown()} />
  </Dialog>)
  await screen.findByRole('img', { name: '测试流程' })
  const field = screen.getByRole('textbox', { name: '保留的输入' })
  const expand = screen.getByRole('button', { name: '放大图表' })
  await user.click(expand)
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
  expect(screen.getByRole('dialog')).toHaveAccessibleName('图表')
  expect(screen.queryByRole('textbox')).toBeNull()
  await user.keyboard('{Escape}')
  await waitFor(() => expect(expand).toHaveFocus())
  expect(screen.getByRole('dialog')).toHaveAccessibleName('文档预览')
  expect(screen.getByRole('textbox')).toBe(field)
  expect(field).toHaveValue('草稿')
  expect(close).not.toHaveBeenCalled()
})

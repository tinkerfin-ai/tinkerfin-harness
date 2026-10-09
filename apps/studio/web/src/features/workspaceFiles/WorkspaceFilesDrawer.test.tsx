import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { WorkspaceFilesDrawer } from './WorkspaceFilesDrawer'
import type { WorkspaceFilesState } from './useWorkspaceFiles'
import type { WorkspaceFile } from './api'

const nativePopover = new Map<string, PropertyDescriptor | undefined>()
beforeEach(() => {
  for (const [method, next] of [['showPopover', 'open'], ['hidePopover', 'closed']] as const) {
    nativePopover.set(method, Object.getOwnPropertyDescriptor(HTMLElement.prototype, method))
    Object.defineProperty(HTMLElement.prototype, method, { configurable: true, value(this: HTMLElement) {
      const event = new Event('toggle')
      Object.defineProperty(event, 'newState', { value: next })
      this.dispatchEvent(event)
    } })
  }
})
afterEach(() => {
  for (const [method, descriptor] of nativePopover) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, method, descriptor)
    else Reflect.deleteProperty(HTMLElement.prototype, method)
  }
  nativePopover.clear()
})

const file = (path: string, kind: WorkspaceFile['kind'] = 'file'): WorkspaceFile => ({ path, name: path.split('/').at(-1)!, kind, sizeBytes: 8, modifiedAt: '2030-01-01T00:00:00Z', etag: 'one' })
const state = (): WorkspaceFilesState => ({
  projectId: 'project', availability: 'ready', connection: 'ready', directoryPath: '/', selected: null,
  preview: null, previewPhase: 'idle', updated: false,
  directories: { '/': { phase: 'ready', entries: [file('/scripts', 'directory'), file('/page.html')], pages: 1, nextCursor: null } },
  openDirectory: vi.fn(), closeFile: vi.fn(), selectFile: vi.fn(), retryDirectory: vi.fn(), loadMore: vi.fn(), refreshPreview: vi.fn(), refresh: vi.fn(),
})
const props = { open: true, fullPage: false, onClose: vi.fn(), onToast: vi.fn() }

it('默认列表可切换图标，目录与文件仍使用相同可访问操作', () => {
  const files = state()
  render(<WorkspaceFilesDrawer {...props} state={files} />)
  expect(screen.queryByRole('navigation', { name: '文件路径' })).toBeNull()
  expect(screen.getAllByText('工作区')).toHaveLength(1)
  const list = screen.getByRole('button', { name: '列表视图' })
  const grid = screen.getByRole('button', { name: '图标视图' })
  expect(list).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(grid)
  expect(grid).toHaveAttribute('aria-pressed', 'true')
  expect(list).toHaveAttribute('aria-pressed', 'false')
  fireEvent.click(screen.getByRole('button', { name: 'scripts' }))
  expect(files.openDirectory).toHaveBeenCalledWith('/scripts')
  fireEvent.click(screen.getByRole('button', { name: 'page.html' }))
  expect(files.selectFile).toHaveBeenCalledWith(file('/page.html'))
  expect(screen.queryByText('只读')).toBeNull()
  expect(screen.queryByText('自动更新')).toBeNull()
})

it('内容区复制预览正文，不包含行号或路径；截断时明确复制当前预览', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  try {
    const files = { ...state(), selected: file('/page.html'), previewPhase: 'ready' as const,
      preview: { kind: 'text' as const, file: file('/page.html'), text: '<h1>source</h1>\n', truncated: true } }
    render(<WorkspaceFilesDrawer {...props} state={files} />)
    const button = screen.getByRole('button', { name: '复制当前预览' })
    expect(screen.getByRole('region', { name: '源码预览' })).toContainElement(button)
    await act(async () => fireEvent.click(button))
    expect(writeText).toHaveBeenCalledWith(files.preview.text)
    fireEvent.click(screen.getByRole('button', { name: '文件信息' }))
    expect(screen.getByRole('complementary', { name: '文件信息' })).toHaveTextContent('/page.html')
    expect(screen.getByRole('complementary', { name: '文件信息' })).toHaveTextContent('HTML')
  } finally {
    if (descriptor) Object.defineProperty(navigator, 'clipboard', descriptor)
    else Reflect.deleteProperty(navigator, 'clipboard')
  }
})

it.each(['unmount', 'close', 'select'] as const)('复制在 %s 后完成时不再发送旧文件的提示', async action => {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  let complete!: () => void
  const written = new Promise<void>(resolve => { complete = resolve })
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn(() => written) } })
  try {
    const onToast = vi.fn()
    const files = { ...state(), selected: file('/page.html'), previewPhase: 'ready' as const, preview: { kind: 'text' as const, file: file('/page.html'), text: '<h1>sample</h1>', truncated: false } }
    const view = render(<WorkspaceFilesDrawer {...props} state={files} onToast={onToast} />)
    fireEvent.click(screen.getByRole('button', { name: '复制内容' }))
    if (action === 'unmount') view.unmount()
    else view.rerender(<WorkspaceFilesDrawer {...props} state={action === 'select' ? { ...files, selected: file('/other.py') } : files} open={action !== 'close'} onToast={onToast} />)
    await act(async () => { complete(); await written })
    expect(onToast).not.toHaveBeenCalled()
  } finally {
    if (descriptor) Object.defineProperty(navigator, 'clipboard', descriptor)
    else Reflect.deleteProperty(navigator, 'clipboard')
  }
})

it('已有目录刷新期间分页显示忙碌且禁用，刷新完成后可加载下一页', () => {
  const files = state()
  files.directories['/'].nextCursor = 'next-page'
  files.directories['/'].phase = 'loading'
  const { rerender } = render(<WorkspaceFilesDrawer {...props} state={files} />)
  const more = screen.getByRole('button', { name: '加载更多文件' })
  expect(more).toBeDisabled()
  expect(more).toHaveAttribute('aria-busy', 'true')
    fireEvent.click(more)
  expect(files.loadMore).not.toHaveBeenCalled()
  files.directories['/'].phase = 'ready'
  rerender(<WorkspaceFilesDrawer {...props} state={files} />)
  expect(more).toBeEnabled()
  fireEvent.click(more)
  expect(files.loadMore).toHaveBeenCalledWith('/')
})

it('列表通过方向键选择，进入目录与打开文件使用公开动作', () => {
  const scroll = vi.fn()
  const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll })
  try {
    const files = state()
    render(<WorkspaceFilesDrawer {...props} state={files} />)
    const folder = screen.getByRole('button', { name: 'scripts' })
    const source = screen.getByRole('button', { name: 'page.html' })
    folder.focus()
    fireEvent.click(folder)
    expect(files.openDirectory).toHaveBeenCalledWith('/scripts')
    fireEvent.keyDown(folder, { key: 'ArrowDown' })
    expect(source).toHaveFocus()
    fireEvent.click(source)
    expect(files.selectFile).toHaveBeenCalledWith(file('/page.html'))
    fireEvent.keyDown(source, { key: 'Home' })
    expect(folder).toHaveFocus()
  } finally {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', descriptor)
    else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView')
  }
})

it('HTML 以源码显示，更新提示由用户刷新，不创建文档阅读器', () => {
  const files = { ...state(), selected: file('/page.html'), previewPhase: 'ready' as const, updated: true,
    preview: { kind: 'text' as const, file: file('/page.html'), text: '<iframe src="https://example.com"></iframe>', truncated: true } }
  const { container } = render(<WorkspaceFilesDrawer {...props} state={files} />)
  expect(screen.getByText(files.preview.text)).toBeInTheDocument()
  expect(container.querySelector('iframe')).toBeNull()
  expect(screen.getByText('仅预览前 200 行或 100 KiB')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '刷新预览' }))
  expect(files.refreshPreview).toHaveBeenCalledOnce()
})

it('空项目、错误和非文本文件均有准确反馈，错误可以重试', () => {
  const files = state()
  const { rerender } = render(<WorkspaceFilesDrawer {...props} state={{ ...files, availability: 'uninitialized' }} />)
  expect(screen.getByText('工作区尚无文件')).toBeInTheDocument()
  rerender(<WorkspaceFilesDrawer {...props} state={{ ...files, availability: 'unavailable' }} />)
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(files.refresh).toHaveBeenCalledOnce()
  rerender(<WorkspaceFilesDrawer {...props} state={{ ...files, selected: file('/report.pdf'), previewPhase: 'ready', preview: { kind: 'unsupported', file: file('/report.pdf') } }} />)
  expect(screen.getByText('此格式仅显示文件信息')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '文件信息' }))
  expect(screen.getByRole('complementary', { name: '文件信息' })).toHaveTextContent('8 B')
  rerender(<WorkspaceFilesDrawer {...props} state={{ ...files, selected: file('/report.pdf'), previewPhase: 'missing',
    directories: { '/': { phase: 'ready', entries: [], pages: 1, nextCursor: null } } }} />)
  expect(screen.getByText('文件已被移除')).toBeInTheDocument()
  expect(screen.queryByText('工作区尚无文件')).not.toBeInTheDocument()
})

import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { WorkspaceFilesDrawer } from './WorkspaceFilesDrawer'
import type { WorkspaceFilesState } from './useWorkspaceFiles'
import type { WorkspaceFile } from './api'

const file = (path: string, kind: WorkspaceFile['kind'] = 'file'): WorkspaceFile => ({ path, name: path.split('/').at(-1)!, kind, sizeBytes: 8, modifiedAt: '2030-01-01T00:00:00Z', etag: 'one' })
const state = (): WorkspaceFilesState => ({
  projectId: 'project', availability: 'ready', connection: 'ready', expanded: new Set(['/']), selected: null,
  preview: null, previewPhase: 'idle', updated: false,
  directories: { '/': { phase: 'ready', entries: [file('/scripts', 'directory'), file('/page.html')], pages: 1, nextCursor: null } },
  toggleDirectory: vi.fn(), selectFile: vi.fn(), retryDirectory: vi.fn(), loadMore: vi.fn(), refreshPreview: vi.fn(), refresh: vi.fn(),
})
const props = { projectName: '项目 A', open: true, fullPage: false, onClose: vi.fn(), onToast: vi.fn() }

it('已有目录刷新期间分页显示忙碌且禁用，刷新完成后可加载下一页', () => {
  const files = state()
  files.directories['/'].nextCursor = 'next-page'
  files.directories['/'].phase = 'loading'
  const { rerender } = render(<WorkspaceFilesDrawer {...props} state={files} />)
  const more = screen.getByRole('treeitem', { name: '加载更多文件' })
  expect(more).toBeDisabled()
  expect(more).toHaveAttribute('aria-busy', 'true')
  expect(more).toHaveAttribute('tabindex', '-1')
  fireEvent.click(more)
  expect(files.loadMore).not.toHaveBeenCalled()
  files.directories['/'].phase = 'ready'
  rerender(<WorkspaceFilesDrawer {...props} state={files} />)
  expect(more).toBeEnabled()
  fireEvent.click(more)
  expect(files.loadMore).toHaveBeenCalledWith('/')
})

it('树通过方向键选择并展开目录，选中文件交付源码预览', () => {
  const scroll = vi.fn()
  const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll })
  try {
    const files = state()
    render(<WorkspaceFilesDrawer {...props} state={files} />)
    const folder = screen.getByRole('treeitem', { name: 'scripts' })
    const source = screen.getByRole('treeitem', { name: 'page.html' })
    folder.focus()
    fireEvent.keyDown(folder, { key: 'ArrowRight' })
    expect(files.toggleDirectory).toHaveBeenCalledWith('/scripts')
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
  expect(screen.getByText('8 字节')).toBeInTheDocument()
  rerender(<WorkspaceFilesDrawer {...props} state={{ ...files, selected: file('/report.pdf'), previewPhase: 'missing',
    directories: { '/': { phase: 'ready', entries: [], pages: 1, nextCursor: null } } }} />)
  expect(screen.getByText('文件已被移除')).toBeInTheDocument()
  expect(screen.queryByText('工作区尚无文件')).not.toBeInTheDocument()
})

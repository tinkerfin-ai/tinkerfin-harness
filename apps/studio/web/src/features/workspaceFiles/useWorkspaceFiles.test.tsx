import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { saveAuthSession, clearAuthSession } from '../../auth/session'
import { getServerAddress } from '../../api/shared/config'
import { ApiError } from '../../api/shared/http'
import { startResourceFeed } from '../../api/shared/resourceFeed'
import { readWorkspaceDirectory, readWorkspaceFileInfo, readWorkspacePreview, WORKSPACE_ERRORS, type WorkspaceDirectory, type WorkspaceFile } from './api'
import { useWorkspaceFiles } from './useWorkspaceFiles'

vi.mock('../../api/shared/resourceFeed', () => ({ startResourceFeed: vi.fn() }))
vi.mock('./api', async importOriginal => ({ ...await importOriginal<typeof import('./api')>(),
  readWorkspaceDirectory: vi.fn(), readWorkspaceFileInfo: vi.fn(), readWorkspacePreview: vi.fn(),
}))

const file = (path: string, kind: WorkspaceFile['kind'] = 'file', etag = 'first'): WorkspaceFile => ({
  path, name: path.split('/').at(-1)!, kind, etag, modifiedAt: '2030-01-01T00:00:00Z', sizeBytes: kind === 'file' ? 8 : null,
})
const page = (path: string, entries: WorkspaceFile[] = [], nextCursor: string | null = null): WorkspaceDirectory => ({ state: 'ready', path, entries, nextCursor })
const feed = () => vi.mocked(startResourceFeed).mock.calls.at(-1)![0]
const ready = () => act(() => { feed().onState?.('ready'); feed().onFrame({ id: null, event: 'ready', data: {} }) })
const change = () => act(() => feed().onFrame({ id: null, event: 'change', data: { kind: 'files_changed' } }))
const settle = () => act(() => vi.advanceTimersByTimeAsync(0))
const closeFeed = vi.fn()
function signIn(token = 'one') {
  saveAuthSession({ serverAddress: getServerAddress(), token, tokenType: 'Bearer', expiresAt: '2100-01-01T00:00:00Z',
    user: { user_id: token === 'one' ? 1 : 2, username: token, display_name: token, roles: [], disabled: false, avatar_url: null } })
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(reply => { resolve = reply })
  return { promise, resolve }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2030-01-01T00:00:00Z'))
  vi.clearAllMocks()
  signIn()
  vi.mocked(startResourceFeed).mockReturnValue(closeFeed)
  vi.mocked(readWorkspaceDirectory).mockImplementation(async (_project, path) => page(path, path === '/' ? [file('/a.py')] : []))
  vi.mocked(readWorkspacePreview).mockImplementation(async (_project, path) => ({ kind: 'text', file: file(path), text: 'original', truncated: false }))
  vi.mocked(readWorkspaceFileInfo).mockImplementation(async (_project, path) => file(path))
})
afterEach(() => { cleanup(); clearAuthSession(); vi.restoreAllMocks(); vi.useRealTimers() })

it('订阅就绪后读取基线，折叠关闭保留文件和阅读内容，重新打开先建立订阅', async () => {
  const { result, rerender } = renderHook(({ open }) => useWorkspaceFiles('project', open), { initialProps: { open: true } })
  expect(readWorkspaceDirectory).not.toHaveBeenCalled()
  ready(); await settle()
  act(() => result.current.selectFile(file('/a.py'))); await settle()
  rerender({ open: false })
  expect(closeFeed).toHaveBeenCalledOnce()
  expect(result.current.preview).toMatchObject({ text: 'original' })
  vi.mocked(readWorkspaceDirectory).mockClear()
  rerender({ open: true })
  expect(readWorkspaceDirectory).not.toHaveBeenCalled()
  ready(); await settle()
  expect(readWorkspaceDirectory).toHaveBeenCalledOnce()
})

it('项目和登录身份变化清空旧状态并取消查询，迟到结果不能覆盖新项目', async () => {
  const pending = deferred<WorkspaceDirectory>()
  vi.mocked(readWorkspaceDirectory).mockReturnValueOnce(pending.promise)
  const { result, rerender } = renderHook(({ project }) => useWorkspaceFiles(project, true), { initialProps: { project: 'old' } })
  ready()
  const oldSignal = vi.mocked(readWorkspaceDirectory).mock.calls[0][3]
  rerender({ project: 'new' })
  expect(oldSignal.aborted).toBe(true)
  ready(); await settle()
  pending.resolve(page('/', [file('/old-secret.txt')]))
  await settle()
  expect(result.current.directories['/'].entries).toEqual([file('/a.py')])
  act(() => signIn('two'))
  expect(result.current.directories).toEqual({})
  expect(startResourceFeed).toHaveBeenCalledTimes(3)
  ready(); await settle()
  expect(result.current.availability).toBe('ready')
})

it('合并变化提示，只在选中文件改变时提示，刷新预览由用户触发', async () => {
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  act(() => result.current.selectFile(file('/a.py'))); await settle()
  change(); change(); change()
  await act(() => vi.advanceTimersByTimeAsync(150))
  expect(result.current.updated).toBe(false)
  expect(readWorkspacePreview).toHaveBeenCalledOnce()
  expect(readWorkspaceDirectory).toHaveBeenCalledTimes(2)
  vi.mocked(readWorkspaceFileInfo).mockResolvedValue(file('/a.py', 'file', 'changed'))
  change(); await act(() => vi.advanceTimersByTimeAsync(150))
  expect(result.current.updated).toBe(true)
  expect(result.current.preview).toMatchObject({ text: 'original' })
  vi.mocked(readWorkspacePreview).mockResolvedValue({ kind: 'text', file: file('/a.py', 'file', 'changed'), text: 'updated', truncated: false })
  act(() => result.current.refreshPreview()); await settle()
  expect(result.current.preview).toMatchObject({ text: 'updated' })
  expect(result.current.updated).toBe(false)
  vi.mocked(readWorkspaceFileInfo).mockRejectedValue(new ApiError('missing', { code: WORKSPACE_ERRORS.notFound }))
  change(); await act(() => vi.advanceTimersByTimeAsync(150))
  expect(result.current.previewPhase).toBe('missing')
  expect(result.current.preview).toBeNull()
})

it('隐藏页面与关闭抽屉会取消查询，恢复可见后重新校准，不交付取消后的预览', async () => {
  const pending = deferred<Awaited<ReturnType<typeof readWorkspacePreview>>>()
  vi.mocked(readWorkspacePreview).mockReturnValueOnce(pending.promise)
  const { result, rerender } = renderHook(({ open }) => useWorkspaceFiles('project', open), { initialProps: { open: true } })
  ready(); await settle()
  act(() => result.current.selectFile(file('/a.py')))
  const signal = vi.mocked(readWorkspacePreview).mock.calls[0][2]
  act(() => feed().onState?.('hidden'))
  expect(signal.aborted).toBe(true)
  pending.resolve({ kind: 'text', file: file('/a.py'), text: 'late', truncated: false }); await settle()
  expect(result.current.preview).toBeNull()
  ready(); await settle()
  expect(result.current.preview).toMatchObject({ text: 'original' })
  rerender({ open: false })
  const queries = vi.mocked(readWorkspaceDirectory).mock.calls.length
  await act(() => vi.advanceTimersByTimeAsync(60_000))
  expect(readWorkspaceDirectory).toHaveBeenCalledTimes(queries)
})

it('目录查询最多四个并发，收起目录取消后代查询并释放名额', async () => {
  const pending = deferred<WorkspaceDirectory>()
  vi.mocked(readWorkspaceDirectory).mockImplementation(async (_project, path) => path === '/' ? page('/', Array.from({ length: 5 }, (_, i) => file(`/d${i}`, 'directory'))) : pending.promise)
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  act(() => { for (let i = 0; i < 5; i++) result.current.toggleDirectory(`/d${i}`) })
  expect(readWorkspaceDirectory).toHaveBeenCalledTimes(5)
  const first = vi.mocked(readWorkspaceDirectory).mock.calls[1][3]
  act(() => result.current.toggleDirectory('/d0'))
  expect(first.aborted).toBe(true)
  expect(readWorkspaceDirectory).toHaveBeenCalledTimes(6)
  expect(result.current.expanded.has('/d0')).toBe(false)
})

it('分页游标失效重新读取目录，未初始化与暂停均不提供旧文件', async () => {
  vi.mocked(readWorkspaceDirectory).mockResolvedValueOnce(page('/', [file('/a.py')], 'cursor'))
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  vi.mocked(readWorkspaceDirectory).mockRejectedValueOnce(new ApiError('changed', { code: WORKSPACE_ERRORS.changed }))
  act(() => result.current.loadMore('/')); await settle()
  expect(readWorkspaceDirectory).toHaveBeenLastCalledWith('project', '/', null, expect.any(AbortSignal))
  expect(result.current.directories['/'].phase).toBe('ready')
  act(() => feed().onState?.('disconnected', new ApiError('paused', { code: WORKSPACE_ERRORS.paused })))
  expect(result.current.availability).toBe('paused')
  expect(result.current.directories).toEqual({})
  act(() => feed().onState?.('disconnected', new ApiError('empty', { code: WORKSPACE_ERRORS.uninitialized })))
  expect(result.current.availability).toBe('uninitialized')
})

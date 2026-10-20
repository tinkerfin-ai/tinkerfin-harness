import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getServerAddress } from '../../api/shared/config'
import { ApiError } from '../../api/shared/http'
import { startResourceFeed } from '../../api/shared/resourceFeed'
import { clearAuthSession, saveAuthSession } from '../../auth/session'
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
    user: { user_id: token === 'one' ? 1 : 2, username: token, roles: [], disabled: false, avatar_url: null } })
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((reply, fail) => { resolve = reply; reject = fail })
  return { promise, resolve, reject }
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

it('进入新目录取消旧查询，迟到目录与预览不能覆盖当前目录', async () => {
  const pending = deferred<WorkspaceDirectory>()
  const pendingPreview = deferred<Awaited<ReturnType<typeof readWorkspacePreview>>>()
  vi.mocked(readWorkspaceDirectory).mockImplementation(async (_project, path) => path === '/scripts' ? pending.promise : page(path, [file(`${path === '/' ? '' : path}/a.py`)]))
  vi.mocked(readWorkspacePreview).mockReturnValueOnce(pendingPreview.promise)
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  act(() => result.current.selectFile(file('/a.py')))
  const previewSignal = vi.mocked(readWorkspacePreview).mock.calls[0][2]
  act(() => result.current.openDirectory('/scripts'))
  const directorySignal = vi.mocked(readWorkspaceDirectory).mock.calls.at(-1)![3]
  expect(previewSignal.aborted).toBe(true)
  act(() => result.current.openDirectory('/skills')); await settle()
  expect(directorySignal.aborted).toBe(true)
  pending.resolve(page('/scripts', [file('/scripts/old.py')]))
  pendingPreview.resolve({ kind: 'text', file: file('/a.py'), text: 'late', truncated: false })
  await settle()
  expect(result.current.directoryPath).toBe('/skills')
  expect(result.current.directories['/skills'].entries).toEqual([file('/skills/a.py')])
  expect(result.current.selected).toBeNull()
  expect(result.current.preview).toBeNull()
  change(); await act(() => vi.advanceTimersByTimeAsync(150))
  expect(readWorkspaceDirectory).toHaveBeenLastCalledWith('project', '/skills', null, expect.any(AbortSignal))
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


it('就绪后空闲不重复查询目录或文件，变化合并读取，重连和手动刷新可恢复', async () => {
  const { result, rerender } = renderHook(({ enabled }) => useWorkspaceFiles('project', enabled), { initialProps: { enabled: true } })
  expect(readWorkspaceDirectory).not.toHaveBeenCalled()
  ready(); await settle()
  act(() => result.current.selectFile(file('/a.py'))); await settle()
  await act(() => vi.advanceTimersByTimeAsync(90_000))
  expect(readWorkspaceDirectory).toHaveBeenCalledOnce()
  expect(readWorkspacePreview).toHaveBeenCalledOnce()
  expect(readWorkspaceFileInfo).not.toHaveBeenCalled()
  change(); change(); await act(() => vi.advanceTimersByTimeAsync(150))
  expect(readWorkspaceDirectory).toHaveBeenCalledTimes(2)
  expect(readWorkspaceFileInfo).toHaveBeenCalledOnce()
  act(() => feed().onFrame({ id: null, event: 'resync', data: {} })); await settle()
  expect(readWorkspaceDirectory).toHaveBeenCalledTimes(3)
  act(() => result.current.refresh()); ready(); await settle()
  expect(readWorkspaceDirectory).toHaveBeenCalledTimes(4)
  rerender({ enabled: false })
  await act(() => vi.advanceTimersByTimeAsync(90_000))
  expect(readWorkspaceDirectory).toHaveBeenCalledTimes(4)
  expect(closeFeed).toHaveBeenCalledTimes(2)
})

it('手动刷新立即显示进行中并拒绝重复点击，保留文件直至目录和选中文件检查完成', async () => {
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  act(() => result.current.selectFile(file('/a.py'))); await settle()
  const directory = deferred<WorkspaceDirectory>()
  const info = deferred<WorkspaceFile>()
  vi.mocked(readWorkspaceDirectory).mockReturnValueOnce(directory.promise)
  vi.mocked(readWorkspaceFileInfo).mockReturnValueOnce(info.promise)
  act(() => { result.current.refresh(); result.current.refresh() })
  expect(result.current.refreshing).toBe(true)
  expect(startResourceFeed).toHaveBeenCalledTimes(2)
  expect(result.current.directories['/'].entries).toEqual([file('/a.py')])
  expect(result.current.preview).toMatchObject({ text: 'original' })
  ready()
  directory.resolve(page('/', [file('/a.py', 'file', 'new')]))
  await settle()
  expect(result.current.refreshing).toBe(true)
  info.resolve(file('/a.py', 'file', 'new'))
  await settle()
  expect(result.current.refreshing).toBe(false)
  expect(result.current.updated).toBe(true)
  expect(result.current.preview).toMatchObject({ text: 'original' })
  expect(readWorkspacePreview).toHaveBeenCalledOnce()
})

it('手动刷新失败后解除进行中状态并允许再次刷新', async () => {
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  const directory = deferred<WorkspaceDirectory>()
  vi.mocked(readWorkspaceDirectory).mockReturnValueOnce(directory.promise)
  act(() => result.current.refresh()); ready()
  expect(result.current.refreshing).toBe(true)
  directory.reject(new Error('offline'))
  await settle()
  expect(result.current.refreshing).toBe(false)
  expect(result.current.directories['/'].entries).toEqual([file('/a.py')])
  expect(result.current.directories['/'].phase).toBe('error')
  act(() => result.current.refresh())
  expect(result.current.refreshing).toBe(true)
  ready(); await settle()
  expect(result.current.refreshing).toBe(false)
  expect(result.current.directories['/'].phase).toBe('ready')
})

it.each(['hidden', 'disconnected'] as const)('刷新时通知源%s会取消请求并结束进行中状态', async connection => {
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  const directory = deferred<WorkspaceDirectory>()
  vi.mocked(readWorkspaceDirectory).mockReturnValueOnce(directory.promise)
  act(() => result.current.refresh()); ready()
  const signal = vi.mocked(readWorkspaceDirectory).mock.calls.at(-1)![3]
  act(() => feed().onState?.(connection))
  expect(signal.aborted).toBe(true)
  expect(result.current.refreshing).toBe(false)
  directory.resolve(page('/', [file('/late.txt')]))
  await settle()
  expect(result.current.directories['/'].entries).toEqual([file('/a.py')])
})

it.each(['disabled', 'project', 'account', 'directory'] as const)('刷新时切换%s不会残留进行中状态或接受旧结果', async destination => {
  const { result, rerender } = renderHook(({ project, enabled }) => useWorkspaceFiles(project, enabled),
    { initialProps: { project: 'project', enabled: true } })
  ready(); await settle()
  const directory = deferred<WorkspaceDirectory>()
  vi.mocked(readWorkspaceDirectory).mockReturnValueOnce(directory.promise)
  act(() => result.current.refresh()); ready()
  const signal = vi.mocked(readWorkspaceDirectory).mock.calls.at(-1)![3]
  if (destination === 'disabled') rerender({ project: 'project', enabled: false })
  if (destination === 'project') rerender({ project: 'next', enabled: true })
  if (destination === 'account') act(() => signIn('two'))
  if (destination === 'directory') act(() => result.current.openDirectory('/next'))
  expect(signal.aborted).toBe(true)
  expect(result.current.refreshing).toBe(false)
  directory.resolve(page('/', [file('/late.txt')]))
  await settle()
  expect(result.current.directories['/']?.entries ?? []).not.toContainEqual(file('/late.txt'))
  if (destination === 'disabled') {
    act(() => result.current.refresh())
    expect(result.current.refreshing).toBe(false)
    rerender({ project: 'project', enabled: true })
    ready(); await settle()
    expect(result.current.refreshing).toBe(false)
  }
})

it('关闭选中文件后不再等待其已取消的刷新检查', async () => {
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  act(() => result.current.selectFile(file('/a.py'))); await settle()
  const info = deferred<WorkspaceFile>()
  vi.mocked(readWorkspaceFileInfo).mockReturnValueOnce(info.promise)
  act(() => result.current.refresh()); ready(); await settle()
  expect(result.current.refreshing).toBe(true)
  const signal = vi.mocked(readWorkspaceFileInfo).mock.calls.at(-1)![2]
  act(() => result.current.closeFile())
  expect(signal.aborted).toBe(true)
  expect(result.current.refreshing).toBe(false)
  info.resolve(file('/a.py', 'file', 'late')); await settle()
  expect(result.current.selected).toBeNull()
})

it('刷新会等待重新加载的预览，预览失败后释放进行中状态', async () => {
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  const oldPreview = deferred<Awaited<ReturnType<typeof readWorkspacePreview>>>()
  const nextPreview = deferred<Awaited<ReturnType<typeof readWorkspacePreview>>>()
  vi.mocked(readWorkspacePreview).mockReturnValueOnce(oldPreview.promise).mockReturnValueOnce(nextPreview.promise)
  act(() => result.current.selectFile(file('/a.py')))
  const oldSignal = vi.mocked(readWorkspacePreview).mock.calls.at(-1)![2]
  act(() => result.current.refresh()); ready(); await settle()
  expect(oldSignal.aborted).toBe(true)
  expect(result.current.refreshing).toBe(true)
  oldPreview.resolve({ kind: 'text', file: file('/a.py'), text: 'late', truncated: false })
  await settle()
  expect(result.current.refreshing).toBe(true)
  nextPreview.reject(new Error('unavailable')); await settle()
  expect(result.current.refreshing).toBe(false)
  expect(result.current.previewPhase).toBe('error')
  expect(result.current.preview).toBeNull()
})

it('刷新选中文件检查失败后仍结束进行中状态', async () => {
  const { result } = renderHook(() => useWorkspaceFiles('project', true))
  ready(); await settle()
  act(() => result.current.selectFile(file('/a.py'))); await settle()
  const info = deferred<WorkspaceFile>()
  vi.mocked(readWorkspaceFileInfo).mockReturnValueOnce(info.promise)
  act(() => result.current.refresh()); ready(); await settle()
  expect(result.current.refreshing).toBe(true)
  info.reject(new ApiError('missing', { code: WORKSPACE_ERRORS.notFound })); await settle()
  expect(result.current.refreshing).toBe(false)
  expect(result.current.previewPhase).toBe('missing')
})

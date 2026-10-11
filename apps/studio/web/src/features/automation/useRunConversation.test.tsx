import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { fetchRunStatus } from './api'
import { useRunConversation } from './useRunConversation'
import { runFixture } from '../../test/automationFixtures'
import type { AutomationRunRecord } from './model'

vi.mock('./api', () => ({ fetchRunStatus: vi.fn() }))
beforeEach(() => { vi.resetAllMocks(); window.history.replaceState({}, '', '/?page=automation') })
afterEach(cleanup)
const target = { state: 'available', projectId: 'destination', threadId: 'conversation' } as const

it('读取会话当前位置后准备目标草稿并导航，不使用详情中的旧位置', async () => {
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ conversation: target }))
  const draft = vi.fn(() => true), open = vi.fn()
  const { result } = renderHook(() => useRunConversation(open, vi.fn()))
  let opened = false
  await act(async () => { opened = await result.current(runFixture(), { prepareDraft: draft }) })
  expect(opened).toBe(true)
  expect(fetchRunStatus).toHaveBeenCalledWith('project-1', 'run', expect.any(AbortSignal))
  expect(draft).toHaveBeenCalledWith(target)
  expect(open).toHaveBeenCalledWith(target)
})

it.each(['deleted', 'not_created', 'pending'] as const)('会话 %s 显示准确全局提示且不改变草稿或导航', async state => {
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ conversation: { state } }))
  const open = vi.fn(), toast = vi.fn(), draft = vi.fn(() => true)
  const { result } = renderHook(() => useRunConversation(open, toast))
  await act(async () => { expect(await result.current(runFixture(), { prepareDraft: draft })).toBe(false) })
  expect(toast).toHaveBeenCalledExactlyOnceWith('error', state === 'deleted' ? '关联会话已删除' : state === 'pending' ? '关联会话尚未开放' : '关联会话尚未创建')
  expect(open).not.toHaveBeenCalled(); expect(draft).not.toHaveBeenCalled()
})

it('读取失败可重试，草稿拒绝添加时保留原页面', async () => {
  vi.mocked(fetchRunStatus).mockRejectedValueOnce(new Error('offline')).mockResolvedValue(runFixture({ conversation: target }))
  const open = vi.fn(), toast = vi.fn()
  const { result } = renderHook(() => useRunConversation(open, toast))
  await act(async () => { expect(await result.current(runFixture())).toBe(false) })
  expect(toast).toHaveBeenCalledExactlyOnceWith('error', '关联会话加载失败，请重试')
  await act(async () => { expect(await result.current(runFixture(), { prepareDraft: () => false })).toBe(false) })
  expect(open).not.toHaveBeenCalled()
  await act(async () => { expect(await result.current(runFixture())).toBe(true) })
  expect(open).toHaveBeenCalledExactlyOnceWith(target)
})

it.each(['close', 'navigate', 'unmount', 'supersede'] as const)('%s 后的迟到结果不能写入草稿、导航或提示', async mode => {
  let resolve!: (run: AutomationRunRecord) => void
  vi.mocked(fetchRunStatus).mockReturnValueOnce(new Promise(done => { resolve = done })).mockResolvedValue(runFixture({ conversation: target }))
  const open = vi.fn(), toast = vi.fn(), draft = vi.fn(() => true), controller = new AbortController()
  const { result, unmount } = renderHook(() => useRunConversation(open, toast))
  let completion!: Promise<boolean>
  act(() => { completion = result.current(runFixture(), { signal: controller.signal, prepareDraft: draft }) })
  if (mode === 'close') controller.abort()
  if (mode === 'navigate') window.history.pushState({}, '', '/?page=skills')
  if (mode === 'unmount') unmount()
  if (mode === 'supersede') await act(async () => { await result.current(runFixture({ id: 'other' })) })
  await act(async () => { resolve(runFixture({ conversation: target })); expect(await completion).toBe(false) })
  expect(open).toHaveBeenCalledTimes(mode === 'supersede' ? 1 : 0)
  expect(draft).not.toHaveBeenCalled(); expect(toast).not.toHaveBeenCalled()
})

it('卸载后的通知回调不能重新创建请求', async () => {
  const { result, unmount } = renderHook(() => useRunConversation(vi.fn(), vi.fn()))
  const open = result.current
  unmount()
  expect(await open(runFixture())).toBe(false)
  expect(fetchRunStatus).not.toHaveBeenCalled()
})

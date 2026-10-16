import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { mockResourceNotices } from '../../test/resourceNotices'
import { listMemories, type MemoryPage } from './api'
import { useMemories } from './useMemories'

vi.mock('./api', () => ({ listMemories: vi.fn() }))
const page = (path: string): MemoryPage => ({ items: [{ path, etag: path, sizeBytes: 1, updatedAt: '2030-01-01T00:00:00Z', editable: true, preview: path }], nextOffset: 1 })
let notices: ReturnType<typeof mockResourceNotices>
beforeEach(() => { vi.useFakeTimers(); vi.resetAllMocks(); notices = mockResourceNotices(); vi.mocked(listMemories).mockResolvedValue(page('/current')) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })
const settle = () => act(() => vi.advanceTimersByTimeAsync(0))

it('搜索合并连续输入并取消旧请求，项目切换拒绝旧结果，空闲不轮询', async () => {
  let resolve!: (value: MemoryPage) => void
  vi.mocked(listMemories).mockReturnValueOnce(new Promise(reply => { resolve = reply }))
  const { result, rerender } = renderHook(({ project, query }) => useMemories(project, query), { initialProps: { project: 'one', query: '' } })
  const oldSignal = vi.mocked(listMemories).mock.calls[0][3]
  rerender({ project: 'one', query: 'a' }); rerender({ project: 'one', query: 'ab' })
  expect(oldSignal.aborted).toBe(true)
  await act(() => vi.advanceTimersByTimeAsync(250))
  expect(listMemories).toHaveBeenCalledTimes(2)
  expect(listMemories).toHaveBeenLastCalledWith('one', 'ab', 0, expect.any(AbortSignal))
  await act(async () => resolve(page('/private')))
  expect(result.current.items[0].path).toBe('/current')
  rerender({ project: 'one', query: 'pending' }); rerender({ project: 'two', query: '' }); await settle()
  await act(() => vi.advanceTimersByTimeAsync(90_000))
  expect(listMemories).toHaveBeenCalledTimes(3)
  act(() => notices.changed('studio.memories.changed', 'one')); await settle()
  expect(listMemories).toHaveBeenCalledTimes(3)
  act(() => notices.changed('studio.memories.changed', 'two')); await settle()
  expect(listMemories).toHaveBeenCalledTimes(4)
})

it('刷新取消未完成的追加页，迟到分页不能覆盖新基线', async () => {
  const { result } = renderHook(() => useMemories('project', ''))
  await settle()
  let resolve!: (value: MemoryPage) => void
  vi.mocked(listMemories).mockReturnValueOnce(new Promise(reply => { resolve = reply }))
  act(() => { void result.current.more(); void result.current.more() })
  expect(listMemories).toHaveBeenCalledTimes(2)
  const signal = vi.mocked(listMemories).mock.calls[1][3]
  act(() => result.current.refresh()); await settle()
  expect(signal.aborted).toBe(true)
  await act(async () => resolve(page('/old-page')))
  expect(result.current.items.map(item => item.path)).toEqual(['/current'])
})

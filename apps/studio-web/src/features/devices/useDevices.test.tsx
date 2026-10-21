import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from './api'
import type { LocalDevice } from './api'
import { useDevices } from './useDevices'

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

it('基线读取期间主动刷新等待新读取，重复调用合并且旧结果不宣告刷新完成', async () => {
  let baseline!: (devices: LocalDevice[]) => void
  let refreshed!: (devices: LocalDevice[]) => void
  const fetch = vi.spyOn(api, 'fetchDevices')
    .mockImplementationOnce(() => new Promise(resolve => { baseline = resolve }))
    .mockImplementationOnce(() => new Promise(resolve => { refreshed = resolve }))
  const { result } = renderHook(useDevices)
  act(() => { result.current.refresh(); result.current.refresh() })
  expect(fetch).toHaveBeenCalledOnce()
  await act(async () => { baseline([]) })
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(result.current.refreshing).toBe(true)
  expect(result.current.refreshed).toBe(false)
  await act(async () => { refreshed([]) })
  expect(result.current.refreshing).toBe(false)
  expect(result.current.refreshed).toBe(true)
})

it('隐藏页面取消刷新并忽略迟到结果，重新可见后可继续读取', async () => {
  let complete!: (devices: LocalDevice[]) => void
  const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  const fetch = vi.spyOn(api, 'fetchDevices').mockResolvedValueOnce([])
    .mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
    .mockResolvedValueOnce([])
  const { result } = renderHook(useDevices)
  await act(async () => {})
  act(() => result.current.refresh())
  const signal = fetch.mock.calls[1][0]!
  act(() => { hidden.mockReturnValue(true); document.dispatchEvent(new Event('visibilitychange')) })
  expect(signal.aborted).toBe(true)
  await act(async () => { complete([]) })
  expect(result.current.refreshing).toBe(false)
  expect(result.current.refreshed).toBe(false)
  act(() => { hidden.mockReturnValue(false); document.dispatchEvent(new Event('visibilitychange')) })
  await act(async () => {})
  expect(fetch).toHaveBeenCalledTimes(3)
  expect(result.current.status).toBe('ready')
  expect(result.current.refreshing).toBe(false)
})

it('卸载时取消当前查询且迟到结果不触发新请求', async () => {
  let complete!: (devices: LocalDevice[]) => void
  const fetch = vi.spyOn(api, 'fetchDevices').mockImplementation(() => new Promise(resolve => { complete = resolve }))
  const { unmount } = renderHook(useDevices)
  const signal = fetch.mock.calls[0][0]!
  unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => { complete([]) })
  expect(fetch).toHaveBeenCalledOnce()
  expect(vi.getTimerCount()).toBe(0)
})

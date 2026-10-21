import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as http from '../../api/shared/http'
import { fetchDevices } from './api'

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

it.each(['timeout', 'cancel'] as const)('设备查询在 %s 后释放等待并清理计时器', async ending => {
  vi.spyOn(http, 'requestJson').mockImplementation((_path, options) => new Promise((_resolve, reject) => {
    options!.signal!.addEventListener('abort', () => reject(options!.signal!.reason), { once: true })
  }))
  const controller = new AbortController()
  const pending = fetchDevices(controller.signal)
  const assertion = expect(pending).rejects.toMatchObject({ name: ending === 'timeout' ? 'TimeoutError' : 'AbortError' })
  if (ending === 'timeout') await vi.advanceTimersByTimeAsync(15_000)
  else controller.abort()
  await assertion
  expect(vi.getTimerCount()).toBe(0)
})

it('设备查询成功后立即释放超时计时器', async () => {
  vi.spyOn(http, 'requestJson').mockResolvedValue([])
  await expect(fetchDevices()).resolves.toEqual([])
  expect(vi.getTimerCount()).toBe(0)
})

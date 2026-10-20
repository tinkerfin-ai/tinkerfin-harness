import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ResourceNotice } from '../notifications'
import { watchResource } from './watchResource'

const source = vi.hoisted(() => ({
  state: 'inactive' as 'inactive' | 'connecting' | 'ready' | 'disconnected',
  listeners: new Set<(notice: ResourceNotice) => void>(),
}))
vi.mock('../notifications', () => ({
  getResourceConnectionState: () => source.state,
  subscribeResourceChanges: (listener: (notice: ResourceNotice) => void) => {
    source.listeners.add(listener)
    return () => source.listeners.delete(listener)
  },
}))
const emit = (notice: ResourceNotice) => source.listeners.forEach(listener => listener(notice))
const changed = (topic = 'selected') => emit({ kind: 'change', change: {
  scope: { namespace: 'ns_1', owner_id: null }, topic, key: 'resource', details: {},
} })
const watches: Array<ReturnType<typeof watchResource>> = []
function watch<T>(options: Parameters<typeof watchResource<T>>[0]) {
  const value = watchResource(options)
  watches.push(value)
  return value
}
beforeEach(() => { vi.useFakeTimers(); source.state = 'inactive'; source.listeners.clear() })
afterEach(() => { watches.splice(0).forEach(value => value.close()); vi.useRealTimers(); vi.restoreAllMocks() })

it('订阅就绪后只读一次基线，空闲与无关通知不产生请求', async () => {
  source.state = 'connecting'
  const read = vi.fn().mockResolvedValue('value'), update = vi.fn()
  watch({ read, update, matches: change => change.topic === 'selected' })
  expect(read).not.toHaveBeenCalled()
  source.state = 'ready'
  emit({ kind: 'resync' })
  await vi.advanceTimersByTimeAsync(0)
  expect(update).toHaveBeenCalledOnce()
  await vi.advanceTimersByTimeAsync(90_000)
  changed('unrelated')
  expect(read).toHaveBeenCalledOnce()
  changed()
  await vi.advanceTimersByTimeAsync(0)
  expect(read).toHaveBeenCalledTimes(2)
  emit({ kind: 'resync' })
  await vi.advanceTimersByTimeAsync(0)
  expect(read).toHaveBeenCalledTimes(3)
})

it('在途通知合并成一次追加读取，关闭后丢弃迟到响应', async () => {
  let complete!: (value: string) => void
  const read = vi.fn().mockImplementationOnce(() => new Promise<string>(resolve => { complete = resolve })).mockResolvedValue('latest')
  const update = vi.fn()
  const current = watch({ read, update, matches: () => true })
  changed(); changed(); changed()
  expect(read).toHaveBeenCalledOnce()
  complete('old')
  await vi.advanceTimersByTimeAsync(0)
  expect(read).toHaveBeenCalledTimes(2)
  expect(update.mock.calls.at(-1)?.[0]).toBe('latest')
  read.mockImplementationOnce(() => new Promise<string>(resolve => { complete = resolve }))
  current.refresh()
  const signal = read.mock.calls.at(-1)![0] as AbortSignal
  current.close()
  expect(signal.aborted).toBe(true)
  complete('late')
  await vi.advanceTimersByTimeAsync(90_000)
  expect(update).toHaveBeenCalledTimes(2)
  changed()
  expect(read).toHaveBeenCalledTimes(3)
})

it('只有未完成结果继续校准，确认完成后停止', async () => {
  const read = vi.fn(async () => ({ pending: false })).mockResolvedValueOnce({ pending: true })
  watch({ read, update: vi.fn(), matches: () => true, refreshWhile: value => value.pending })
  await vi.advanceTimersByTimeAsync(30_000)
  expect(read).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(90_000)
  expect(read).toHaveBeenCalledTimes(2)
})

it('读取失败交给显式重试，隐藏时取消并在恢复可见后读取', async () => {
  const read = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue('ok')
  const onError = vi.fn(), update = vi.fn()
  const current = watch({ read, update, onError, matches: () => true })
  await vi.advanceTimersByTimeAsync(90_000)
  expect(read).toHaveBeenCalledOnce()
  expect(onError).toHaveBeenCalledOnce()
  current.refresh()
  await vi.advanceTimersByTimeAsync(0)
  expect(update).toHaveBeenCalledOnce()
  const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  document.dispatchEvent(new Event('visibilitychange'))
  changed()
  await vi.advanceTimersByTimeAsync(90_000)
  expect(read).toHaveBeenCalledTimes(2)
  hidden.mockReturnValue(false)
  document.dispatchEvent(new Event('visibilitychange'))
  await vi.advanceTimersByTimeAsync(0)
  expect(read).toHaveBeenCalledTimes(3)
})

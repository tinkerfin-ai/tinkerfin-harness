import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getResourceConnectionState, startNotificationFeed } from './notifications'
import { startResourceFeed } from './shared/resourceFeed'
import { watchResource } from './shared/watchResource'

vi.mock('./shared/resourceFeed', () => ({ startResourceFeed: vi.fn() }))
const release: Array<() => void> = []
beforeEach(() => { vi.useFakeTimers(); vi.mocked(startResourceFeed).mockReset() })
afterEach(() => { release.splice(0).reverse().forEach(close => close()); vi.useRealTimers() })

it('共享连接的 ready 只触发一次基线，最后一个使用者退出时释放连接', async () => {
  const close = vi.fn()
  vi.mocked(startResourceFeed).mockReturnValue(close)
  const first = startNotificationFeed('project')
  release.push(first)
  const second = startNotificationFeed('project')
  release.push(second)
  const read = vi.fn(async () => 'current')
  const watch = watchResource({ read, update: vi.fn(), matches: () => true })
  release.push(watch.close)
  expect(startResourceFeed).toHaveBeenCalledOnce()
  expect(getResourceConnectionState()).toBe('connecting')
  expect(read).not.toHaveBeenCalled()
  const feed = vi.mocked(startResourceFeed).mock.calls[0][0]
  feed.onState?.('ready')
  feed.onFrame({ id: null, event: 'ready', data: {} })
  await vi.advanceTimersByTimeAsync(90_000)
  expect(read).toHaveBeenCalledOnce()
  second()
  expect(close).not.toHaveBeenCalled()
  first()
  expect(close).toHaveBeenCalledOnce()
  expect(getResourceConnectionState()).toBe('inactive')
})

it('通知连接失败仍能读基线，连续连接失败不反复查询数据，恢复后再同步', async () => {
  vi.mocked(startResourceFeed).mockReturnValue(vi.fn())
  release.push(startNotificationFeed('project'))
  const read = vi.fn(async () => 'current')
  release.push(watchResource({ read, update: vi.fn(), matches: () => true }).close)
  const feed = vi.mocked(startResourceFeed).mock.calls[0][0]
  feed.onState?.('disconnected')
  await vi.advanceTimersByTimeAsync(0)
  expect(read).toHaveBeenCalledOnce()
  feed.onState?.('connecting'); feed.onState?.('disconnected')
  await vi.advanceTimersByTimeAsync(90_000)
  expect(read).toHaveBeenCalledOnce()
  feed.onState?.('ready'); feed.onFrame({ id: null, event: 'ready', data: {} })
  await vi.advanceTimersByTimeAsync(0)
  expect(read).toHaveBeenCalledTimes(2)
})

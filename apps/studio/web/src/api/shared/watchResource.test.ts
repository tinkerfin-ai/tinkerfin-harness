import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { clearAuthSession, saveAuthSession } from '../../auth/session'
import { mockResourceNotices } from '../../test/resourceNotices'
import { getServerAddress, setServerAddress } from './config'
import { watchResource } from './watchResource'

let notices: ReturnType<typeof mockResourceNotices>
const closes: (() => void)[] = []
const settle = () => vi.advanceTimersByTimeAsync(0)
const matches = (change: { key: string }) => change.key === 'resource'
function signIn(token: string) {
  saveAuthSession({ serverAddress: getServerAddress(), token, tokenType: 'Bearer', expiresAt: '2100-01-01T00:00:00Z', user: {
    user_id: token === 'one' ? 1 : 2, username: token, display_name: token, avatar_url: null, disabled: false, roles: [],
  } })
}
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2030-01-01T00:00:00Z')); clearAuthSession(); notices = mockResourceNotices() })
afterEach(async () => { for (const close of closes.splice(0)) close(); await settle(); clearAuthSession(); vi.restoreAllMocks(); vi.useRealTimers() })

it('读取期间的多次失效合并为下一次读取，不丢通知且不并发查询', async () => {
  let release!: (value: number) => void
  const read = vi.fn().mockImplementationOnce(() => new Promise<number>(resolve => { release = resolve })).mockResolvedValue(2)
  const update = vi.fn()
  closes.push(watchResource({ read, update, matches }).close)
  notices.changed('test.changed', 'resource')
  notices.changed('test.changed', 'resource')
  expect(read).toHaveBeenCalledTimes(1)
  release(1)
  await settle()
  expect(read).toHaveBeenCalledTimes(2)
  expect(update.mock.calls.map(call => call[0])).toEqual([1, 2])
})

it('结果持续变化至少间隔两秒读取，静默资源只在三十秒校准', async () => {
  const read = vi.fn().mockResolvedValue('current')
  closes.push(watchResource({ read, update: vi.fn(), matches, minRefreshMs: 2000 }).close)
  await settle()
  notices.changed('trace.changed', 'resource')
  notices.changed('trace.changed', 'unrelated')
  notices.resync()
  await vi.advanceTimersByTimeAsync(1999)
  expect(read).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(read).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(29_999)
  expect(read).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(1)
  expect(read).toHaveBeenCalledTimes(3)
})

it('隐藏取消读取并拒绝迟到值，重新可见和连接ready都要求最新基线', async () => {
  let release!: (value: string) => void
  const visibility = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  const read = vi.fn().mockImplementationOnce(() => new Promise<string>(resolve => { release = resolve })).mockResolvedValue('current')
  const update = vi.fn()
  closes.push(watchResource({ read, update, matches }).close)
  const signal = read.mock.calls[0][0] as AbortSignal
  visibility.mockReturnValue(true)
  document.dispatchEvent(new Event('visibilitychange'))
  expect(signal.aborted).toBe(true)
  release('stale')
  await vi.advanceTimersByTimeAsync(60_000)
  expect(update).not.toHaveBeenCalled()
  expect(read).toHaveBeenCalledTimes(1)
  visibility.mockReturnValue(false)
  document.dispatchEvent(new Event('visibilitychange'))
  await settle()
  notices.resync()
  await settle()
  expect(read).toHaveBeenCalledTimes(3)
  expect(update.mock.calls.map(call => call[0])).toEqual(['current', 'current'])
})

it.each(['account', 'server'])('身份变化%s使旧账号结果失效，即使请求忽略取消', async kind => {
  signIn('one')
  let release!: (value: string) => void
  const read = vi.fn().mockImplementationOnce(() => new Promise<string>(resolve => { release = resolve })).mockResolvedValue('new')
  const update = vi.fn()
  closes.push(watchResource({ read, update, matches }).close)
  if (kind === 'server') setServerAddress('http://localhost:4567')
  signIn('two')
  expect((read.mock.calls[0][0] as AbortSignal).aborted).toBe(true)
  release('old')
  await settle()
  expect(update.mock.calls.map(call => call[0])).toEqual(['new'])
})

import { describe, expect, it, vi } from 'vitest'

import { TaskTraceFollowOwnership } from './followOwnership'

const waitForAbort = (
  events: string[],
  label: string,
  options: { includeTaskTrace: boolean; signal: AbortSignal },
) => new Promise<void>((resolve) => {
  events.push(`${label}:start:${options.includeTaskTrace}`)
  const finish = () => {
    events.push(`${label}:settled`)
    resolve()
  }
  if (options.signal.aborted) finish()
  else options.signal.addEventListener('abort', finish, { once: true })
})

describe('TaskTraceFollowOwnership', () => {
  it('终止同时撤销尚未执行的跟随、移交与停止，随后新请求可独立开始', async () => {
    const ownership = new TaskTraceFollowOwnership()
    const start = vi.fn(async () => {})
    const queuedFollow = ownership.follow('thread-a', start)
    const queuedHandoff = ownership.handoff('thread-a')
    const queuedStop = ownership.stop('thread-b')
    ownership.abortAll()
    await Promise.all([queuedFollow, queuedHandoff, queuedStop])
    expect(start).not.toHaveBeenCalled()
    await ownership.follow('thread-a', start)
    expect(start).toHaveBeenCalledWith(expect.objectContaining({ includeTaskTrace: false }))
    await ownership.close()
  })

  it('等待已有跟随结束期间撤销，不启动排队的替代订阅', async () => {
    const ownership = new TaskTraceFollowOwnership()
    let release!: () => void
    let entered!: () => void
    let stopping!: () => void
    const started = new Promise<void>(resolve => { entered = resolve })
    const stoppingStarted = new Promise<void>(resolve => { stopping = resolve })
    const stopped = new Promise<void>(resolve => { release = resolve })
    const first = ownership.follow('thread-a', async ({ signal }) => {
      signal.addEventListener('abort', stopping, { once: true })
      entered()
      await stopped
    })
    await started
    const handoff = ownership.handoff('thread-a')
    await stoppingStarted
    const replacement = vi.fn(async () => {})
    const second = ownership.follow('thread-a', replacement)
    ownership.abortAll()
    release()
    await Promise.all([first, handoff, second])
    expect(replacement).not.toHaveBeenCalled()
    await ownership.close()
  })

  it('重新开始跟随会等待已取消连接释放，随后建立本轮连接', async () => {
    const ownership = new TaskTraceFollowOwnership()
    let entered!: () => void
    let release!: () => void
    const started = new Promise<void>(resolve => { entered = resolve })
    const finished = new Promise<void>(resolve => { release = resolve })
    const first = ownership.follow('background', async () => { entered(); await finished })
    await started
    await ownership.handoff('foreground')
    ownership.abortAll()
    const operation = vi.fn(async ({ signal }: { signal: AbortSignal }) => { expect(signal.aborted).toBe(false) })
    const next = ownership.follow('background', operation)
    queueMicrotask(release)
    await Promise.all([first, next])
    expect(operation).toHaveBeenCalledOnce()
    await ownership.close()
  })

  it('settles the old true follow before promoting the next thread', async () => {
    const ownership = new TaskTraceFollowOwnership()
    const events: string[] = []
    await ownership.handoff('thread-a')
    const first = ownership.follow(
      'thread-a',
      (options) => waitForAbort(events, 'a', options),
    )
    await Promise.resolve()

    const handoff = await ownership.handoff('thread-b')
    expect(handoff.demotedThreadIds).toEqual(['thread-a'])
    expect(events).toEqual(['a:start:true', 'a:settled'])
    await first

    const background = ownership.follow(
      'thread-a',
      (options) => waitForAbort(events, 'a-background', options),
    )
    const current = ownership.follow(
      'thread-b',
      (options) => waitForAbort(events, 'b', options),
    )
    await vi.waitFor(() => {
      expect(events).toContain('a-background:start:false')
      expect(events).toContain('b:start:true')
    })

    await ownership.close()
    await Promise.all([background, current])
    expect(events.slice(-2).sort()).toEqual(['a-background:settled', 'b:settled'])
  })

  it('serializes rapid handoffs and leaves only the final owner eligible', async () => {
    const ownership = new TaskTraceFollowOwnership()

    const results = await Promise.all([
      ownership.handoff('thread-a'),
      ownership.handoff('thread-b'),
      ownership.handoff('thread-c'),
    ])
    const events: string[] = []
    const current = ownership.follow(
      'thread-c',
      (options) => waitForAbort(events, 'current', options),
    )
    const background = ownership.follow(
      'thread-a',
      (options) => waitForAbort(events, 'background', options),
    )
    await vi.waitFor(() => {
      expect(events).toContain('current:start:true')
      expect(events).toContain('background:start:false')
    })

    expect(results.map((result) => result.epoch)).toEqual([1, 2, 3])
    await ownership.close()
    await Promise.all([current, background])
  })
})

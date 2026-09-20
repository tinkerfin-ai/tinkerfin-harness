import { act, render, screen } from '@testing-library/react'
import { useRef, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConversationDisplayEntry } from '../conversation/todoTrace/displayEntries'
import {
  useConversationMessageWindow,
} from './useConversationMessageWindow'

const entry = (index: number): ConversationDisplayEntry => ({
  type: 'message',
  message: {
    id: `message-${index}`,
    role: 'user',
    meta: { traceMessageId: `trace-message-${index}` },
    content: `消息 ${index}`,
    createdAt: '2026-08-31T00:00:00.000Z',
  },
})

let current: ReturnType<typeof useConversationMessageWindow> | undefined

function Harness({
  entries,
  historyCursor,
  loadOlderTrace,
  threadId = 'thread-1',
  readingPosition,
}: {
  entries: ConversationDisplayEntry[]
  threadId?: string
  readingPosition?: Parameters<typeof useConversationMessageWindow>[0]['readingPosition']
  historyCursor?: string | null
  loadOlderTrace: (
    threadId: string,
    options?: { signal?: AbortSignal },
  ) => Promise<boolean>
}) {
  const paneRef = useRef<HTMLElement>(null)
  current = useConversationMessageWindow({
    threadId,
    readingPosition,
    entries,
    historyCursor,
    paneRef,
    loadOlderTrace,
  })
  return (
    <section ref={paneRef}>
      {current.visibleEntries.map((item) => item.type === 'message' && (
        <article key={item.message.id} id={item.message.id}>
          {item.message.content}
        </article>
      ))}
    </section>
  )
}

beforeEach(() => {
  current = undefined
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn(),
  })
})

describe('useConversationMessageWindow', () => {
  it('目录可按顶部对齐定位窗口外消息，且不额外请求历史', async () => {
    const loadOlderTrace = vi.fn(async () => false)
    render(<Harness entries={Array.from({ length: 250 }, (_, index) => entry(index))} loadOlderTrace={loadOlderTrace} />)
    let locating: Promise<string> | undefined
    act(() => { locating = current?.revealMessage('message-10', 'start') })
    expect(await locating).toBe('found')
    expect(screen.getByText('消息 10')).toHaveFocus()
    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ block: 'start' }))
    expect(loadOlderTrace).not.toHaveBeenCalled()
  })

  it('keeps the latest batch mounted and expands earlier hydrated entries', async () => {
    const loadOlderTrace = vi.fn(async () => false)
    render(
      <Harness
        entries={Array.from({ length: 150 }, (_, index) => entry(index))}
        loadOlderTrace={loadOlderTrace}
      />,
    )

    expect(screen.queryByText('消息 49')).not.toBeInTheDocument()
    expect(screen.getByText('消息 50')).toBeInTheDocument()
    expect(current?.visibleEntries).toHaveLength(100)

    const trigger = document.createElement('button')
    document.body.append(trigger)
    await act(async () => current?.loadEarlierMessages(trigger))

    expect(screen.getByText('消息 0')).toBeInTheDocument()
    expect(current?.visibleEntries).toHaveLength(150)
    expect(loadOlderTrace).not.toHaveBeenCalled()
    trigger.remove()
  })

  it('applies the latest batch during the first non-empty hydration render', () => {
    const loadOlderTrace = vi.fn(async () => false)
    const { rerender } = render(
      <Harness entries={[]} loadOlderTrace={loadOlderTrace} />,
    )

    rerender(
      <Harness
        entries={Array.from({ length: 101 }, (_, index) => entry(index))}
        loadOlderTrace={loadOlderTrace}
      />,
    )

    expect(current?.visibleEntries).toHaveLength(100)
    expect(screen.queryByText('消息 0')).not.toBeInTheDocument()
    expect(screen.getByText('消息 100')).toBeInTheDocument()
  })

  it('reveals a hydrated message outside the current render window', async () => {
    render(
      <Harness
        entries={Array.from({ length: 150 }, (_, index) => entry(index))}
        loadOlderTrace={async () => false}
      />,
    )

    let locating!: ReturnType<typeof currentReveal>
    act(() => {
      locating = currentReveal('trace-message-10')
    })
    const result = await locating

    expect(result).toBe('found')
    expect(screen.getByText('消息 10')).toHaveFocus()
    expect(screen.getByText('消息 10')).toHaveClass('todo-trace-locate-target')
    expect(current?.visibleEntries.length).toBeLessThanOrEqual(100)
    expect(screen.queryByText('消息 149')).not.toBeInTheDocument()

    act(() => current?.restoreTail())
    expect(screen.getByText('消息 149')).toBeInTheDocument()
    expect(current?.followsTail).toBe(true)
  })

  it('loads an older fixed Trace page before revealing its message', async () => {
    const loadOlderTrace = vi.fn()

    function StatefulHarness() {
      const [entries, setEntries] = useState([entry(100), entry(101)])
      const [cursor, setCursor] = useState<string | null>('older-page')
      return (
        <Harness
          entries={entries}
          historyCursor={cursor}
          loadOlderTrace={async (_threadId, options) => {
            loadOlderTrace(options?.signal)
            if (options?.signal?.aborted) return false
            setEntries([entry(1), ...entries])
            setCursor(null)
            return true
          }}
        />
      )
    }

    render(<StatefulHarness />)
    let locating!: ReturnType<typeof currentReveal>
    act(() => {
      locating = currentReveal('trace-message-1')
    })
    const result = await locating

    expect(result).toBe('found')
    expect(loadOlderTrace).toHaveBeenCalledTimes(1)
    expect(screen.getByText('消息 1')).toHaveFocus()
  })

  it('cancels the previous locator and forwards its AbortSignal', async () => {
    let firstSignal: AbortSignal | undefined
    let requestCount = 0
    const loadOlderTrace = vi.fn()

    function TakeoverHarness() {
      const [entries, setEntries] = useState([entry(2)])
      return (
        <Harness
          entries={entries}
          historyCursor="older-page"
          loadOlderTrace={(_threadId, options) => {
            requestCount += 1
            loadOlderTrace(options?.signal)
            if (requestCount === 1) {
              return new Promise<boolean>((resolve) => {
                firstSignal = options?.signal
                options?.signal?.addEventListener('abort', () => resolve(false), { once: true })
              })
            }
            setEntries([entry(3), ...entries])
            return Promise.resolve(true)
          }}
        />
      )
    }
    render(<TakeoverHarness />)

    let first!: Promise<unknown>
    let second!: Promise<unknown>
    act(() => {
      first = currentReveal('missing-message')
    })
    await Promise.resolve()
    act(() => {
      second = currentReveal('message-3')
    })
    expect(await second).toBe('found')
    expect(await first).toBe('cancelled')

    expect(firstSignal?.aborted).toBe(true)
    expect(loadOlderTrace).toHaveBeenCalledTimes(2)
  })

  it('reports an exhausted history cursor without requesting another page', async () => {
    const loadOlderTrace = vi.fn(async () => false)
    render(<Harness entries={[entry(2)]} loadOlderTrace={loadOlderTrace} />)

    await expect(currentReveal('missing-message')).resolves.toBe('not-found')
    expect(loadOlderTrace).not.toHaveBeenCalled()
  })
})

const currentReveal = (messageId: string) => {
  if (!current) throw new Error('消息窗口尚未挂载')
  return current.revealMessage(messageId)
}


describe('页面内阅读锚点恢复', () => {
  let frames: Map<number, FrameRequestCallback>
  let nextFrame: number
  const flushFrames = async () => {
    while (frames.size) {
      const pending = [...frames.values()]
      frames.clear()
      await act(async () => pending.forEach(callback => callback(0)))
    }
  }
  beforeEach(() => {
    frames = new Map()
    nextFrame = 0
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.set(++nextFrame, callback)
      return nextFrame
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  })
  afterEach(() => vi.unstubAllGlobals())

  const readingOptions = () => ({
    isHydrated: true,
    threadIds: ['thread-1', 'thread-2'],
    isFollowingLatest: vi.fn(() => false),
    pauseFollowing: vi.fn(),
    scrollToBottom: vi.fn(),
    onMissing: vi.fn(),
  })
  const saveAnchor = () => {
    const anchor = screen.getByText('消息 1')
    anchor.getBoundingClientRect = () => ({ top: -25, bottom: 75 }) as DOMRect
    act(() => current?.captureReadingPosition())
  }

  it('重新水化后恢复稳定消息偏移，不移动焦点或突出消息', async () => {
    const options = readingOptions()
    const loadOlderTrace = vi.fn(async () => false)
    const view = render(<Harness entries={[entry(1), entry(2)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    saveAnchor()
    view.rerender(<Harness threadId="thread-2" entries={[entry(2)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    view.rerender(<Harness entries={[]} readingPosition={{ ...options, isHydrated: false }} loadOlderTrace={loadOlderTrace} />)
    expect(options.pauseFollowing).not.toHaveBeenCalled()
    view.rerender(<Harness entries={[entry(1), entry(2)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    await act(async () => {})
    expect(screen.getByText('消息 1').parentElement?.scrollTop).toBe(25)
    expect(screen.getByText('消息 1')).not.toHaveFocus()
    expect(screen.getByText('消息 1')).not.toHaveClass('todo-trace-locate-target')
    expect(options.pauseFollowing).toHaveBeenCalledOnce()
    expect(loadOlderTrace).not.toHaveBeenCalled()
  })

  it('窗口外锚点通过同一定位窗口恢复，仍只展示一批消息', async () => {
    const options = readingOptions()
    const loadOlderTrace = vi.fn(async () => false)
    const view = render(<Harness entries={[entry(1)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    saveAnchor()
    view.rerender(<Harness threadId="thread-2" entries={[entry(2)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    view.rerender(<Harness entries={Array.from({ length: 250 }, (_, index) => entry(index))} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    await flushFrames()
    expect(screen.getByText('消息 1')).toBeInTheDocument()
    expect(screen.getByText('消息 1')).not.toHaveFocus()
    expect(current?.visibleEntries).toHaveLength(100)
    expect(options.onMissing).not.toHaveBeenCalled()
  })

  it('淘汰重开通过更早历史页恢复锚点，加载失败保留原位置供重试', async () => {
    const options = readingOptions()
    const loadOlderTrace = vi.fn(async () => false)
    const view = render(<Harness entries={[entry(1)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    saveAnchor()
    view.rerender(<Harness threadId="thread-2" entries={[entry(2)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    view.rerender(<Harness entries={[entry(100)]} historyCursor="older" readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    await act(async () => {})
    expect(current?.readingPositionFailed).toBe(true)
    expect(options.onMissing).not.toHaveBeenCalled()
    act(() => current?.captureReadingPosition())
    loadOlderTrace.mockImplementationOnce(async () => {
      view.rerender(<Harness entries={[entry(1), entry(100)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
      return true
    })
    let restoring: Promise<void> | undefined
    act(() => { restoring = current?.retryReadingPosition() })
    await flushFrames()
    await restoring
    expect(current?.readingPositionFailed).toBe(false)
    expect(screen.getByText('消息 1').parentElement?.scrollTop).toBe(25)
    expect(loadOlderTrace).toHaveBeenCalledTimes(2)
  })

  it.each(['user', 'navigation'])('%s 取消恢复并阻止迟到分页移动视口', async reason => {
    const options = readingOptions()
    let signal: AbortSignal | undefined
    let finish: ((value: boolean) => void) | undefined
    const loadOlderTrace = vi.fn((_threadId: string, request?: { signal?: AbortSignal }) => {
      signal = request?.signal
      return new Promise<boolean>(resolve => { finish = resolve })
    })
    const view = render(<Harness entries={[entry(1)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    saveAnchor()
    view.rerender(<Harness threadId="thread-2" entries={[entry(2)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    view.rerender(<Harness entries={[entry(100)]} historyCursor="older" readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    if (reason === 'user') act(() => current?.cancelReadingRestore())
    else view.rerender(<Harness threadId="thread-2" entries={[entry(2)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    expect(signal?.aborted).toBe(true)
    await act(async () => finish?.(true))
    expect(options.scrollToBottom).not.toHaveBeenCalled()
    expect(options.onMissing).not.toHaveBeenCalled()
    expect(current?.readingPositionFailed).toBe(false)
  })

  it('历史已无锚点时回到最新消息并通知，跟随尾部直接恢复', async () => {
    const options = readingOptions()
    const loadOlderTrace = vi.fn(async () => false)
    const view = render(<Harness entries={[entry(1)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    saveAnchor()
    view.rerender(<Harness threadId="thread-2" entries={[entry(2)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    view.rerender(<Harness entries={[entry(100)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    await act(async () => {})
    expect(options.onMissing).toHaveBeenCalledOnce()
    expect(options.scrollToBottom).toHaveBeenCalledOnce()
    options.isFollowingLatest.mockReturnValue(true)
    act(() => current?.captureReadingPosition())
    view.rerender(<Harness threadId="thread-2" entries={[entry(2)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    view.rerender(<Harness entries={[entry(101)]} readingPosition={options} loadOlderTrace={loadOlderTrace} />)
    expect(options.scrollToBottom).toHaveBeenCalledTimes(2)
    expect(options.onMissing).toHaveBeenCalledOnce()
    expect(loadOlderTrace).not.toHaveBeenCalled()
  })
})

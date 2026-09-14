import { act, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PropsWithChildren } from 'react'

import { useTypewriterText } from './useTypewriterText'
import { MessageBlock } from './MessageBlock'
import { applyConversationEvent } from '../agui/runtime'
import { buildEmptyConversation } from '../../../lib/workspace'
import type { ConversationAgUiEvent } from '../../../api/conversation/types'
import { TextRevealProgressContext } from './textRevealProgress'

const frames = new Map<number, FrameRequestCallback>()
let frameId = 0
let progress: Map<string, string>

function ProgressScope({ children }: PropsWithChildren) {
  return <TextRevealProgressContext.Provider value={progress}>{children}</TextRevealProgressContext.Provider>
}

function frame() {
  act(() => {
    const callbacks = [...frames.values()]
    frames.clear()
    callbacks.forEach(callback => callback(0))
  })
}

describe('实时正文逐字展示', () => {
  beforeEach(() => {
    frames.clear()
    progress = new Map()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.set(++frameId, callback)
      return frameId
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  })
  afterEach(() => vi.unstubAllGlobals())

  it('整段及结束一起到达时仍每帧一个字，完整内容不被改写', () => {
    const source = { key: 'thread/run/answer', initialContent: '' }
    const content = '逐字显示整段回复'
    const { result, unmount } = renderHook(() => useTypewriterText(content, source, true), { wrapper: ProgressScope })
    expect(result.current).toBe('')
    for (let index = 1; index <= content.length; index++) {
      frame()
      expect(result.current).toBe(content.slice(0, index))
    }
    expect(frames.size).toBe(0)
    unmount()
  })

  it('追加大块文本和终态不会跳字或重播，最后一个未完整字素等待后续输入', () => {
    const source = { key: 'thread/run/answer', initialContent: '' }
    const { result, rerender, unmount } = renderHook(
      ({ content, complete }) => useTypewriterText(content, source, complete),
      { initialProps: { content: '你', complete: false }, wrapper: ProgressScope },
    )
    frame()
    expect(result.current).toBe('')
    rerender({ content: '你好世界', complete: false })
    frame()
    expect(result.current).toBe('你')
    rerender({ content: '你好世界再见', complete: true })
    frame()
    expect(result.current).toBe('你好')
    for (let i = 0; i < 4; i++) frame()
    expect(result.current).toBe('你好世界再见')
    unmount()
  })

  it('组合表情、代理对和音标跨片段不会被拆坏', () => {
    const source = { key: 'thread/run/answer', initialContent: '' }
    const { result, rerender, unmount } = renderHook(
      ({ content, complete }) => useTypewriterText(content, source, complete),
      { initialProps: { content: '\ud83d', complete: false }, wrapper: ProgressScope },
    )
    frame()
    expect(result.current).toBe('')
    rerender({ content: '👩‍💻e', complete: false })
    frame()
    expect(result.current).toBe('👩‍💻')
    rerender({ content: '👩‍💻e\u0301好', complete: true })
    frame()
    expect(result.current).toBe('👩‍💻e\u0301')
    frame()
    expect(result.current).toBe('👩‍💻e\u0301好')
    unmount()
  })

  it('历史消息直接显示，不创建逐字任务', () => {
    const { result, unmount } = renderHook(() => useTypewriterText('已有历史', undefined, true))
    expect(result.current).toBe('已有历史')
    expect(frames.size).toBe(0)
    unmount()
  })

  it('卸载取消任务，同一消息重挂从已显示位置继续，其他消息进度独立', () => {
    const source = { key: 'thread/run/answer', initialContent: '' }
    const first = renderHook(() => useTypewriterText('第一条', source, true), { wrapper: ProgressScope })
    frame()
    expect(first.result.current).toBe('第')
    first.unmount()
    expect(frames.size).toBe(0)
    const resumed = renderHook(() => useTypewriterText('第一条', { ...source }, true), { wrapper: ProgressScope })
    const otherSource = { key: 'another-thread/run/answer', initialContent: '' }
    const other = renderHook(() => useTypewriterText('另外一条', otherSource, true), { wrapper: ProgressScope })
    expect(resumed.result.current).toBe('第')
    expect(other.result.current).toBe('')
    frame()
    expect(resumed.result.current).toBe('第一')
    expect(other.result.current).toBe('另')
    resumed.unmount()
    other.unmount()
    expect(frames.size).toBe(0)
  })

  it.each(['RUN_FINISHED', 'RUN_ERROR'] as const)('同批文本和 %s 到达后继续逐字显示，显示完整后才提供复制', (terminal) => {
    const events: ConversationAgUiEvent[] = [
      { type: 'RUN_STARTED', threadId: 'thread', runId: 'run' },
      { type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '你好吗' },
      { type: 'TEXT_MESSAGE_END', messageId: 'answer' },
      terminal === 'RUN_FINISHED'
        ? { type: 'RUN_FINISHED', threadId: 'thread', runId: 'run' }
        : { type: 'RUN_ERROR', code: 'cancelled', message: '已停止' },
    ]
    const conversation = events.reduce(applyConversationEvent, buildEmptyConversation({ now: '' }))
    const message = conversation.messages.find(message => message.id === 'answer')!
    expect(message.content).toBe('你好吗')
    const { container, unmount } = render(<MessageBlock message={message} />, { wrapper: ProgressScope })
    const article = container.querySelector('article')!
    expect(article.textContent).toBe('')
    frame()
    expect(article.textContent).toBe('你')
    expect(screen.queryByRole('group', { name: '回答操作' })).not.toBeInTheDocument()
    frame()
    expect(article.textContent).toBe('你好')
    frame()
    expect(screen.getByText('你好吗', { exact: true })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: '回答操作' })).toBeInTheDocument()
    unmount()
  })

  it.each([2, 6])('已显示 %s 字后更换投影对象，进度不倒退且继续逐字显示', (displayed) => {
    const content = '历史同步不重播'
    const { result, rerender, unmount } = renderHook(
      ({ source }) => useTypewriterText(content, source, true),
      { initialProps: { source: { key: 'thread/run/answer', initialContent: '' } }, wrapper: ProgressScope },
    )
    for (let i = 0; i < displayed; i++) frame()
    expect(result.current).toBe(content.slice(0, displayed))
    rerender({ source: { key: 'thread/run/answer', initialContent: '' } })
    expect(result.current).toBe(content.slice(0, displayed))
    frame()
    expect(result.current).toBe(content.slice(0, displayed + 1))
    unmount()
  })

  it('恢复前缀立即显示并覆盖旧动画进度，只对新增正文逐字呈现', () => {
    const key = 'thread/run/answer'
    progress.set(key, '已')
    const prefix = '已经收到的完整正文'
    const { result, rerender, unmount } = renderHook(
      ({ content, source }) => useTypewriterText(content, source, true),
      { initialProps: { content: prefix + '新增', source: { key, initialContent: '' } }, wrapper: ProgressScope },
    )
    expect(result.current).toBe('已')
    rerender({ content: prefix + '新增', source: { key, initialContent: prefix } })
    expect(result.current).toBe(prefix)
    expect(progress.get(key)).toBe(prefix)
    frame()
    expect(result.current).toBe(prefix + '新')
    rerender({ content: '替换后的内容', source: { key, initialContent: prefix } })
    expect(result.current).toBe('')
    frame()
    expect(result.current).toBe('替')
    unmount()
    expect(frames.size).toBe(0)
  })

  it('工作台之间不共享相同消息身份的显示进度', () => {
    const source = { key: 'thread/run/answer', initialContent: '' }
    const first = renderHook(() => useTypewriterText('回复内容', source, true), { wrapper: ProgressScope })
    frame()
    first.unmount()
    progress = new Map()
    const second = renderHook(() => useTypewriterText('回复内容', source, true), { wrapper: ProgressScope })
    expect(second.result.current).toBe('')
    second.unmount()
  })
})

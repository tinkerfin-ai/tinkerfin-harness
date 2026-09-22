import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatRequestPayload } from '../../../api/conversation/types'
import {
  clearActiveRunSession,
  readActiveRunSession,
  writeActiveRunSession,
} from './activeRunSession'

const payload: ChatRequestPayload = {
  threadId: 'thread-active',
  runId: 'run-active',
  state: {},
  messages: [{ id: 'request-run-active', role: 'user', content: '继续输出' }],
  tools: [],
  context: [],
  forwardedProps: { accessMode: 'write_approval', model: 'main', command: { plan: 'off' } },
}

describe('active run session', () => {
  beforeEach(() => window.sessionStorage.clear())
  afterEach(() => vi.restoreAllMocks())

  it('round-trips one active run and only its owner can clear it', () => {
    writeActiveRunSession({
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 41,
    })

    expect(readActiveRunSession('thread-active')).toEqual({
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 41,
    })
    clearActiveRunSession('other-run')
    expect(readActiveRunSession('thread-active')?.payload.runId).toBe('run-active')
    clearActiveRunSession('run-active')
    expect(readActiveRunSession('thread-active')).toBeNull()
  })

  it('rejects a persisted protocol message without its required ID', () => {
    window.sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify([{
      threadId: 'thread-active',
      payload: {
        ...payload,
        messages: [{ role: 'user', content: 'invalid' }],
      },
      mode: 'start',
      lastSeq: 1,
    }]))

    expect(readActiveRunSession('thread-active')).toBeNull()
    expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
  })

  it('rejects an unexpected field in a stored run', () => {
    window.sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify([{
      unexpected: true,
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 1,
    }]))

    expect(readActiveRunSession('thread-active')).toBeNull()
    expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
  })

  it('returns null when session storage access and cleanup are both blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })

    expect(readActiveRunSession('thread-active')).toBeNull()
  })
})

it('多个会话的游标独立保存和清理', () => {
  window.sessionStorage.clear()
  writeActiveRunSession({ threadId: 'thread-active', payload, mode: 'start', lastSeq: 3 })
  writeActiveRunSession({ threadId: 'second', payload: { ...payload, threadId: 'second', runId: 'second-run' }, mode: 'start', lastSeq: 8 })
  expect(readActiveRunSession('thread-active')?.lastSeq).toBe(3)
  expect(readActiveRunSession('second')?.lastSeq).toBe(8)
  clearActiveRunSession('second-run')
  expect(readActiveRunSession('thread-active')?.lastSeq).toBe(3)
  expect(readActiveRunSession('second')).toBeNull()
})

it('压缩恢复只保存原运行和所选模型，不引入聊天输入', () => {
  const compact = { threadId: 't', payload: { threadId: 't', runId: 'compact', model: 'main' }, mode: 'compact' as const, lastSeq: 8 }
  writeActiveRunSession(compact)
  expect(readActiveRunSession('t')).toEqual(compact)
})

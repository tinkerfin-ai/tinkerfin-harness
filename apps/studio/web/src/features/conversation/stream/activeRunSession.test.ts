import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatRequestPayload } from '../../../api/conversation/types'
import { attachmentInput } from '../attachments/content'
import {
  clearActiveRunSession,
  readActiveRunSession,
  readActiveRunSessions,
  writeActiveRunSession,
} from './activeRunSession'

const payload: ChatRequestPayload = {
  threadId: 'thread-active',
  runId: 'run-active',
  state: {},
  messages: [{ id: 'request-run-active', role: 'user', content: '继续输出' }],
  tools: [],
  context: [],
  forwardedProps: {projectId: 'project-1',  skillIds: [], accessMode: 'write_approval', model: 'main', command: { plan: 'off' } },
}

describe('active run session', () => {
  beforeEach(() => window.sessionStorage.clear())
  afterEach(() => vi.restoreAllMocks())

  it('round-trips one active run and only its owner can clear it', () => {
    writeActiveRunSession({projectId: 'project-1',
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 41,
    })

    expect(readActiveRunSession('thread-active', 'project-1')).toEqual({projectId: 'project-1',
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 41,
    })
    clearActiveRunSession('other-run')
    expect(readActiveRunSession('thread-active', 'project-1')?.payload.runId).toBe('run-active')
    clearActiveRunSession('run-active')
    expect(readActiveRunSession('thread-active', 'project-1')).toBeNull()
  })

  it('rejects a persisted protocol message without its required ID', () => {
    window.sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify([{projectId: 'project-1',
      threadId: 'thread-active',
      payload: {
        ...payload,
        messages: [{ role: 'user', content: 'invalid' }],
      },
      mode: 'start',
      lastSeq: 1,
    }]))

    expect(readActiveRunSession('thread-active', 'project-1')).toBeNull()
    expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
  })

  it('文本和技能 ZIP 运行共同保存时保留完整请求、技能选择和游标', () => {
    const text = {projectId: 'project-1',  threadId: payload.threadId, payload, mode: 'start' as const, lastSeq: 3 }
    const zipPayload: ChatRequestPayload = {
      ...payload,
      threadId: '',
      runId: 'run-zip',
      forwardedProps: { ...payload.forwardedProps, skillIds: ['selected-skill'] },
      messages: [{ id: 'request-run-zip', role: 'user', content: [
        { type: 'text', text: '安装这个技能' },
        attachmentInput({ id: 'zip', name: 'reports.zip', mime_type: 'application/zip', size_bytes: 3 }),
      ] }],
    }
    const zip = {projectId: 'project-1',  threadId: '', payload: zipPayload, mode: 'start' as const, lastSeq: 0 }
    writeActiveRunSession(text)
    writeActiveRunSession(zip)

    expect(readActiveRunSessions()).toEqual([text, zip])
    clearActiveRunSession(zipPayload.runId)
    expect(readActiveRunSessions()).toEqual([text])
  })

  it('拒绝附件数组中的非对象内容块', () => {
    window.sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify([{projectId: 'project-1',
      threadId: payload.threadId,
      payload: {
        ...payload,
        messages: [{ id: 'request-run-active', role: 'user', content: [
          attachmentInput({ id: 'zip', name: 'reports.zip', mime_type: 'application/zip', size_bytes: 3 }),
          null,
        ] }],
      },
      mode: 'start',
      lastSeq: 1,
    }]))

    expect(readActiveRunSession(payload.threadId, 'project-1')).toBeNull()
    expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
  })

  it('rejects an unexpected field in a stored run', () => {
    window.sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify([{projectId: 'project-1',
      unexpected: true,
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 1,
    }]))

    expect(readActiveRunSession('thread-active', 'project-1')).toBeNull()
    expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
  })

  it('returns null when session storage access and cleanup are both blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })

    expect(readActiveRunSession('thread-active', 'project-1')).toBeNull()
  })
})

it('多个会话的游标独立保存和清理', () => {
  window.sessionStorage.clear()
  writeActiveRunSession({projectId: 'project-1',  threadId: 'thread-active', payload, mode: 'start', lastSeq: 3 })
  writeActiveRunSession({projectId: 'project-1',  threadId: 'second', payload: { ...payload, threadId: 'second', runId: 'second-run' }, mode: 'start', lastSeq: 8 })
  expect(readActiveRunSession('thread-active', 'project-1')?.lastSeq).toBe(3)
  expect(readActiveRunSession('second', 'project-1')?.lastSeq).toBe(8)
  clearActiveRunSession('second-run')
  expect(readActiveRunSession('thread-active', 'project-1')?.lastSeq).toBe(3)
  expect(readActiveRunSession('second', 'project-1')).toBeNull()
})

it('压缩恢复只保存原运行和所选模型，不引入聊天输入', () => {
  const compact = {projectId: 'project-1',  threadId: 't', payload: { threadId: 't', runId: 'compact', model: 'main' }, mode: 'compact' as const, lastSeq: 8 }
  writeActiveRunSession(compact)
  expect(readActiveRunSession('t', 'project-1')).toEqual(compact)
})

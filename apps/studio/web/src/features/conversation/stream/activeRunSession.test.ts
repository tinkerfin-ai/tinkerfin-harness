import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatRequestPayload } from '../../../api/conversation/types'
import { attachmentInput } from '../attachments/content'
import { clearAuthSession, saveAuthSession, updateAuthSession } from '../../../auth/session'
import { setServerAddress } from '../../../api/shared/config'
import { testAuthSession } from '../../../test/authSession'
import {
  captureActiveRunOwner,
  clearActiveRunSession,
  readActiveRunSession,
  readActiveRunSessions,
  writeActiveRunSession,
} from './activeRunSession'

beforeEach(() => {
  saveAuthSession(testAuthSession)
  clearActiveRunSession()
})
afterEach(() => clearAuthSession())

const storeRuns = (runs: unknown) => window.sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify({
  serverAddress: testAuthSession.serverAddress, userId: testAuthSession.user.user_id, runs,
}))

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
    storeRuns([{projectId: 'project-1',
      threadId: 'thread-active',
      payload: {
        ...payload,
        messages: [{ role: 'user', content: 'invalid' }],
      },
      mode: 'start',
      lastSeq: 1,
    }])

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
    storeRuns([{projectId: 'project-1',
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
    }])

    expect(readActiveRunSession(payload.threadId, 'project-1')).toBeNull()
    expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
  })

  it('rejects an unexpected field in a stored run', () => {
    storeRuns([{projectId: 'project-1',
      unexpected: true,
      threadId: 'thread-active',
      payload,
      mode: 'start',
      lastSeq: 1,
    }])

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

const active = { projectId: 'project-1', threadId: payload.threadId, payload, mode: 'start' as const, lastSeq: 3 }

it('同一登录刷新用户资料后仍能保存恢复记录，持久内容不包含凭证', () => {
  const owner = captureActiveRunOwner()
  writeActiveRunSession(active, owner)
  updateAuthSession({ expires_at: testAuthSession.expiresAt, user: { ...testAuthSession.user, avatar_url: 'https://example.test/avatar.jpg' } })
  writeActiveRunSession({ ...active, lastSeq: 4 }, owner)
  expect(readActiveRunSession(payload.threadId, 'project-1')?.lastSeq).toBe(4)
  const raw = window.sessionStorage.getItem('tinkerfin:active-conversation-run')!
  expect(JSON.parse(raw)).toEqual({ serverAddress: testAuthSession.serverAddress, userId: 7, runs: [{ ...active, lastSeq: 4 }] })
  expect(raw).not.toContain(testAuthSession.token)
})

it('全局清空撤销已捕获的写入归属，当前登录可以重新建立记录', () => {
  const owner = captureActiveRunOwner()
  writeActiveRunSession(active, owner)
  clearActiveRunSession()
  writeActiveRunSession(active, owner)
  expect(readActiveRunSessions()).toEqual([])
  writeActiveRunSession(active)
  expect(readActiveRunSessions()).toEqual([active])
})

it('未登录时不读取或保存恢复记录', () => {
  writeActiveRunSession(active)
  clearAuthSession()
  expect(readActiveRunSessions()).toEqual([])
  writeActiveRunSession(active)
  expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
})

it.each(['账号', '服务器', '同账号重新登录'] as const)('%s变化后迟到读写与清理不能覆盖当前记录', change => {
  const owner = captureActiveRunOwner()
  writeActiveRunSession(active, owner)
  const serverAddress = change === '服务器' ? 'http://localhost:8091' : testAuthSession.serverAddress
  if (change === '服务器') setServerAddress(serverAddress)
  saveAuthSession({ ...testAuthSession, serverAddress, token: 'another-login', user: {
    ...testAuthSession.user, user_id: change === '账号' ? 8 : 7,
  } })
  if (change !== '同账号重新登录') expect(readActiveRunSessions()).toEqual([])
  const current = { ...active, lastSeq: 9 }
  writeActiveRunSession(current)
  expect(readActiveRunSessions(owner)).toEqual([])
  writeActiveRunSession(active, owner)
  clearActiveRunSession(active.payload.runId, owner)
  clearActiveRunSession(undefined, owner)
  expect(readActiveRunSessions()).toEqual([current])
})

it.each([null, [], { runs: [], serverAddress: testAuthSession.serverAddress, userId: 7, unexpected: true }])('清理不符合当前缓存边界的输入 %j', value => {
  window.sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify(value))
  expect(readActiveRunSessions()).toEqual([])
  expect(window.sessionStorage.getItem('tinkerfin:active-conversation-run')).toBeNull()
})

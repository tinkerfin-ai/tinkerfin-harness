import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearAuthSession, saveAuthSession } from '../../auth/session'
import { fetchConversationTitle, isConversationTitle } from './titles'

describe('会话标题边界', () => {
  it.each(['中', 'a', '😀'])('按Unicode字符限制32个字符：%s', (character) => {
    const snapshot = { threadId: 'thread', title: character.repeat(32), titleSource: 'generated', titleGenerationStatus: 'succeeded', titleSeq: 2 }
    expect(isConversationTitle(snapshot)).toBe(true)
    expect(isConversationTitle({ ...snapshot, title: character.repeat(33) })).toBe(false)
    expect(isConversationTitle({ ...snapshot, title: '' })).toBe(false)
  })
})

it('标题来源和生成状态必须是字符串', () => {
  expect(isConversationTitle({ threadId: 'thread', title: '标题', titleSource: ['user'], titleGenerationStatus: ['skipped'], titleSeq: 2 })).toBe(false)
})

beforeEach(() => saveAuthSession({
  token: 'title-test', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z',
  user: { user_id: 1, username: 'test', display_name: '测试', avatar_url: null, roles: [], disabled: false },
}))
afterEach(() => { clearAuthSession(); vi.unstubAllGlobals() })

it.each(['valid', 'wrong-thread', 'invalid-sequence'] as const)('标题查询使用认证 API 并校验响应归属及序号：%s', async (scenario) => {
  const controller = new AbortController()
  const snapshot = {
    threadId: scenario === 'wrong-thread' ? 'other' : 'thread/title',
    title: '自动标题', titleSource: 'generated', titleGenerationStatus: 'succeeded',
    titleSeq: scenario === 'invalid-sequence' ? -1 : 2,
  }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), window.location.origin), init)
    expect(new URL(request.url).pathname).toBe('/api/conversation/thread%2Ftitle/title')
    expect(request.method).toBe('GET')
    expect(request.headers.get('Authorization')).toBe('Bearer title-test')
    expect(request.signal.aborted).toBe(false)
    return new Response(JSON.stringify({ code: 0, message: 'success', data: snapshot }), { headers: { 'Content-Type': 'application/json' } })
  }))
  const result = fetchConversationTitle('thread/title', controller.signal)
  if (scenario === 'valid') await expect(result).resolves.toEqual(snapshot)
  else await expect(result).rejects.toMatchObject({ code: 'stream_event_invalid' })
})

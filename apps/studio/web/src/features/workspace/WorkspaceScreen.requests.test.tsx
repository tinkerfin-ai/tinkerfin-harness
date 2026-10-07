import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ConversationHistoryDetail, ConversationHistoryListItem } from '../../api/conversation/history'
import { clearAuthSession, saveAuthSession } from '../../auth/session'
import { emptyTraceGraph } from '../../test/traceFixtures'
import { mockResourceNotices } from '../../test/resourceNotices'
import { WorkspaceScreen } from './WorkspaceScreen'

const user = { user_id: 7, username: 'requests', display_name: '请求验证', avatar_url: null, roles: [], disabled: false }
const time = '2030-01-01T00:00:00Z'
const historyItem = (overrides: Partial<ConversationHistoryListItem>): ConversationHistoryListItem => ({projectId: 'project-1', archived: false,
  id: 1, threadId: 'thread', title: '标题', titleSource: 'generated', titleGenerationStatus: 'succeeded', titleSeq: 1,
  accessMode: 'full', status: 'idle', lastRunId: 'run', lastModel: 'main', messageCount: 1, toolCallCount: 0,
  hasPendingInterrupt: false, pendingInteractionKind: null, pinned: false, createdAt: time, updatedAt: time,
  ...overrides,
})
const traceDetail = (overrides: Partial<ConversationHistoryDetail>): ConversationHistoryDetail => ({projectId: 'project-1', archived: false,
  id: 1, threadId: 'thread', title: '标题', titleSource: 'generated', titleGenerationStatus: 'succeeded', titleSeq: 1,
  accessMode: 'full', lastModel: 'main', pinned: false, createdAt: time, updatedAt: time,
  asOfSeq: 3, generation: 'generation', observedAt: time, headRunId: 'run', availableHeads: ['run'], historyCursor: null,
  messageCount: 1, toolCallCount: 0, messages: [{
    agui: null, id: 'answer', sourceId: 'answer', traceSeq: 2, graphNamespace: [], runId: 'run', role: 'assistant',
    content: '来自 Trace 的历史回复', contentOmitted: false, status: 'completed', createdAt: time, completedAt: time,
  }],
  reasoning: [], runFailures: [], state: { root: {}, subgraphs: {} }, interactionAvailability: [], interactions: [],
  status: { execution: 'succeeded', headRunId: 'run' }, completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  graph: emptyTraceGraph(3), taskTrace: { status: 'ready', todoGroups: [] }, ...overrides,
})
const jsonResponse = (data: unknown) => new Response(JSON.stringify({ code: 0, message: 'success', data }), {
  headers: { 'Content-Type': 'application/json' },
})
function installFetch({ list, details }: { list: ConversationHistoryListItem[]; details: Record<string, ConversationHistoryDetail> }) {
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init)
    const path = new URL(request.url).pathname
    if (path === '/api/projects') return jsonResponse([{ id: 'project-1', name: '测试项目', createdAt: time, updatedAt: time }])
    if (path === '/api/models') return jsonResponse({ items: [{ modelId: 'main', displayName: 'Main', connectionId: 'provider', connectionDisplayName: '模型', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' })
    if (path === '/api/skills/installations') return jsonResponse([])
    if (path === '/api/conversation/config') return jsonResponse({ dayRanges: [7, 30] })
    if (path === '/api/conversation/history') return jsonResponse({ items: list, nextCursor: null })
    const detail = details[path.split('/')[3]!]
    if (path.endsWith('/history') && detail) return jsonResponse(detail)
    throw new Error(`未预期的接口：${path}`)
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}
beforeEach(() => {
  saveAuthSession({ token: 'requests-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user })
  window.history.replaceState({}, '', '/')
  const matchMedia = window.matchMedia
  vi.stubGlobal('matchMedia', (query: string) => ({ ...matchMedia(query), matches: query.includes('prefers-reduced-motion') ? query === '(prefers-reduced-motion: reduce)' : matchMedia(query).matches }))
})
afterEach(() => {
  cleanup()
  clearAuthSession()
  window.sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

it.each(['succeeded', 'running'] as const)('四十个列表会话在重同步、校准和恢复前台时共享标题读取：%s', async titleGenerationStatus => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2030-01-01T00:00:00Z'))
  const notices = mockResourceNotices()
  const visibility = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  const list = Array.from({ length: 40 }, (_, index) => historyItem({
    id: index + 1, threadId: `listed-${index}`, title: `列表会话${index}`,
    titleSource: titleGenerationStatus === 'succeeded' ? 'generated' : 'default',
    titleGenerationStatus, titleSeq: 1,
  }))
  const first = list[0]!
  const fetch = installFetch({ list, details: { [first.threadId]: traceDetail({
    threadId: first.threadId, title: first.title, titleSource: first.titleSource,
    titleGenerationStatus, titleSeq: 1,
  }) } })
  const original = fetch.getMockImplementation()!
  fetch.mockImplementation(async (input, init) => {
    const path = new URL(input instanceof Request ? input.url : String(input)).pathname
    if (path === '/api/notifications') return new Response(new ReadableStream(), { headers: { 'Content-Type': 'text/event-stream' } })
    if (path.endsWith('/title')) return jsonResponse(list.find(item => path.includes(`/${item.threadId}/`)))
    return original(input, init)
  })
  const paths = () => fetch.mock.calls.map(([input]) => new URL(input instanceof Request ? input.url : String(input)).pathname)
  const titleReads = () => paths().filter(path => path.endsWith('/title'))
  const listReads = () => paths().filter(path => path === '/api/conversation/history').length
  const view = render(<WorkspaceScreen user={user} onLogout={vi.fn()} onToast={vi.fn()} />)
  try {
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getAllByRole('button', { name: /^打开会话：列表会话/ })).toHaveLength(40)
    expect(titleReads()).toEqual([])
    let reads = listReads()
    await act(async () => { notices.resync(); await vi.advanceTimersByTimeAsync(0) })
    expect(listReads()).toBe(++reads)
    expect(titleReads()).toEqual([])
    await act(async () => vi.advanceTimersByTimeAsync(30_000))
    expect(listReads()).toBe(++reads)
    expect(titleReads()).toEqual([])
    act(() => { visibility.mockReturnValue(true); document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => vi.advanceTimersByTimeAsync(60_000))
    expect(listReads()).toBe(reads)
    await act(async () => { visibility.mockReturnValue(false); document.dispatchEvent(new Event('visibilitychange')); await vi.advanceTimersByTimeAsync(0) })
    expect(listReads()).toBe(++reads)
    expect(titleReads()).toEqual([])
    list[1] = { ...list[1]!, title: '后台完成的标题', titleSource: 'generated', titleGenerationStatus: 'succeeded', titleSeq: 2 }
    await act(async () => { notices.changed('studio.conversation.title.changed', list[1]!.threadId); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByRole('button', { name: '打开会话：后台完成的标题' })).toBeVisible()
    expect(titleReads()).toEqual([])
    expect(screen.getByText('来自 Trace 的历史回复')).toBeVisible()
  } finally {
    view.unmount()
    await act(async () => vi.advanceTimersByTimeAsync(0))
    vi.restoreAllMocks()
  }
})

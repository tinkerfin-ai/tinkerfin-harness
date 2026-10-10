import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { clearAuthSession, saveAuthSession } from '../../auth/session'
import { mockNativePopover } from '../../test/nativePopover'
import { emptyTraceGraph } from '../../test/traceFixtures'
import { WorkspaceScreen } from './WorkspaceScreen'

const user = { user_id: 7, username: 'review', avatar_url: null, roles: [], disabled: false }
const time = '2030-01-01T00:00:00Z'
const projects = [{ id: 'first', name: '研究', createdAt: time, updatedAt: time }, { id: 'second', name: '开发', createdAt: time, updatedAt: time }]
const jsonResponse = (data: unknown) => new Response(JSON.stringify({ code: 0, message: 'success', data }), { headers: { 'Content-Type': 'application/json' } })
let restorePopover: () => void
beforeEach(() => {
  restorePopover = mockNativePopover()
  vi.useFakeTimers()
  vi.setSystemTime(new Date(time))
  saveAuthSession({ token: 'review-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user })
  window.history.replaceState({}, '', '/?project=first&thread=thread')
  const matchMedia = window.matchMedia
  vi.stubGlobal('matchMedia', (query: string) => ({ ...matchMedia(query), matches: query.includes('prefers-reduced-motion') ? query === '(prefers-reduced-motion: reduce)' : matchMedia(query).matches }))
})
afterEach(() => { cleanup(); clearAuthSession(); sessionStorage.clear(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers(); restorePopover() })

it.each([false, true])('会话移动在导航前后提交都保留未发送文本：后退发生在提交前=%s', async deferredMove => {
  let completePatch: (() => void) | undefined
  const record = { id: 1, projectId: 'first', archived: false, threadId: 'thread', title: '研究对话', titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 1, accessMode: 'full', status: 'idle', lastRunId: 'run', lastModel: 'main', messageCount: 1, toolCallCount: 0, hasPendingInterrupt: false, pendingInteractionKind: null, pinned: false, createdAt: time, updatedAt: time }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init)
    const url = new URL(request.url), path = url.pathname
    if (path === '/api/projects') return jsonResponse(projects)
    if (path === '/api/notifications') return new Response(new ReadableStream({ start(reader) { reader.enqueue(new TextEncoder().encode('event: ready\ndata: {}\n\n')) } }), { headers: { 'Content-Type': 'text/event-stream' } })
    if (path === '/api/models') return jsonResponse({ items: [{ modelId: 'main', displayName: 'Main', connectionId: 'provider', connectionDisplayName: '模型', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' })
    if (path === '/api/skills/installations') return jsonResponse([])
    if (path === '/api/conversation/config') return jsonResponse({ dayRanges: [7, 30] })
    if (path === '/api/conversation/history') return jsonResponse({ items: record.projectId === url.searchParams.get('projectId') ? [record] : [], nextCursor: null })
    if (path === '/api/conversation/thread' && request.method === 'PATCH') {
      const body = await request.json()
      if (deferredMove) return new Promise<Response>(resolve => { completePatch = () => { Object.assign(record, body); resolve(jsonResponse(record)) } })
      Object.assign(record, body); return jsonResponse(record)
    }
    if (path === '/api/conversation/thread/history') return jsonResponse({
      ...record, asOfSeq: 3, generation: 'generation', observedAt: time, headRunId: 'run', availableHeads: ['run'], historyCursor: null,
      messages: [{ agui: null, id: 'answer', sourceId: 'answer', traceSeq: 2, graphNamespace: [], runId: 'run', role: 'assistant', content: '对话正文', contentOmitted: false, status: 'completed', createdAt: time, completedAt: time }],
      reasoning: [], runFailures: [], state: { root: {}, subgraphs: {} }, submissionResult: null, planResults: [], interactionAvailability: [], interactions: [], status: { execution: 'succeeded', headRunId: 'run' },
      completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false }, graph: emptyTraceGraph(3), taskTrace: { status: 'ready', todoGroups: [] },
    })
    throw new Error(`未预期的接口：${path}`)
  }))
  render(<WorkspaceScreen user={user} onLogout={vi.fn()} onToast={vi.fn()} />)
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(screen.getByText('对话正文')).toBeVisible()
  fireEvent.change(screen.getByRole('textbox', { name: '消息输入' }), { target: { value: '移动后还要继续写的草稿' } })
  fireEvent.click(screen.getByRole('button', { name: '管理会话：研究对话' }))
  fireEvent.click(screen.getByRole('button', { name: '移动到项目' }))
  fireEvent.click(within(screen.getByRole('dialog', { name: '移动到项目' })).getByRole('button', { name: '移动' }))
  await act(async () => vi.advanceTimersByTimeAsync(0))
  if (deferredMove) {
    expect(completePatch).toBeTypeOf('function')
    act(() => { window.history.replaceState({}, '', '/?project=second'); window.dispatchEvent(new PopStateEvent('popstate')) })
    await act(async () => vi.advanceTimersByTimeAsync(0))
    await act(async () => { completePatch?.(); await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); await vi.advanceTimersByTimeAsync(0) })
    fireEvent.click(screen.getByRole('button', { name: '打开会话：研究对话' }))
  } else {
    fireEvent.click(screen.getByRole('button', { name: '切换项目：研究' }))
    fireEvent.click(screen.getByRole('button', { name: '开发' }))
  }
  expect(record.projectId).toBe('second')
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(screen.getByText('对话正文')).toBeVisible()
  expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('移动后还要继续写的草稿')
})

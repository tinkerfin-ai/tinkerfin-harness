import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ConversationHistoryDetail, ConversationHistoryListItem } from '../../api/conversation/history'
import { clearAuthSession, saveAuthSession } from '../../auth/session'
import { emptyTraceGraph } from '../../test/traceFixtures'
import { mockResourceNotices } from '../../test/resourceNotices'
import { runFixture } from '../../test/automationFixtures'
import { WorkspaceScreen } from './WorkspaceScreen'
import { ProjectsWorkspace } from '../projects/ProjectsWorkspace'
import type { Project } from '../projects/api'
import type { ModelSettings } from '../settings/useModelSettings'

const user = { user_id: 7, username: 'requests', avatar_url: null, roles: [], disabled: false }
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
  reasoning: [], runFailures: [], state: { root: {}, subgraphs: {} }, submissionResult: null, planResults: [], interactionAvailability: [], interactions: [],
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

it('每次进入新会话刷新模型和默认选择，保留正文并丢弃过期目录', async () => {
  const fetch = installFetch({ list: [], details: {} })
  const original = fetch.getMockImplementation()!
  const refreshes: { request: Request; resolve: (response: Response) => void }[] = []
  let catalogReads = 0
  fetch.mockImplementation((input, init) => {
    const request = input instanceof Request ? input : new Request(input, init)
    if (new URL(request.url).pathname === '/api/models' && catalogReads++ > 0) {
      return new Promise<Response>(resolve => refreshes.push({ request, resolve }))
    }
    return original(input, init)
  })
  render(<WorkspaceScreen user={user} onLogout={vi.fn()} onToast={vi.fn()} />)
  const input = await screen.findByRole('textbox', { name: '消息输入' })
  await waitFor(() => expect(input).toBeEnabled())
  fireEvent.change(input, { target: { value: '保留新会话正文', selectionStart: 7 } })
  const enterNew = () => fireEvent.click(screen.getAllByRole('button', { name: '新会话' })[0]!)
  enterNew()
  await waitFor(() => expect(refreshes).toHaveLength(1))
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  enterNew()
  await waitFor(() => expect(refreshes).toHaveLength(2))
  expect(refreshes[0]!.request.signal.aborted).toBe(true)
  const catalog = (id: string) => jsonResponse({ items: [{ modelId: id, displayName: id,
    connectionId: 'provider', connectionDisplayName: '模型', reasoningEnabled: false, isDefault: true }], defaultModelId: id })
  await act(async () => refreshes[1]!.resolve(catalog('Latest')))
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('Latest')
  expect(input).toHaveValue('保留新会话正文')
  expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  await act(async () => refreshes[0]!.resolve(catalog('Stale')))
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('Latest')
  expect(fetch.mock.calls.some(([request, init]) => new Request(request, init).method === 'POST')).toBe(false)
})

it('新会话目录刷新失败可重试，空目录不能提交且正文保留', async () => {
  const fetch = installFetch({ list: [], details: {} })
  const original = fetch.getMockImplementation()!
  let catalogReads = 0
  fetch.mockImplementation(async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init)
    if (new URL(request.url).pathname === '/api/models') {
      catalogReads += 1
      if (catalogReads === 2) return new Response('', { status: 503 })
      if (catalogReads === 3) return jsonResponse({ items: [], defaultModelId: null })
    }
    return original(input, init)
  })
  render(<WorkspaceScreen user={user} onLogout={vi.fn()} onToast={vi.fn()} />)
  const input = await screen.findByRole('textbox', { name: '消息输入' })
  await waitFor(() => expect(input).toBeEnabled())
  fireEvent.change(input, { target: { value: '刷新期间保留正文', selectionStart: 8 } })
  fireEvent.click(screen.getAllByRole('button', { name: '新会话' })[0]!)
  fireEvent.click(await screen.findByRole('button', { name: '重新加载模型' }))
  await screen.findByText('未配置可用模型')
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  expect(input).toHaveValue('刷新期间保留正文')
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled())
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('Main')
  expect(input).toHaveValue('刷新期间保留正文')
})

it('新会话刷新返回时不能覆盖已切回的历史模型', async () => {
  const fetch = installFetch({ list: [historyItem({})], details: { thread: traceDetail({}) } })
  const original = fetch.getMockImplementation()!
  let catalogReads = 0
  let finishRefresh: (response: Response) => void = () => { throw new Error('目录请求尚未开始') }
  fetch.mockImplementation((input, init) => {
    const request = input instanceof Request ? input : new Request(input, init)
    if (new URL(request.url).pathname === '/api/models' && catalogReads++ > 0) {
      return new Promise<Response>(resolve => { finishRefresh = resolve })
    }
    return original(input, init)
  })
  render(<WorkspaceScreen user={user} onLogout={vi.fn()} onToast={vi.fn()} />)
  await screen.findByText('来自 Trace 的历史回复')
  fireEvent.click(screen.getAllByRole('button', { name: '新会话' })[0]!)
  await waitFor(() => expect(catalogReads).toBe(2))
  fireEvent.click(screen.getByRole('button', { name: '打开会话：标题' }))
  await screen.findByText('来自 Trace 的历史回复')
  await act(async () => finishRefresh(jsonResponse({ items: ['main', 'new-default'].map(id => ({
    modelId: id, displayName: id === 'main' ? 'Main' : 'New Default', connectionId: 'provider',
    connectionDisplayName: '模型', reasoningEnabled: false, isDefault: id === 'new-default',
  })), defaultModelId: 'new-default' })))
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('Main')
})

it('恢复已停用模型的历史时保留草稿并要求主动选择可用模型', async () => {
  const list = [historyItem({ lastModel: 'unavailable-model' })]
  const fetch = installFetch({ list, details: { thread: traceDetail({ lastModel: 'unavailable-model' }) } })
  render(<WorkspaceScreen user={user} onLogout={vi.fn()} onToast={vi.fn()} />)
  await screen.findByText('来自 Trace 的历史回复')
  const input = screen.getByRole('textbox', { name: '消息输入' })
  expect(input).toBeEnabled()
  fireEvent.change(input, { target: { value: '保留正文再选模型', selectionStart: 8 } })
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('请选择可用模型')
  expect(screen.getByRole('status')).toHaveTextContent('请选择可用模型')
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(fetch.mock.calls.some(([request, init]) => new Request(request, init).method === 'POST')).toBe(false)
  expect(input).toHaveValue('保留正文再选模型')
  fireEvent.click(screen.getByRole('button', { name: '选择模型' }))
  fireEvent.click(within(screen.getByRole('listbox', { name: '模型选项' })).getByRole('option', { name: /Main/ }))
  expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('Main')
  expect(input).toHaveValue('保留正文再选模型')
})

it('初始失败按原模型判断重试资格，改选有效模型只能发起新请求', async () => {
  let models: ModelSettings[] = ['main', 'available'].map((id, index) => ({
    model_id: id, display_name: id === 'main' ? 'Main' : 'Available', connection_id: 'provider',
    model_name: id, reasoning_enabled: false, image_support: 'unknown', enabled: true,
    is_default: index === 1, sort_order: index, image_input_capability: null,
    chat_options: { max_tokens: null, temperature: null, top_p: null, stop: null,
      reasoning_effort: null, context_window: null, keep_alive: null },
  }))
  const submissions: { runId: string; forwardedProps: { model: string }; messages: { content: string }[] }[] = []
  const fetch = installFetch({ list: [], details: {} })
  const original = fetch.getMockImplementation()!
  fetch.mockImplementation(async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init)
    const path = new URL(request.url).pathname
    if (path === '/api/models') return jsonResponse({
      items: models.filter(model => model.enabled).map(model => ({ modelId: model.model_id,
        displayName: model.display_name, connectionId: 'provider', connectionDisplayName: '提供方',
        reasoningEnabled: false, isDefault: model.is_default })), defaultModelId: 'available',
    })
    if (path === '/api/models/settings') return jsonResponse({ models, providers: [], connections: [{
      connection_id: 'provider', provider_id: 'custom', display_name: '提供方', api_type: 'openai_chat_completions',
      base_url: 'https://example.invalid', auth_type: 'api_key', has_key: true,
    }] })
    if (path === '/api/models/configurations/main' && request.method === 'PUT') {
      models = models.map(model => model.model_id === 'main' ? { ...model, enabled: false } : model)
      return jsonResponse(null)
    }
    if (path === '/api/conversation/chat' && request.method === 'POST') {
      submissions.push(await request.json())
      return new Response(JSON.stringify({ code: 1001005001, message: '模型已停用', data: null }), {
        status: 409, headers: { 'Content-Type': 'application/json' },
      })
    }
    return original(input, init)
  })
  render(<WorkspaceScreen user={user} onLogout={vi.fn()} onToast={vi.fn()} />)
  const input = await screen.findByRole('textbox', { name: '消息输入' })
  await waitFor(() => expect(input).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '选择模型' }))
  fireEvent.click(screen.getByRole('option', { name: 'Main' }))
  fireEvent.change(input, { target: { value: '保留原始正文', selectionStart: 6 } })
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  await screen.findByRole('alert')
  expect(submissions).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  await waitFor(() => expect(submissions).toHaveLength(2))
  await screen.findByRole('alert')
  expect(submissions[1]!.runId).toBe(submissions[0]!.runId)
  fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
  fireEvent.click(screen.getByRole('menuitem', { name: '设置' }))
  fireEvent.click(screen.getByRole('button', { name: '模型配置' }))
  const toggle = await screen.findByRole('switch', { name: '启用模型 Main' })
  await act(async () => { fireEvent.click(toggle) })
  fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('请选择可用模型'))
  expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '选择模型' }))
  fireEvent.click(screen.getByRole('option', { name: /Available/ }))
  expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  expect(input).toHaveValue('保留原始正文')
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  await waitFor(() => expect(submissions).toHaveLength(3))
  expect(submissions[2]!.forwardedProps.model).toBe('available')
  expect(submissions[2]!.runId).not.toBe(submissions[0]!.runId)
  expect(submissions[2]!.messages[0]!.content).toBe('保留原始正文')
})

it.each(['停用模型', '切换模型', '切换会话后返回', '编辑草稿'] as const)('历史重试准备期间%s后只提交仍有效的请求并保留新选择和草稿', async change => {
  let models: ModelSettings[] = ['main', 'available'].map((id, index) => ({
    model_id: id, display_name: id === 'main' ? 'Main' : 'Available', connection_id: 'provider',
    model_name: id, reasoning_enabled: false, image_support: 'unknown', enabled: true,
    is_default: index === 1, sort_order: index, image_input_capability: null,
    chat_options: { max_tokens: null, temperature: null, top_p: null, stop: null,
      reasoning_effort: null, context_window: null, keep_alive: null },
  }))
  const failure = traceDetail({
    messages: [{
      agui: null, id: 'question', sourceId: 'question', traceSeq: 1, graphNamespace: [], runId: 'run', role: 'user',
      content: '原历史提问', contentOmitted: false, status: 'completed', createdAt: time, completedAt: time,
    }],
    runFailures: [{ runId: 'run', errorCode: 'runtime_initialization_error', retryable: true, failedAt: time }],
    status: { execution: 'failed', headRunId: 'run' },
  })
  const fetch = installFetch({
    list: [historyItem({}), historyItem({ id: 2, threadId: 'other', title: '另一会话' })],
    details: { thread: failure, other: traceDetail({ id: 2, threadId: 'other', title: '另一会话' }) },
  })
  const original = fetch.getMockImplementation()!
  const selections: { request: Request; resolve: (response: Response) => void }[] = []
  const submissions: { threadId: string; forwardedProps: { model: string }; messages: { content: string }[] }[] = []
  fetch.mockImplementation(async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init)
    const path = new URL(request.url).pathname
    if (path === '/api/models') return jsonResponse({
      items: models.filter(model => model.enabled).map(model => ({ modelId: model.model_id,
        displayName: model.display_name, connectionId: 'provider', connectionDisplayName: '提供方',
        reasoningEnabled: false, isDefault: model.is_default })), defaultModelId: 'available',
    })
    if (path === '/api/models/settings') return jsonResponse({ models, providers: [], connections: [{
      connection_id: 'provider', provider_id: 'custom', display_name: '提供方', api_type: 'openai_chat_completions',
      base_url: 'https://example.invalid', auth_type: 'api_key', has_key: true,
    }] })
    if (path === '/api/models/configurations/main' && request.method === 'PUT') {
      models = models.map(model => model.model_id === 'main' ? { ...model, enabled: false } : model)
      return jsonResponse(null)
    }
    if (path === '/api/skills/selection') {
      return new Promise<Response>(resolve => selections.push({ request, resolve }))
    }
    if (path === '/api/conversation/chat' && request.method === 'POST') {
      submissions.push(await request.json())
      return new Response('', { status: 409 })
    }
    return original(input, init)
  })
  render(<WorkspaceScreen user={user} onLogout={vi.fn()} onToast={vi.fn()} />)
  await screen.findByText('原历史提问')
  const input = screen.getByRole('textbox', { name: '消息输入' })
  fireEvent.change(input, { target: { value: '准备期间的新草稿', selectionStart: 8 } })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重试' })) })
  expect(selections).toHaveLength(1)
  expect(new URL(selections[0]!.request.url).searchParams.get('thread_id')).toBe('thread')
  expect(new URL(selections[0]!.request.url).searchParams.get('run_id')).toBe('run')
  expect(submissions).toHaveLength(0)

  if (change === '停用模型') {
    fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    fireEvent.click(screen.getByRole('menuitem', { name: '设置' }))
    fireEvent.click(screen.getByRole('button', { name: '模型配置' }))
    const toggle = await screen.findByRole('switch', { name: '启用模型 Main' })
    await act(async () => { fireEvent.click(toggle) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '关闭对话框' })) })
    expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('请选择可用模型')
    expect(screen.getByRole('button', { name: '重试' })).toBeDisabled()
  } else if (change === '切换模型') {
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }))
    fireEvent.click(screen.getByRole('option', { name: /Available/ }))
  } else if (change === '切换会话后返回') {
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '打开会话：另一会话' })) })
    fireEvent.change(screen.getByRole('textbox', { name: '消息输入' }), { target: { value: '另一会话的草稿', selectionStart: 7 } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '打开会话：标题' })) })
    expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('准备期间的新草稿')
  } else {
    fireEvent.change(input, { target: { value: '继续编辑后的草稿', selectionStart: 8 } })
  }

  expect(selections[0]!.request.signal.aborted).toBe(change !== '编辑草稿')
  await act(async () => selections[0]!.resolve(jsonResponse([])))
  if (change === '编辑草稿') {
    expect(submissions).toMatchObject([{ threadId: 'thread', forwardedProps: { model: 'main' },
      messages: [{ content: '原历史提问' }] }])
    expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('继续编辑后的草稿')
    return
  }
  expect(submissions).toHaveLength(0)
  expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('准备期间的新草稿')
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent(
    change === '停用模型' ? '请选择可用模型' : change === '切换模型' ? 'Available' : 'Main',
  )

  if (change === '停用模型') {
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }))
    fireEvent.click(screen.getByRole('option', { name: /Available/ }))
  }
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送消息' })) })
  expect(submissions).toMatchObject([{ threadId: 'thread', forwardedProps: { model: change === '切换会话后返回' ? 'main' : 'available' },
    messages: [{ content: '准备期间的新草稿' }] }])
  if (change === '切换会话后返回') {
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '打开会话：另一会话' })) })
    expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('另一会话的草稿')
  }
})

it.each(['succeeded', 'running'] as const)('四十个列表会话空闲不查询，重同步和恢复前台共享标题读取：%s', async titleGenerationStatus => {
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
    if (path === '/api/notifications') return new Response(new ReadableStream({ start(reader) { reader.enqueue(new TextEncoder().encode('event: ready\ndata: {}\n\n')) } }), { headers: { 'Content-Type': 'text/event-stream' } })
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
    await act(async () => vi.advanceTimersByTimeAsync(90_000))
    expect(listReads()).toBe(reads)
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


it('空项目工作区也接收其他窗口创建项目的通知，始终只保留一个订阅', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date(time))
  let projects: Project[] = []
  const readers = new Set<ReadableStreamDefaultController<Uint8Array>>()
  let projectReads = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init)
    const path = new URL(request.url).pathname
    if (path === '/api/projects') { projectReads += 1; return jsonResponse(projects) }
    if (path === '/api/notifications') {
      let reader: ReadableStreamDefaultController<Uint8Array>
      return new Response(new ReadableStream({
        start(value) { reader = value; readers.add(value); value.enqueue(new TextEncoder().encode('event: ready\ndata: {}\n\n')) },
        cancel() { readers.delete(reader) },
      }), { headers: { 'Content-Type': 'text/event-stream' } })
    }
    throw new Error(`未预期的接口：${path}`)
  }))
  const view = render(<ProjectsWorkspace user={user} onLogout={vi.fn()}>{scope => <p role="status">{scope.project.name}</p>}</ProjectsWorkspace>)
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(screen.getByRole('heading', { name: '从一个项目开始' })).toBeVisible()
  expect(readers.size).toBe(1)
  expect(projectReads).toBe(1)
  projects = [{ id: 'remote', name: '另一个窗口创建的项目', createdAt: time, updatedAt: time }]
  await act(async () => {
    const data = { scope: { namespace: 'ns_7', owner_id: null }, topic: 'studio.projects.changed', key: 'remote', details: {} }
    for (const reader of readers) reader.enqueue(new TextEncoder().encode(`event: change\ndata: ${JSON.stringify(data)}\n\n`))
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(screen.getByRole('status')).toHaveTextContent('另一个窗口创建的项目')
  expect(readers.size).toBe(1)
  const afterChange = projectReads
  await act(async () => vi.advanceTimersByTimeAsync(90_000))
  expect(projectReads).toBe(afterChange)
  view.unmount()
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(readers.size).toBe(0)
})

it('运行详情打开尚未在目录中的会话', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date(time))
  window.history.replaceState({}, '', '/?project=project-1&page=automation')
  const list = [historyItem({ threadId: 'old', title: '已有会话' })]
  const run = runFixture({ name: '任务', conversation: { state: 'available', projectId: 'project-1', threadId: 'fresh' },
    updatedAt: time, queuedAt: time, startedAt: time, finishedAt: time })
  const toast = vi.fn()
  const fetch = installFetch({ list, details: {
    old: traceDetail({ threadId: 'old', title: '已有会话' }),
    fresh: traceDetail({ threadId: 'fresh', title: '自动化新会话', messages: [{ ...traceDetail({}).messages[0]!, id: 'fresh-message', content: '自动化新会话正文' }] }),
  } })
  const original = fetch.getMockImplementation()!
  fetch.mockImplementation(async (input, init) => {
    const path = new URL(input instanceof Request ? input.url : String(input)).pathname
    if (path === '/api/notifications') return new Response(new ReadableStream({ start(value) {
      value.enqueue(new TextEncoder().encode('event: ready\ndata: {}\n\n'))
    } }), { headers: { 'Content-Type': 'text/event-stream' } })
    if (path === '/api/automation/tasks') return jsonResponse({ items: [], nextCursor: null })
    if (path === '/api/automation/runs/active') return jsonResponse({ items: [], nextCursor: null })
    if (path === '/api/automation/runs/calendar') return jsonResponse({ days: [{ date: run.date, items: [run], nextCursor: null }] })
    if (path === '/api/automation/runs/run') return jsonResponse({ ...run, threadId: 'fresh', runId: 'run', messages: [], outputFiles: [], resultAvailable: true })
    if (path === '/api/automation/runs/run/status') return jsonResponse(run)
    return original(input, init)
  })
  render(<WorkspaceScreen user={user} onLogout={vi.fn()} onToast={toast} />)
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(screen.getByRole('button', { name: '打开会话：已有会话' })).toBeVisible()
  expect(screen.queryByRole('button', { name: '打开会话：自动化新会话' })).not.toBeInTheDocument()
  await act(async () => {
    fireEvent.click(screen.getByRole('tab', { name: '历史' }))
    await vi.advanceTimersByTimeAsync(0)
  })
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /^查看运行：任务，/ }))
    await vi.advanceTimersByTimeAsync(0)
  })
  const dialog = screen.getByRole('dialog', { name: '任务' })
  expect(within(dialog).getByRole('button', { name: '查看关联会话' })).toBeEnabled()
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: '查看关联会话' }))
    await vi.advanceTimersByTimeAsync(0)
  })
  const newHistoryReads = fetch.mock.calls.filter(([input]) => new URL(input instanceof Request ? input.url : String(input)).pathname === '/api/conversation/fresh/history')
  expect(newHistoryReads.length).toBeGreaterThan(0)
  expect(window.location.search).toBe('?project=project-1&thread=fresh')
  expect(screen.getByText('自动化新会话正文')).toBeVisible()
})

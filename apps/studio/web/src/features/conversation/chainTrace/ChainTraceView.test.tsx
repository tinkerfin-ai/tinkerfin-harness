import type { ComponentProps } from 'react'
import { useDrawerLayout } from '../../../components/ui/useDrawerLayout'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  TraceGraphFilter,
  TraceGraphNode,
  TraceGraphPage,
} from '../../../api/conversation/traceGraph'
import { LocaleProvider } from '../../../i18n'
import { ChainTraceView as TraceView } from './ChainTraceView'

function ChainTraceView(props: Omit<ComponentProps<typeof TraceView>, 'drawerLayout'>) {
  const drawerLayout = useDrawerLayout(520)
  return <TraceView {...props} drawerLayout={drawerLayout} />
}

const useChainTrace = vi.hoisted(() => vi.fn())
const queryTraceGraph = vi.hoisted(() => vi.fn())
vi.mock('./useChainTrace', () => ({ useChainTrace }))
vi.mock('../../../api/conversation/traceGraph', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../../api/conversation/traceGraph')>(),
  queryTraceGraph,
}))

const BASE_TIME = Date.parse('2026-09-04T00:00:00.000Z')

const node = (
  id: string,
  kind: TraceGraphNode['kind'],
  startedSeq: number,
  values: Partial<TraceGraphNode> = {},
): TraceGraphNode => ({
  agui: null,
  id,
  turnId: startedSeq < 10 ? 'turn-1' : 'turn-2',
  parentSubagentId: null,
  modelCallId: null,
  kind,
  status: 'succeeded',
  name: kind === 'human_message'
    ? 'HumanMessage'
    : kind === 'assistant_message' ? 'AssistantMessage' : id,
  runId: startedSeq < 10 ? 'run-1' : 'run-2',
  graphNamespace: [],
  startedAt: new Date(BASE_TIME + startedSeq * 100).toISOString(),
  completedAt: new Date(BASE_TIME + startedSeq * 100 + 50).toISOString(),
  startedSeq,
  updatedSeq: startedSeq,
  contentOmitted: false,
  toolCallOnly: false,
  requestOmitted: false,
  resultOmitted: false,
  linkIssues: [],
  ...values,
})

const nodes: TraceGraphNode[] = [
  node('human-1', 'human_message', 1, { content: '第一轮问题' }),
  node('context-1', 'custom', 2, { name: '检索上下文', result: '第一轮上下文' }),
  node('human-2', 'human_message', 10, { content: '第二轮问题' }),
  node('context-2', 'context', 11, {
    content: [{ type: 'text', text: '# 最终系统提示词' }],
  }),
  node('model-2', 'model', 12, {
    name: 'deepseek-chat',
    provider: 'deepseek',
    model: 'deepseek-chat',
    firstOutputAt: new Date(BASE_TIME + 1220).toISOString(),
    request: {
      messages: [{
        messageType: 'system',
        content: [{ type: 'text', text: '# 最终系统提示词' }],
      }],
    },
    usage: {
      input_tokens: 4872,
      output_tokens: 185,
      total_tokens: 5057,
    },
    responseMetadata: { finish_reason: 'stop' },
  }),
  node('assistant-2', 'assistant_message', 13, {
    modelCallId: 'model-2',
    content: '完成',
  }),
  node('failed-tool', 'tool', 14, {
    modelCallId: 'model-2',
    name: 'search',
    status: 'failed',
    failure: {
      errorType: 'builtins.TimeoutError',
      message: '搜索服务超时',
    },
  }),
  node('subagent-2', 'subagent', 15, {
    modelCallId: 'model-2',
    name: 'researcher',
    graphNamespace: ['tools:child'],
    request: {
      description: '核验子任务',
      subagent_type: 'researcher',
    },
    sourceId: 'call-task',
  }),
  node('subagent-input', 'human_message', 16, {
    parentSubagentId: 'subagent-2',
    graphNamespace: ['tools:child'],
    content: '核验子任务',
  }),
  node('child-model', 'model', 17, {
    parentSubagentId: 'subagent-2',
    graphNamespace: ['tools:child'],
    name: 'deepseek-research',
  }),
  node('child-assistant', 'assistant_message', 18, {
    parentSubagentId: 'subagent-2',
    modelCallId: 'child-model',
    graphNamespace: ['tools:child'],
    content: '子任务完成',
  }),
]

const turns = [
  {
    id: 'turn-1',
    ordinal: 1,
    startedAt: nodes[0]!.startedAt,
  },
  {
    id: 'turn-2',
    ordinal: 2,
    startedAt: nodes[2]!.startedAt,
  },
]

const graphPage = (
  returnedNodes: TraceGraphNode[],
  matchedNodeIds = returnedNodes.map((item) => item.id),
): TraceGraphPage => ({
  turns: turns.filter((turn) => returnedNodes.some((item) => item.turnId === turn.id)),
  nodes: returnedNodes,
  orderedNodeIds: returnedNodes.map((item) => item.id),
  matchedNodeIds,
  nextCursor: null,
  asOfSeq: 30,
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
})

const filteredPage = (filter: TraceGraphFilter): TraceGraphPage => {
  const query = filter.query?.toLocaleLowerCase()
  const direct = nodes.filter((item) => (
    (!filter.kinds || filter.kinds.includes(item.kind))
    && (!query || JSON.stringify(item).toLocaleLowerCase().includes(query))
  ))
  const returnedIds = new Set(direct.map((item) => item.id))
  const byId = new Map(nodes.map((item) => [item.id, item]))
  direct.forEach((item) => {
    let ownerId = item.parentSubagentId
    while (ownerId) {
      returnedIds.add(ownerId)
      ownerId = byId.get(ownerId)?.parentSubagentId ?? null
    }
  })
  const returned = nodes.filter((item) => returnedIds.has(item.id))
  return graphPage(returned, direct.map((item) => item.id))
}

const row = (name: string | RegExp) => screen.getByRole('button', { name })

describe('ChainTraceView', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1200)
    useChainTrace.mockReset()
    queryTraceGraph.mockReset()
    window.localStorage.clear()
    useChainTrace.mockImplementation(({ filter }: { filter: TraceGraphFilter }) => ({
      state: { phase: 'ready', page: filteredPage(filter) },
      retry: vi.fn(),
    }))
  })

  it('压缩合并到上下文一行，详情页签各自展示输入和摘要', () => {
    const compactNodes = [
      node('compact', 'custom', 1, { name: 'context_compaction', contextKind: 'compaction', parentNodeId: 'input', request: { messages: [{ id: 'source', content: '原始选中消息' }] }, result: { status: 'not_reduced', generated_summary: '已生成的摘要文本', compacted_messages: 0 } }),
      node('input', 'context', 1, { content: '实际系统提示词', contextKind: 'compaction' }),
      node('summary-model', 'model', 3, { name: 'qwen3.8-max', parentNodeId: 'compact' }),
    ]
    useChainTrace.mockReturnValue({ state: { phase: 'ready', page: graphPage(compactNodes) }, retry: vi.fn() })
    render(<ChainTraceView threadId="thread-compact" active live={false} />)
    expect(row('上下文，压缩，已完成，查看详情')).toBeVisible()
    expect(screen.queryByRole('button', { name: '压缩，压缩上下文，已完成，查看详情' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '模型，qwen3.8-max，已完成，查看详情' })).not.toBeInTheDocument()
    const details = screen.getByRole('complementary', { name: '链路详情' })
    expect(within(details).queryByText('实际系统提示词')).not.toBeInTheDocument()
    expect(within(details).getByText('基本信息')).toBeVisible()
    expect(within(details).queryByText('上下文未缩短，已保留原内容')).not.toBeInTheDocument()
    expect(within(details).queryByText('生成的摘要')).not.toBeInTheDocument()
    expect(within(details).queryByText('原始选中历史')).not.toBeInTheDocument()
    expect(within(details).queryByText('摘要模型与用量')).not.toBeInTheDocument()
    fireEvent.click(within(details).getByRole('tab', { name: '压缩内容' }))
    expect(within(details).getByText('原始选中消息')).toBeVisible()
    expect(within(details).queryByText('已生成的摘要文本')).not.toBeInTheDocument()
    fireEvent.click(within(details).getByRole('tab', { name: '摘要' }))
    expect(within(details).getByText('已生成的摘要文本')).toBeVisible()
    expect(within(details).queryByText('原始选中历史')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /展开节点 input/ }))
    expect(row('模型，qwen3.8-max，已完成，查看详情')).toBeVisible()
  })

  it.each([
    [null, '正在整理上下文'],
    [{ status: 'generating' }, '正在整理上下文'],
    [{ status: 'saving', summary: '尚未采用的摘要' }, '正在保存压缩结果'],
  ] as const)('压缩概述只显示基本信息，不展示进度或输入内容 %j', (result, expected) => {
    const compactNodes = [
      node('input', 'context', 1, { contextKind: 'compaction', status: 'running', completedAt: null }),
      node('compact', 'custom', 1, { contextKind: 'compaction', parentNodeId: 'input', name: 'context_compaction', status: 'running', completedAt: null, request: { origin: 'manual', messages: [{ content: '只属于压缩内容页签' }] }, result }),
    ]
    useChainTrace.mockReturnValue({ state: { phase: 'ready', page: graphPage(compactNodes) }, retry: vi.fn() })
    render(<ChainTraceView threadId="compacting" active live />)
    const details = screen.getByRole('complementary', { name: '链路详情' })
    expect(within(details).queryByText(expected)).not.toBeInTheDocument()
    expect(within(details).getByText('基本信息')).toBeVisible()
    expect(within(details).getByText('运行中')).toBeVisible()
    expect(within(details).queryByText('本次模型请求没有最终系统提示词')).not.toBeInTheDocument()
    expect(within(details).queryByText(/只属于压缩内容页签|generating|origin|尚未采用的摘要/)).not.toBeInTheDocument()
  })

  it.each([true, false])('筛选仅命中摘要模型时，执行图仍显示所属上下文（移动端 %s）', (mobile) => {
    const compactNodes = [
      node('input', 'context', 1, { contextKind: 'compaction' }),
      node('compact', 'custom', 1, { name: 'context_compaction', contextKind: 'compaction', parentNodeId: 'input' }),
      node('summary-model', 'model', 3, { name: 'qwen3.8-max', parentNodeId: 'compact' }),
    ]
    useChainTrace.mockReturnValue({ state: { phase: 'ready', page: {
      ...graphPage(compactNodes), matchedNodeIds: ['summary-model'],
    } }, retry: vi.fn() })
    render(<ChainTraceView threadId="thread-compact" active live={false} mobile={mobile} />)
    const sequence = screen.getByRole('region', { name: mobile ? '执行序列' : '调用时间线' })
    expect(within(sequence).getByRole('button', { name: /选择 上下文/ })).toBeVisible()
  })

  it('移动端点击节点进入详情并聚焦返回入口，不要求展开窗口', async () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(320)
    const unavailable = vi.fn()
    render(<ChainTraceView threadId="thread-1" active live={false} mobile onDetailsUnavailable={unavailable} />)
    expect(screen.queryByRole('complementary', { name: '链路详情' })).not.toBeInTheDocument()
    const trigger = row('模型，deepseek-chat，已完成，查看详情')
    fireEvent.click(trigger)
    const back = screen.getByRole('button', { name: '返回链路' })
    expect(back).toHaveFocus()
    expect(screen.getByRole('complementary', { name: '链路详情' })).toBeVisible()
    expect(screen.queryByRole('separator')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: '请求' }))
    expect(screen.getByRole('tab', { name: '请求' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(back)
    expect(screen.queryByRole('complementary', { name: '链路详情' })).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(unavailable).not.toHaveBeenCalled()
  })

  it('宿主收窄保留选中详情与分类，主动关闭后不自动打开', () => {
    let width = 1200
    const callbacks: Array<() => void> = []
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => width)
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { callbacks.push(callback) }
      observe() {}
      disconnect() {}
    })
    const unavailable = vi.fn()
    render(<ChainTraceView threadId="thread-1" active live={false} onDetailsUnavailable={unavailable} />)
    fireEvent.click(row('模型，deepseek-chat，已完成，查看详情'))
    fireEvent.click(screen.getByRole('tab', { name: '请求' }))
    act(() => { width = 819; callbacks.forEach(callback => callback()) })
    expect(screen.queryByRole('complementary', { name: '链路详情' })).not.toBeInTheDocument()
    fireEvent.click(row('模型，deepseek-chat，已完成，查看详情'))
    expect(unavailable).toHaveBeenCalledOnce()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    act(() => { width = 1200; callbacks.forEach(callback => callback()) })
    expect(screen.getByRole('tab', { name: '请求' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(screen.getByRole('button', { name: '关闭链路详情' }))
    act(() => { width = 819; callbacks.forEach(callback => callback()) })
    act(() => { width = 1200; callbacks.forEach(callback => callback()) })
    expect(screen.queryByRole('complementary', { name: '链路详情' })).not.toBeInTheDocument()
  })

  it('renders the six product lanes and complete Model details', async () => {
    render(<ChainTraceView threadId="thread-1" active live={false} />)

    fireEvent.click(screen.getByRole('button', { name: '关闭链路详情' }))
    expect([...document.querySelectorAll('.chain-trace-lane-label')].map(
      (element) => element.textContent,
    )).toEqual(['用户', '上下文', '模型', '工具', '子智能体', '助手'])
    expect(screen.queryByRole('group', { name: '节点类型' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '执行序列' })).toBeVisible()
    expect(screen.getAllByRole('button', { name: /^选择 / })).toHaveLength(nodes.length)
    expect(document.querySelectorAll('.chain-trace-sequence-turn-boundary')).toHaveLength(1)
    expect(document.querySelector('.chain-trace-ticks')).not.toBeInTheDocument()
    const context = row(/^上下文，# 最终系统提示词/)
    expect(within(context).queryByText('Context', { exact: true })).not.toBeInTheDocument()
    fireEvent.click(context)
    const contextDetails = screen.getByRole('complementary', { name: '链路详情' })
    expect(within(contextDetails).getByRole('heading', { name: '最终系统提示词' }))
      .toBeVisible()
    expect(within(contextDetails).queryByRole('tab', { name: '系统提示词' }))
      .not.toBeInTheDocument()
    fireEvent.click(within(contextDetails).getByRole('button', { name: '关闭链路详情' }))
    const failedTool = row('工具，search，失败，查看详情')
    fireEvent.click(failedTool)
    expect(failedTool).toHaveAttribute('aria-current', 'true')

    const errorDetails = await screen.findByRole('complementary', { name: '链路详情' })
    expect(within(errorDetails).getByText('搜索服务超时')).toBeVisible()
    fireEvent.click(within(errorDetails).getByRole('button', { name: '关闭链路详情' }))
    await waitFor(() => expect(failedTool).toHaveFocus())

    fireEvent.click(row('模型，deepseek-chat，已完成，查看详情'))
    const modelDetails = screen.getByRole('complementary', { name: '链路详情' })
    fireEvent.click(within(modelDetails).getByRole('tab', { name: '响应' }))
    expect(within(modelDetails).getByText('完成')).toBeVisible()
    expect(within(modelDetails).getByText(/"name": "search"/)).toBeVisible()
    expect(within(modelDetails).getByText(/"name": "researcher"/)).toBeVisible()
    expect(within(modelDetails).getByText(/"input_tokens": 4872/)).toBeVisible()
    expect(within(modelDetails).getByText(/"finish_reason": "stop"/)).toBeVisible()
    expect(queryTraceGraph).not.toHaveBeenCalled()

    fireEvent.click(within(modelDetails).getByRole('tab', { name: '系统提示词' }))
    expect(within(modelDetails).getByRole('heading', { name: '最终系统提示词' }))
      .toBeVisible()
    fireEvent.click(within(modelDetails).getByRole('tab', { name: '用量' }))
    expect(within(modelDetails).getByText('4,872')).toBeVisible()
  })

  it('explains when a Context has no final SystemMessage', () => {
    const context = node('context-empty', 'context', 11, { content: null })
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: graphPage([context]) },
      retry: vi.fn(),
    })
    render(<ChainTraceView threadId="thread-empty-context" active live={false} />)

    fireEvent.click(row('上下文，模型输入准备，已完成，查看详情'))

    expect(screen.getByText('本次模型请求没有最终系统提示词')).toBeVisible()
    expect(screen.queryByRole('tab', { name: '系统提示词' })).not.toBeInTheDocument()
  })

  it('labels only assistants with concrete Tool output as Tool-call-only', () => {
    const toolOnlyNodes = [
      node('human-tool-only', 'human_message', 1, { content: '调用工具' }),
      node('model-tool-only', 'model', 2, { name: 'deepseek-chat' }),
      node('assistant-tool-only', 'assistant_message', 3, {
        modelCallId: 'model-tool-only',
        content: '',
        toolCallOnly: true,
      }),
      node('tool-only-result', 'tool', 4, {
        modelCallId: 'model-tool-only',
        name: 'read_file',
      }),
      node('assistant-unknown', 'assistant_message', 5, {
        modelCallId: 'model-without-tool',
        content: '',
      }),
    ]
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: graphPage(toolOnlyNodes) },
      retry: vi.fn(),
    })
    render(<ChainTraceView threadId="thread-tool-only" active live={false} />)

    const toolOnly = row('助手，（仅工具调用），已完成，查看详情')
    expect(within(toolOnly).getByText('（仅工具调用）')).toHaveClass('is-muted')
    expect(row('助手，不可用，已完成，查看详情')).toBeVisible()
    expect(screen.getByRole('button', { name: /^选择 助手，（仅工具调用），已完成/ })).toBeVisible()
    expect(screen.getByRole('button', { name: /^选择 助手，不可用，已完成/ })).toBeVisible()
  })

  it('消息摘要只展示可见文字或附件名称，不挤入文件元数据', () => {
    const attachment = { type: 'file', file_id: 'private-file-id', extras: { attachment: {
      id: 'report', name: '门店经营简报.pdf', mime_type: 'application/pdf', size_bytes: 20,
    } } }
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: graphPage([
        node('mixed', 'human_message', 1, { content: [{ type: 'text', text: '整理门店经营数据' }, attachment] }),
        node('attachment', 'human_message', 2, { content: [attachment] }),
        node('unnamed', 'human_message', 3, { content: [{ type: 'file', file_id: 'unnamed' }] }),
        node('omitted', 'assistant_message', 4, { content: null, contentOmitted: true }),
      ]) }, retry: vi.fn(),
    })
    render(<ChainTraceView threadId="thread-summary" active live={false} />)
    for (const summary of ['整理门店经营数据', '门店经营简报.pdf', '附件']) {
      expect(row(`用户，${summary}，已完成，查看详情`)).toBeVisible()
      expect(screen.getByRole('button', { name: new RegExp(`^选择 用户，${summary}，已完成`) })).toBeVisible()
    }
    expect(row('助手，不可用，已完成，查看详情')).toBeVisible()
    expect(screen.queryByText(/private-file-id|extras/)).not.toBeInTheDocument()
    fireEvent.click(row('用户，整理门店经营数据，已完成，查看详情'))
    expect(screen.getByText(/private-file-id/)).toBeVisible()
  })

  it('scopes the timeline summary to the selected Turn', () => {
    const { container } = render(<ChainTraceView threadId="thread-1" active live={false} />)
    const summary = container.querySelector('.chain-trace-range-summary')
    const lastTick = () => container.querySelector('.chain-trace-ticks span:last-child')

    fireEvent.click(row(/^用户，第一轮问题/))
    expect(summary).not.toHaveTextContent('当前范围')
    expect(summary).toHaveTextContent('第 1 轮')
    expect(summary).toHaveTextContent('2 节点')
    expect(lastTick()).toHaveTextContent('150 毫秒')

    fireEvent.click(row(/^助手，完成/))
    expect(summary).toHaveTextContent('第 2 轮')
    expect(summary).toHaveTextContent('9 节点')
    expect(lastTick()).toHaveTextContent('850 毫秒')
  })

  it('requests the complete Graph without a Studio kind filter', () => {
    render(<ChainTraceView threadId="thread-1" active live={false} />)

    expect(useChainTrace.mock.calls.at(-1)?.[0].filter).toEqual({ query: undefined })
    expect(screen.queryByRole('group', { name: '节点类型' })).not.toBeInTheDocument()
  })

  it('collapses and expands only one nested Subagent scope', () => {
    render(<ChainTraceView threadId="thread-1" active live={false} />)

    expect(row('用户，核验子任务，已完成，查看详情')).toBeVisible()
    expect(row('模型，deepseek-chat，已完成，查看详情')).toBeVisible()
    fireEvent.click(screen.getByRole('button', {
      name: '收起子智能体 researcher，第 2 轮步骤 6',
    }))
    expect(screen.queryByRole('button', { name: '用户，核验子任务，已完成，查看详情' }))
      .not.toBeInTheDocument()
    expect(row('用户，第二轮问题，已完成，查看详情')).toBeVisible()
    fireEvent.click(screen.getByRole('button', {
      name: '展开子智能体 researcher，第 2 轮步骤 6',
    }))
    expect(row('助手，子任务完成，已完成，查看详情')).toBeVisible()
  })

  it('queries hidden Model responses through modelCallId without a type filter', async () => {
    queryTraceGraph.mockResolvedValue(graphPage(
      nodes.filter((item) => item.modelCallId === 'model-2'),
    ))
    render(<ChainTraceView threadId="thread-1" active live={false} />)

    fireEvent.click(screen.getByRole('button', { name: '搜索链路节点' }))
    fireEvent.change(screen.getByRole('searchbox', { name: '搜索链路节点' }), {
      target: { value: 'deepseek-chat' },
    })
    await waitFor(() => expect(useChainTrace.mock.calls.at(-1)?.[0].filter.query)
      .toBe('deepseek-chat'))
    fireEvent.click(row('模型，deepseek-chat，已完成，查看详情'))
    const details = screen.getByRole('complementary', { name: '链路详情' })
    fireEvent.click(within(details).getByRole('tab', { name: '响应' }))

    expect(await within(details).findByText('完成')).toBeVisible()
    expect(queryTraceGraph).toHaveBeenCalledWith(
      'thread-1',
      {
        modelCallId: 'model-2',
      },
      expect.objectContaining({ limit: 1000, signal: expect.any(AbortSignal) }),
    )
  })

  it('opens one compact search control and clears it with Escape', async () => {
    render(<ChainTraceView threadId="thread-1" active live={false} />)

    const trigger = screen.getByRole('button', { name: '搜索链路节点' })
    expect(screen.queryByRole('searchbox', { name: '搜索链路节点' }))
      .not.toBeInTheDocument()
    fireEvent.click(trigger)
    const searchbox = screen.getByRole('searchbox', { name: '搜索链路节点' })
    expect(searchbox).toHaveAttribute('placeholder', '搜索节点、内容')
    fireEvent.change(searchbox, { target: { value: '模型' } })
    fireEvent.keyDown(searchbox, { key: 'Escape' })
    await waitFor(() => expect(
      screen.getByRole('button', { name: '搜索链路节点' }),
    ).toHaveFocus())
    expect(screen.queryByRole('searchbox', { name: '搜索链路节点' }))
      .not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '搜索链路节点' })).toHaveLength(1)
  })

  it('shows Trace actions only after Graph data is ready', () => {
    useChainTrace.mockReturnValue({
      state: { phase: 'loading' },
      retry: vi.fn(),
    })
    const rendered = render(<ChainTraceView threadId="thread-1" active live={false} />)
    expect(screen.queryByLabelText('链路操作')).not.toBeInTheDocument()
    expect(screen.getByText('正在加载链路…')).toBeVisible()

    rendered.unmount()
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: graphPage([]) },
      retry: vi.fn(),
    })
    const empty = render(<ChainTraceView threadId="thread-1" active live={false} />)
    expect(screen.getByLabelText('链路操作')).toBeVisible()
    expect(screen.getByText('没有匹配的链路节点')).toBeVisible()

    empty.unmount()
    const retry = vi.fn()
    const onError = vi.fn()
    useChainTrace.mockReturnValue({ state: { phase: 'error' }, retry })
    const failed = render(<ChainTraceView threadId="thread-1" active live={false} onError={onError} />)
    expect(screen.queryByLabelText('链路操作')).not.toBeInTheDocument()
    expect(onError).toHaveBeenCalledExactlyOnceWith('链路加载失败')
    expect(screen.queryByText('链路加载失败')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重新加载' }))
    expect(retry).toHaveBeenCalledOnce()

    failed.unmount()
    useChainTrace.mockReturnValue({
      state: {
        phase: 'ready',
        page: { ...graphPage(nodes), nextCursor: 'older-page' },
      },
      retry: vi.fn(),
    })
    const onWarning = vi.fn()
    const incomplete = render(<ChainTraceView threadId="thread-1" active live={false} onWarning={onWarning} />)
    expect(onWarning).toHaveBeenCalledExactlyOnceWith(
      '链路超过完整视图上限，请使用搜索缩小范围',
    )
    incomplete.rerender(<ChainTraceView threadId="thread-1" active={false} live={false} onWarning={onWarning} />)
    incomplete.rerender(<ChainTraceView threadId="thread-1" active live={false} onWarning={onWarning} />)
    expect(onWarning).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('链路超过完整视图上限，请使用搜索缩小范围')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '调用时间线' }))
      .not.toBeInTheDocument()
  })

  it('renders the complete one-thousand-node boundary', () => {
    const boundaryNodes = Array.from({ length: 1000 }, (_, index) => node(
      `boundary-${index}`,
      index === 0 ? 'human_message' : 'assistant_message',
      index + 1,
      {
        turnId: 'turn-boundary',
        runId: 'run-boundary',
        content: `item-${index}`,
      },
    ))
    const boundaryPage: TraceGraphPage = {
      ...graphPage(boundaryNodes),
      turns: [{
        id: 'turn-boundary',
        ordinal: 1,
        startedAt: boundaryNodes[0]!.startedAt,
      }],
    }
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: boundaryPage },
      retry: vi.fn(),
    })

    const { container } = render(
      <ChainTraceView threadId="thread-boundary" active live={false} />,
    )

    expect(container.querySelectorAll('.chain-trace-ledger-row')).toHaveLength(1000)
    expect(useChainTrace.mock.calls.at(-1)?.[0].limit).toBe(1000)
  })

  it('uses the correct English Turn unit', () => {
    window.localStorage.setItem('tinkerfin:language', 'en')
    useChainTrace.mockReturnValue({
      state: { phase: 'ready', page: graphPage(nodes.slice(2)) },
      retry: vi.fn(),
    })
    render(
      <LocaleProvider>
        <ChainTraceView threadId="thread-1" active live={false} />
      </LocaleProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Close trace details' }))
    const summary = document.querySelector('.chain-trace-range-summary')
    expect(summary).toHaveTextContent('Total 1 turn')
    expect(summary?.querySelector('strong')).toHaveTextContent('1')
  })
})

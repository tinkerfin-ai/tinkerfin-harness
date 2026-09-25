import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { Message } from '../../../types'
import '../../../styles/tokens.css'
import '../../../styles/global.css'
import '../conversation.css'
import conversationStyles from '../conversation.css?raw'
import { MessageBlock, ToolCallBatch } from './MessageBlock'

const subagentMessage: Message = {
  id: 'subagent-run-researcher-1',
  role: 'subagent',
  content: '',
  createdAt: '2026-08-06T02:30:45.000Z',
  meta: {
    agentName: 'researcher',
    input: '访问两个 URL 并总结业务',
    result: '公司定位：中国最大的搜索引擎和 AI 科技公司。',
    reasoning: '这段内部思考不应出现在子智能体卡片中',
    status: 'completed',
    subRunId: 'sub-run-researcher-1',
    runId: 'sub-run-researcher-1',
    originMainRunId: 'main-run-1',
    lastMainRunId: 'main-run-1',
    durationMs: 4210,
  },
}

const childTool: Message = {
  id: 'tool-read-file-1',
  role: 'tool',
  content: '',
  createdAt: '2026-08-06T02:30:46.000Z',
  meta: {
    toolName: 'read_file',
    sourceAgentName: 'researcher',
    params: '{"file_path":"/research/url.json"}',
    result: 'https://www.baidu.com',
    status: 'completed',
    runId: 'sub-run-researcher-1',
    durationMs: 1210,
  },
}

const secondChildTool: Message = {
  ...childTool,
  id: 'tool-web-search-2',
  createdAt: '2026-08-06T02:30:47.000Z',
  meta: {
    ...childTool.meta,
    toolName: 'web_search',
    params: '{"query":"百度主营业务"}',
    result: '百度提供搜索与人工智能服务。',
  },
}

describe('MessageBlock subagent card', () => {
  it('工具数量从零变为正数时才显示计数，归零后隐藏', () => {
    const { rerender } = render(<MessageBlock message={subagentMessage} childTools={[]} />)
    expect(screen.queryByText('0 个工具')).not.toBeInTheDocument()
    rerender(<MessageBlock message={subagentMessage} childTools={[childTool, secondChildTool]} />)
    expect(screen.getByText('2 个工具')).toBeVisible()
    rerender(<MessageBlock message={subagentMessage} childTools={[]} />)
    expect(screen.queryByText(/\d+ 个工具/)).not.toBeInTheDocument()
  })

  it('keeps Tool and SubAgent summaries within touch target sizing', () => {
    expect(conversationStyles).toMatch(/@media \(any-hover:\s*none\), \(any-pointer:\s*coarse\)[\s\S]*\.subagent-card-head\s*{\s*height:\s*var\(--control-lg\)/s)
    expect(conversationStyles).toMatch(/@media \(any-hover:\s*none\), \(any-pointer:\s*coarse\)[\s\S]*\.tool-row > summary\s*{\s*height:\s*var\(--control-lg\)/s)
  })

  it('renders Task and SubAgent like a Tool row while keeping the Tool count trailing', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <MessageBlock message={subagentMessage} childTools={[childTool]} />,
    )

    const card = container.querySelector<HTMLDetailsElement>('.subagent-card')
    expect(card).not.toBeNull()
    expect(card?.open).toBe(false)
    const header = card?.querySelector('.subagent-card-head')
    expect(within(header as HTMLElement).getByText('Task')).toHaveClass('tool-row-title')
    expect(within(header as HTMLElement).getByText('SubAgent')).toHaveClass('tool-row-summary')
    expect(within(header as HTMLElement).getByText('1 个工具')).toBeVisible()
    expect(header?.querySelector('.subagent-card-chevron')).toBeNull()
    expect(header).not.toHaveTextContent('访问两个 URL 并总结业务')
    expect(card?.querySelector('.tool-row-state-dot')).toBeNull()
    expect(card?.querySelector('.subagent-visually-hidden')).toHaveTextContent('researcher，已完成')
    expect(screen.queryByText('这段内部思考不应出现在子智能体卡片中')).not.toBeInTheDocument()

    await user.click(card!.querySelector('.subagent-card-head')!)

    expect(card?.open).toBe(true)
    const task = container.querySelector('.subagent-task-line')
    expect(task).toHaveTextContent('researcher访问两个 URL 并总结业务')
    expect(task).not.toHaveTextContent('TASK')
    const trace = screen.getByRole('list', { name: 'researcher 工具轨迹' })
    expect(within(trace).getAllByRole('listitem')).toHaveLength(2)
    const output = container.querySelector('.subagent-output-node')
    expect(output).toHaveClass('is-completed')
    expect(within(output as HTMLElement).getByText('已完成')).toBeVisible()
    expect(conversationStyles).toMatch(/\.subagent-output-copy\s*\{[^}]*gap:\s*var\(--space-3\);/s)
    expect(within(output as HTMLElement).getByText('公司定位：中国最大的搜索引擎和 AI 科技公司。')).toBeVisible()
    expect(screen.getByText('Read')).toBeVisible()
    expect(screen.getByText('/research/url.json')).toBeVisible()
    expect(screen.queryByRole('button', { name: '展开全部详情' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '收起全部详情' })).not.toBeInTheDocument()

    const toolDetails = container.querySelector<HTMLDetailsElement>('.subagent-tool-row')
    expect(toolDetails).toHaveAttribute('data-tool-name', 'read_file')
    expect(toolDetails?.querySelector('summary')).toHaveTextContent('Read/research/url.json')
    expect(toolDetails?.querySelector('summary')).not.toHaveTextContent('researcher')
    expect(toolDetails?.open).toBe(false)
    await user.click(toolDetails!.querySelector('summary')!)
    expect(toolDetails?.open).toBe(true)
    expect(container.querySelector('.subagent-trace-marker')).not.toBeInTheDocument()
    expect(toolDetails?.querySelector('time')).not.toBeInTheDocument()
    expect(screen.getByText('{"file_path":"/research/url.json"}')).toBeVisible()
    expect(screen.getByText('https://www.baidu.com')).toBeVisible()

    await user.click(toolDetails!.querySelector('summary')!)
    expect(toolDetails?.open).toBe(false)
  })

  it('shows the complete SubAgent input only while its summary is hovered or focused', async () => {
    const user = userEvent.setup()
    const fullInput = '先读取完整需求并逐项核对，再调用 write_file 写入结果；不要省略任何已确认约束'
    const { container } = render(
      <MessageBlock
        message={{
          ...subagentMessage,
          meta: { ...subagentMessage.meta, input: fullInput },
        }}
        childTools={[childTool]}
      />,
    )
    await user.click(container.querySelector('.subagent-card-head')!)
    const summary = container.querySelector<HTMLElement>('.subagent-task-summary')
    expect(summary).not.toBeNull()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    await user.hover(summary!)
    const hovered = screen.getByRole('tooltip')
    expect(hovered).toHaveTextContent(fullInput)
    expect(summary).toHaveAttribute('aria-describedby', hovered.id)

    await user.unhover(summary!)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    fireEvent.focus(summary!)
    const focused = screen.getByRole('tooltip')
    expect(focused).toHaveTextContent(fullInput)
    fireEvent.blur(summary!)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('never renders legacy process or assistant reasoning content', () => {
    const processMessage: Message = {
      id: 'legacy-process',
      role: 'process',
      content: '旧版思考过程',
      createdAt: '2026-08-06T02:30:40.000Z',
    }
    const assistantMessage: Message = {
      id: 'assistant-with-reasoning',
      role: 'assistant',
      content: '这是最终回答',
      createdAt: '2026-08-06T02:30:50.000Z',
      meta: { reasoning: '不应展示的模型推理', status: 'completed' },
    }

    const { rerender } = render(<MessageBlock message={processMessage} />)
    expect(screen.queryByText('旧版思考过程')).not.toBeInTheDocument()

    rerender(<MessageBlock message={assistantMessage} />)
    expect(screen.getByText('这是最终回答')).toBeVisible()
    expect(screen.queryByText('不应展示的模型推理')).not.toBeInTheDocument()
    expect(screen.queryByText('思考过程')).not.toBeInTheDocument()
  })

  it('maps a standalone file tool to a compact title and parameter summary', () => {
    const { container } = render(<MessageBlock message={childTool} />)
    const summary = container.querySelector('.tool-card > summary')

    expect(summary).toHaveTextContent('Read/research/url.json')
    expect(summary).not.toHaveTextContent('researcher')
  })

  it('never exposes internal Tool call IDs when captured details are unavailable', () => {
    const { container } = render(<>
      <MessageBlock message={{
        id: 'tool:internal-known-id',
        role: 'tool',
        content: '',
        createdAt: '2026-08-24T00:00:00.000Z',
        meta: {
          toolName: 'ls',
          toolCallId: 'call_internal_known',
          status: 'completed',
        },
      }} />
      <MessageBlock message={{
        id: 'tool:internal-unknown-id',
        role: 'tool',
        content: '',
        createdAt: '2026-08-24T00:00:00.000Z',
        meta: {
          toolName: 'custom_tool',
          toolCallId: 'call_internal_unknown',
          status: 'completed',
        },
      }} />
    </>)

    const summaries = Array.from(container.querySelectorAll('.tool-card > summary'))
    expect(summaries[0]).toHaveTextContent('List')
    expect(summaries[0]).not.toHaveTextContent('call_internal_known')
    expect(summaries[0]).not.toHaveTextContent('tool:internal-known-id')
    expect(summaries[1]).toHaveTextContent('Tool callcustom_tool')
    expect(summaries[1]).not.toHaveTextContent('call_internal_unknown')
    expect(summaries[1]).not.toHaveTextContent('tool:internal-unknown-id')
  })

  it('keeps an expanded row open while streamed arguments and the final result update', async () => {
    const user = userEvent.setup()
    const running: Message = {
      id: 'tool-search-stream',
      role: 'tool',
      content: '',
      createdAt: '2026-08-24T00:00:00.000Z',
      meta: {
        toolName: 'web_search',
        toolCallId: 'tool-search-stream',
        params: '{"query":"Lang',
        result: '',
        status: 'running',
      },
    }
    const { container, rerender } = render(<MessageBlock message={running} />)
    const row = container.querySelector<HTMLDetailsElement>('.tool-card')!

    await user.click(row.querySelector('summary')!)
    expect(row.open).toBe(true)
    const detailSections = row.querySelectorAll('.tool-detail-section')
    expect(detailSections).toHaveLength(2)
    expect(detailSections[0]).toHaveClass('tool-detail-section--params')
    expect(detailSections[1]).toHaveClass('tool-detail-section--result')
    expect(row.querySelector('.tool-code-field')).toHaveTextContent('{"query":"Lang')
    expect(screen.getByText('输入')).toBeInTheDocument()
    expect(screen.getByText('输出')).toBeInTheDocument()
    expect(screen.getByLabelText('工具字段加载中')).toHaveClass('tool-field-pending')
    expect(row.querySelector('.tool-skeleton')).toBeNull()
    expect(conversationStyles).toMatch(/\.tool-detail-card\s*\{[^}]*margin:\s*var\(--space-2\) 0 0 calc\(var\(--icon-sm\) \+ var\(--space-2\)\);[^}]*background:\s*var\(--color-layer-1\);[^}]*font-family:\s*var\(--font-ui\);/s)
    expect(conversationStyles).toMatch(/\.tool-detail-section\s*\{[^}]*grid-template-columns:\s*calc\(var\(--space-12\) \+ var\(--space-2\)\) minmax\(0, 1fr\);[^}]*column-gap:\s*var\(--space-4\);[^}]*align-items:\s*baseline;[^}]*max-height:\s*150px;/s)
    expect(conversationStyles).toMatch(/\.tool-detail-section--params\s*\{[^}]*background:\s*var\(--color-layer-2\);/s)
    expect(conversationStyles).toMatch(/\.tool-detail-section--result\s*\{[^}]*background:\s*var\(--color-layer-1\);/s)
    expect(conversationStyles).toMatch(/\.tool-code-field,[\s\S]*\.tool-rich-field\s*\{[^}]*font-family:\s*var\(--font-code\);[^}]*font-size:\s*var\(--type-caption-size\);/s)
    expect(conversationStyles).toMatch(/\.tool-field-label\s*\{[^}]*position:\s*sticky;[^}]*top:\s*0;[^}]*align-self:\s*baseline;/s)
    expect(conversationStyles).toMatch(/\.tool-field-pending > i\s*\{[^}]*animation:\s*conversation-tool-result-pulse var\(--motion-tool-result-cycle\)/s)
    expect(conversationStyles).toMatch(/@container conversation \(max-width:\s*440px\)[\s\S]*\.tool-detail-card\s*\{[^}]*margin-left:\s*0;[^}]*\}[\s\S]*\.tool-detail-section\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);[^}]*row-gap:\s*var\(--space-1-5\);/s)

    rerender(<MessageBlock message={{
      ...running,
      meta: {
        ...running.meta,
        params: '{"query":"LangGraph"}',
        result: '**完成** [文档](https://example.com)',
        status: 'completed',
      },
    }} />)

    expect(row.open).toBe(true)
    expect(row.querySelector('summary')).toHaveTextContent('SearchLangGraph')
    expect(screen.getByText('{"query":"LangGraph"}')).toBeVisible()
    expect(screen.getByRole('link', { name: '文档' })).toHaveAttribute('href', 'https://example.com')
  })

  it('renders a batch as spaced rows without restoring an outer card', () => {
    const { container } = render(<ToolCallBatch messages={[childTool, secondChildTool]} />)

    expect(container.querySelectorAll('.tool-batch > .tool-card')).toHaveLength(2)
    expect(conversationStyles).toMatch(/\.subagent-card\s*\{[^}]*margin:\s*0 0 var\(--space-6\);/s)
    expect(conversationStyles).toMatch(/\.tool-card,\s*\.tool-batch\s*\{\s*margin:\s*0 0 var\(--space-6\);/s)
    expect(conversationStyles).toMatch(/\.tool-batch\s*{[^}]*display:\s*flex;[^}]*gap:\s*var\(--space-4\)/s)
    expect(conversationStyles).not.toMatch(/\.tool-batch\s*{[^}]*(?:border|box-shadow|background):/s)
  })

  it('explains that a paused tool has not executed yet', () => {
    render(
      <MessageBlock
        message={{
          ...childTool,
          id: 'tool-write-file-paused',
          meta: {
            ...childTool.meta,
            toolName: 'write_file',
            result: '',
            status: 'paused',
          },
        }}
      />,
    )

    expect(screen.getByText('等待审批')).toBeInTheDocument()
    expect(screen.getByLabelText('等待审批后执行')).toHaveClass('tool-field-pending')
    expect(screen.getByLabelText('等待审批后执行')).not.toHaveAttribute('aria-busy')
    expect(screen.queryByText('等待审批后执行')).not.toBeInTheDocument()
  })

  it('replaces a failed row summary with the first result line', () => {
    const { container } = render(<MessageBlock message={{
      ...childTool,
      id: 'tool-read-file-failed',
      meta: {
        ...childTool.meta,
        result: '读取失败\n权限不足',
        status: 'failed',
      },
    }} />)

    const row = container.querySelector('.tool-card')
    expect(row?.querySelector('summary')).toHaveTextContent('Read读取失败')
    expect(row?.querySelector('summary')).not.toHaveTextContent('/research/url.json')
    expect(row?.querySelector('.tool-row-state-dot.is-failed')).not.toBeNull()
  })

  it('preserves literal JSON escapes in Tool parameters', async () => {
    const params = '{"content":"line1\\nline2","path":"C:\\\\temp\\\\result.txt"}'
    const { container } = render(<MessageBlock message={{
      ...childTool,
      id: 'tool-literal-escapes',
      meta: {
        ...childTool.meta,
        params,
      },
    }} />)
    const row = container.querySelector<HTMLDetailsElement>('.tool-card')
    if (!row) throw new Error('缺少 Tool 行')

    await userEvent.click(row.querySelector('summary')!)
    const rendered = row.querySelector('.tool-code-field code')?.textContent

    expect(rendered).toBe(params)
    expect(() => JSON.parse(rendered ?? '')).not.toThrow()
  })

  it('keeps child Tool disclosures independent without an expand-all control', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <MessageBlock message={subagentMessage} childTools={[childTool, secondChildTool]} />,
    )

    await user.click(container.querySelector('.subagent-card-head')!)
    const toolRows = Array.from(container.querySelectorAll<HTMLDetailsElement>('.subagent-tool-row'))

    await user.click(toolRows[0].querySelector('summary')!)
    expect(toolRows[0].open).toBe(true)
    expect(toolRows[1].open).toBe(false)

    await user.click(toolRows[1].querySelector('summary')!)
    expect(toolRows.every((row) => row.open)).toBe(true)

    await user.click(toolRows[0].querySelector('summary')!)
    expect(toolRows[0].open).toBe(false)
    expect(toolRows[1].open).toBe(true)
    expect(screen.queryByRole('button', { name: '收起全部详情' })).not.toBeInTheDocument()
  })

  it('keeps the Agent and child Tool open while streamed Tool and Agent output settle', async () => {
    const user = userEvent.setup()
    const runningAgent: Message = {
      ...subagentMessage,
      meta: { ...subagentMessage.meta, result: '', status: 'running' },
    }
    const runningTool: Message = {
      ...childTool,
      meta: {
        ...childTool.meta,
        params: '{"file_path":"/research',
        result: '',
        status: 'running',
      },
    }
    const { container, rerender } = render(
      <MessageBlock message={runningAgent} childTools={[runningTool]} />,
    )
    const agent = container.querySelector<HTMLDetailsElement>('.subagent-card')!

    await user.click(agent.querySelector('.subagent-card-head')!)
    const tool = container.querySelector<HTMLDetailsElement>('.subagent-tool-row')!
    await user.click(tool.querySelector('summary')!)
    expect(agent.open).toBe(true)
    expect(tool.open).toBe(true)
    expect(agent.querySelector('.tool-row-state-dot')).toBeNull()

    rerender(<MessageBlock
      message={{
        ...runningAgent,
        meta: {
          ...runningAgent.meta,
          result: '研究完成，查看[报告](https://example.com/report)',
          status: 'completed',
        },
      }}
      childTools={[{
        ...runningTool,
        meta: {
          ...runningTool.meta,
          params: '{"file_path":"/research/url.json"}',
          result: '读取完成',
          status: 'completed',
        },
      }]}
    />)

    expect(agent.open).toBe(true)
    expect(tool.open).toBe(true)
    expect(agent.querySelector('.tool-row-state-dot')).toBeNull()
    expect(agent.querySelector('.subagent-output-node')).toHaveClass('is-completed')
    expect(screen.getByText('{"file_path":"/research/url.json"}')).toBeVisible()
    expect(screen.getByText('读取完成')).toBeVisible()
    expect(screen.getByRole('link', { name: '报告' })).toHaveAttribute('href', 'https://example.com/report')
  })

  it('uses a direct terminal node when no Tool exists and exposes every Agent state', async () => {
    const user = userEvent.setup()
    const running: Message = {
      ...subagentMessage,
      content: '等待研究任务',
      meta: {
        ...subagentMessage.meta,
        input: undefined,
        result: '',
        status: 'running',
      },
    }
    const { container, rerender } = render(<MessageBlock message={running} childTools={[]} />)
    const agent = container.querySelector<HTMLDetailsElement>('.subagent-card')!
    await user.click(agent.querySelector('.subagent-card-head')!)

    expect(agent.querySelector('.subagent-task-line')).toBeNull()
    expect(agent.querySelector('.subagent-trace-list')).toHaveClass('is-tool-empty')
    expect(agent.querySelector('.subagent-tool-node')).toBeNull()
    expect(agent.querySelector('.subagent-output-node')).toHaveClass('is-running')
    expect(agent.querySelector('.subagent-output-pulse')).not.toBeNull()
    expect(within(agent.querySelector('.subagent-output-node')!).getByText('执行中')).toBeVisible()
    expect(agent.querySelector('.subagent-output-node .activity-dots')).toBeNull()

    rerender(<MessageBlock message={{
      ...running,
      meta: { ...running.meta, result: '等待用户确认', status: 'paused' },
    }} childTools={[]} />)
    expect(agent.querySelector('.tool-row-state-dot.is-paused')).not.toBeNull()
    expect(agent.querySelector('.subagent-output-node')).toHaveClass('is-paused')

    rerender(<MessageBlock message={{
      ...running,
      meta: { ...running.meta, result: '子 Agent 执行失败', status: 'failed' },
    }} childTools={[]} />)
    expect(agent.querySelector('.tool-row-state-dot.is-failed')).not.toBeNull()
    expect(agent.querySelector('.subagent-output-node')).toHaveClass('is-failed')
    expect(within(agent.querySelector('.subagent-output-node')!).getByText('执行失败')).toBeVisible()
    expect(conversationStyles).toMatch(/\.subagent-trace-output\s*{[^}]*max-height:\s*180px;[^}]*overflow:\s*auto;/s)
  })
})

describe('无参工具详情', () => {
  it.each([
    ['list_attachments', '{}'],
    ['compact_conversation', ' {\n } '],
  ])('%s 的完整空对象不显示输入，展开后直接查看输出', async (toolName, params) => {
    const user = userEvent.setup()
    render(<MessageBlock message={{ ...childTool, meta: { toolName, params, result: '处理完成', status: 'completed' } }} />)

    await user.click(screen.getByText(toolName))
    expect(screen.queryByText('输入')).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '输出' })).toHaveTextContent('处理完成')
  })

  it.each([
    ['read_file', '{}'],
    ['custom_tool', '{}'],
    ['list_attachments', '{"filter":"report"}'],
    ['compact_conversation', undefined],
    ['list_attachments', ''],
    ['compact_conversation', '{'],
    ['list_attachments', 'null'],
    ['list_attachments', '[]'],
    ['compact_conversation', '"{}"'],
  ])('%s 参数为 %s 时保留输入供查看', (toolName, params) => {
    render(<MessageBlock message={{ ...childTool, meta: { toolName, params, status: 'running' } }} />)
    expect(screen.getByText('输入')).toBeInTheDocument()
  })

  it('参数收齐后隐藏输入，卡片保持展开且输出状态继续更新', async () => {
    const user = userEvent.setup()
    const message: Message = { ...childTool, meta: { toolName: 'list_attachments', params: '{', status: 'running' } }
    const { rerender } = render(<MessageBlock message={message} />)
    await user.click(screen.getByText('list_attachments'))
    expect(screen.getByRole('region', { name: '输入' })).toBeVisible()

    rerender(<MessageBlock message={{ ...message, meta: { ...message.meta, params: '{}' } }} />)
    expect(screen.queryByText('输入')).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '输出' })).toContainElement(screen.getByLabelText('工具字段加载中'))

    rerender(<MessageBlock message={{ ...message, meta: { ...message.meta, params: '{}', result: '附件暂时无法读取', status: 'failed' } }} />)
    expect(screen.queryByText('输入')).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '输出' })).toHaveTextContent('附件暂时无法读取')
  })

  it('批量与子 Agent 工具共用无参输入展示规则', () => {
    const noInput: Message = { ...childTool, meta: { toolName: 'list_attachments', params: '{}', result: '[]', status: 'completed' } }
    render(<>
      <ToolCallBatch messages={[noInput, { ...noInput, id: 'compact', meta: { ...noInput.meta, toolName: 'compact_conversation' } }]} />
      <MessageBlock message={subagentMessage} childTools={[{ ...noInput, id: 'nested-list' }]} />
    </>)
    expect(screen.queryByText('输入')).not.toBeInTheDocument()
    expect(screen.getAllByText('输出')).toHaveLength(3)
  })
})

describe('MessageBlock user composition', () => {
  it('用户消息在原操作位置显示本地时间，复制仍使用源文', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    const createdAt = new Date(2026, 7, 26, 9, 7).toISOString()
    const { container } = render(<MessageBlock message={{
      id: 'user-copy',
      role: 'user',
      content: '保留 **Markdown** 源文',
      createdAt,
    }} />)

    const markdown = container.querySelector('.message-markdown')
    const actionRow = screen.getByRole('group', { name: '消息操作' })
    expect(markdown).not.toBeNull()
    expect(markdown!.compareDocumentPosition(actionRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(actionRow).getAllByRole('button')).toHaveLength(1)
    expect(within(actionRow).getByRole('button', { name: '复制消息' }).querySelector('svg')).toHaveAttribute('width', '20')
    expect(within(actionRow).getByText('08-26 09:07')).toHaveAttribute('datetime', createdAt)

    await user.click(within(actionRow).getByRole('button', { name: '复制消息' }))
    expect(writeText).toHaveBeenCalledWith('保留 **Markdown** 源文')
    expect(screen.getByText('已复制')).toBeInTheDocument()
  })

  it('reports a failed user-message copy without adding another visible action', async () => {
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValueOnce(new Error('clipboard unavailable'))
    render(<MessageBlock message={{
      id: 'user-copy-failure',
      role: 'user',
      content: '复制失败样本',
      createdAt: '2026-08-26T00:00:00Z',
    }} />)

    await user.click(screen.getByRole('button', { name: '复制消息' }))
    expect(screen.getByRole('button', { name: '复制消息失败' })).toBeInTheDocument()
    expect(screen.getByText('复制失败，请重试')).toBeInTheDocument()
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })
})

describe('MessageBlock assistant composition', () => {
  it.each(['running', 'completed'] as const)(
    'does not present an empty %s Tool-only assistant message as a pending reply',
    (status) => {
      const { container } = render(<MessageBlock message={{
        id: `assistant-tool-only-${status}`,
        role: 'assistant',
        content: '',
        createdAt: '2026-08-29T00:00:00Z',
        meta: { status },
      }} />)

      expect(container).toBeEmptyDOMElement()
      expect(screen.queryByRole('status', { name: '正在回复' })).not.toBeInTheDocument()
    },
  )

  it('renders answer actions only when the message list marks the assistant as final', () => {
    const message: Message = {
      id: 'assistant-stage',
      role: 'assistant',
      content: '阶段性回答',
      createdAt: '2026-08-23T00:00:00Z',
      meta: { status: 'completed' },
    }
    const { container, rerender } = render(<MessageBlock message={message} showActions={false} />)

    expect(container.querySelector('.message-action-row')).toBeNull()

    rerender(<MessageBlock message={message} showActions />)
    expect(screen.getByRole('group', { name: '回答操作' })).toBeInTheDocument()
  })

  it('places the copy action after Markdown without fabricating sources or citations', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    const { container } = render(
      <MessageBlock message={{
        id: 'assistant-final',
        role: 'assistant',
        content: '这是**最终回答**',
        createdAt: '2026-08-23T00:00:00Z',
        meta: { status: 'completed' },
      }} />,
    )

    const markdown = container.querySelector('.message-markdown')
    const actionRow = container.querySelector('.message-action-row--assistant')
    expect(markdown).not.toBeNull()
    expect(actionRow).not.toBeNull()
    expect(screen.getByRole('group', { name: '回答操作' })).toBe(actionRow)
    expect(markdown!.compareDocumentPosition(actionRow!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(container.querySelector('.source-summary, .citation')).toBeNull()
    expect(conversationStyles).toMatch(/\.message-action-row\s*\{[^}]*--message-action-control-size:\s*var\(--control-xs\);[^}]*--message-action-icon-size:\s*var\(--icon-lg\);[^}]*align-items:\s*flex-start;/s)
    expect(conversationStyles).toMatch(/\.message-action-row--assistant\s*\{[^}]*min-height:\s*46px;[^}]*margin-top:\s*0;[^}]*padding-top:\s*5px;/s)
    expect(conversationStyles).toMatch(/\.message-action-row--assistant \.ui-icon-button-wrap\s*\{[^}]*width:\s*var\(--message-action-icon-size\);/s)
    expect(conversationStyles).toMatch(/\.message-action-row--assistant \.ui-icon-button\s*\{[^}]*margin-inline:\s*calc\(\(var\(--message-action-control-size\) - var\(--message-action-icon-size\)\) \/ -2\);/s)
    expect(conversationStyles).toMatch(/@media \(any-hover:\s*none\), \(any-pointer:\s*coarse\)[\s\S]*\.message-action-row\s*\{\s*--message-action-control-size:\s*var\(--control-lg\);/s)

    await user.click(screen.getByRole('button', { name: '复制回答' }))
    expect(writeText).toHaveBeenCalledWith('这是**最终回答**')
    expect(screen.getByText('已复制')).toBeInTheDocument()
  })
})

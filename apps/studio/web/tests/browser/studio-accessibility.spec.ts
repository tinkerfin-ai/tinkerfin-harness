import { toolReviewInterrupts, planInterrupt } from '../../src/test/aguiFixtures'
import { expect, test, type Locator, type Page, type Route } from '@playwright/test'
import { resolve } from 'node:path'
import type { ConversationHistoryDetail, TraceMessage } from '../../src/api/conversation/history'
import type { TaskTraceSnapshot } from '../../src/api/conversation/taskTrace'
import {
  emptyTraceGraph,
  traceGraphNode,
  traceGraphWithNodes,
} from '../../src/test/traceFixtures'
import type { JsonObject, JsonValue, Message } from '../../src/types'

const THREAD_ID = 'browser-thread'
const BASE_TIME = '2026-08-25T00:00:00.000Z'

const user = {
  user_id: 7,
  username: 'browser-user',
  display_name: '浏览器用户',
  avatar_url: null,
  roles: [],
  disabled: false,
}

const messages: Message[] = Array.from({ length: 150 }, (_, index) => ({
  id: `browser-message-${index + 1}`,
  role: 'assistant',
  content: `浏览器历史消息 ${index + 1}`,
  createdAt: BASE_TIME,
}))

messages.push({
  id: 'browser-subagent',
  role: 'subagent',
  content: '浏览器子智能体结果',
  createdAt: BASE_TIME,
  meta: {
    agentName: 'researcher',
    result: '子智能体已完成',
    status: 'completed',
    subRunId: 'browser-subagent-run-completed',
  },
})

messages.push({
  id: 'browser-tool',
  role: 'tool',
  content: 'read_file',
  createdAt: BASE_TIME,
  meta: {
    toolName: 'read_file',
    params: '{"path":"README.md"}',
    result: '读取完成',
    status: 'completed',
  },
})

messages.push({
  id: 'browser-final-answer',
  role: 'assistant',
  content: '浏览器最终回答',
  createdAt: BASE_TIME,
  meta: { status: 'completed' },
})

const runningActivityMessages: Message[] = [
  ...messages,
  {
    id: 'browser-running-user',
    role: 'user',
    content: '继续执行新一轮任务',
    createdAt: BASE_TIME,
  },
  {
    id: 'browser-running-stage',
    role: 'assistant',
    content: '我先检查当前状态',
    createdAt: BASE_TIME,
    meta: { status: 'completed', runId: 'browser-run' },
  },
  {
    id: 'browser-running-subagent',
    role: 'subagent',
    content: '浏览器运行中子智能体',
    createdAt: BASE_TIME,
    meta: {
      agentName: 'researcher',
      input: '检查运行中标题稳定性',
      result: '',
      status: 'running',
      toolCallId: 'browser-task-call',
      subRunId: 'browser-subagent-run',
      runId: 'browser-subagent-run',
    },
  },
  {
    id: 'browser-running-child-tool',
    role: 'tool',
    content: 'read_file',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'read_file',
      params: '{"path":"README.md"}',
      result: '',
      status: 'running',
      toolCallId: 'browser-child-tool-call',
      runId: 'browser-subagent-run',
      sourceAgentName: 'researcher',
    },
  },
  {
    id: 'browser-running-main-tool',
    role: 'tool',
    content: 'read_file',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'read_file',
      params: '{"path":"AGENTS.md"}',
      result: '',
      status: 'running',
      toolCallId: 'browser-main-tool-call',
      runId: 'browser-run',
    },
  },
]

const spacingAuditMessages: Message[] = [
  {
    id: 'spacing-user-a',
    role: 'user',
    content: '气泡 A',
    createdAt: BASE_TIME,
  },
  {
    id: 'spacing-assistant-a',
    role: 'assistant',
    content: '普通文本 A\n\n第二段文本 A',
    createdAt: BASE_TIME,
    meta: { status: 'completed' },
  },
  {
    id: 'spacing-user-b',
    role: 'user',
    content: '气泡 B',
    createdAt: BASE_TIME,
  },
  {
    id: 'spacing-subagent',
    role: 'subagent',
    content: '子智能体结果',
    createdAt: BASE_TIME,
    meta: {
      agentName: 'researcher',
      result: '完成',
      status: 'completed',
      subRunId: 'spacing-subagent-run',
    },
  },
  {
    id: 'spacing-tool',
    role: 'tool',
    content: 'read_file',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'read_file',
      params: '{"path":"README.md"}',
      result: '完成',
      status: 'completed',
    },
  },
  {
    id: 'spacing-assistant-b',
    role: 'assistant',
    content: '普通文本 B',
    createdAt: BASE_TIME,
    meta: { status: 'completed' },
  },
  {
    id: 'spacing-user-c',
    role: 'user',
    content: '气泡 C',
    createdAt: BASE_TIME,
  },
  {
    id: 'spacing-user-d',
    role: 'user',
    content: '气泡 D',
    createdAt: BASE_TIME,
  },
]

const spacingBatchMessages: Message[] = [
  {
    id: 'spacing-batch-user',
    role: 'user',
    content: '批量读取文件',
    createdAt: BASE_TIME,
  },
  ...['README.md', 'AGENTS.md'].map((path, index): Message => ({
    id: `spacing-batch-tool-${index + 1}`,
    role: 'tool',
    content: 'read_file',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'read_file',
      params: JSON.stringify({ path }),
      result: `已读取 ${path}`,
      status: 'completed',
      batchId: 'spacing-batch',
    },
  })),
  {
    id: 'spacing-batch-assistant',
    role: 'assistant',
    content: '批量读取完成',
    createdAt: BASE_TIME,
    meta: { status: 'completed' },
  },
]

const toolIconAuditMessages: Message[] = [
  'ls',
  'read_file',
  'write_file',
  'edit_file',
  'delete',
  'glob',
  'grep',
  'execute',
  'web_search',
  'other',
  'task',
].map((toolName, index): Message => ({
  id: `tool-icon-audit-${index + 1}`,
  role: 'tool',
  content: toolName,
  createdAt: BASE_TIME,
  meta: {
    toolName,
    params: JSON.stringify({ path: `/icon-audit/${toolName}` }),
    result: '已完成',
    status: 'completed',
  },
}))

const markdownLayoutMessages: Message[] = [
  {
    id: 'markdown-layout-user',
    role: 'user',
    content: '检查完整 Markdown 排版',
    createdAt: BASE_TIME,
  },
  {
    id: 'markdown-layout-assistant',
    role: 'assistant',
    content: `# 一级标题

第一段正文

第二段正文

## 二级标题

### 三级标题

#### 四级标题

##### 五级标题

###### 六级标题

- 第一项
- 第二项

> 引用内容

行内代码 \`const answer = 42\`

---

| 名称 | 说明 | 状态 |
| --- | --- | --- |
| TinkerFin | 智能体工作台 | 正常 |

\`\`\`ts
const value = 42
\`\`\`
`,
    createdAt: BASE_TIME,
    meta: { status: 'completed' },
  },
]

const approvalMessages: Message[] = [
  {
    id: 'approval-user',
    role: 'user',
    content: '写入两份文件',
    createdAt: BASE_TIME,
  },
  ...[
    ['browser-approval-tool-1', 'browser-approval-call-1', '/first-approval.txt'],
    ['browser-approval-tool-2', 'browser-approval-call-2', '/second-approval.txt'],
  ].map(([id, toolCallId, filePath]) => ({
    id,
    role: 'tool' as const,
    content: 'write_file',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'write_file',
      params: JSON.stringify({ file_path: filePath, content: `写入 ${filePath}` }),
      status: 'paused' as const,
      batchId: 'browser-approval-batch',
      toolCallId,
      interruptId: `${toolCallId}-interrupt`,
    },
  })),
]

const approvalScrollMessages: Message[] = [
  ...Array.from({ length: 24 }, (_, index): Message => ({
    id: `approval-scroll-${index}`,
    role: 'assistant',
    content: `待审批滚动占位消息 ${index + 1}`,
    createdAt: BASE_TIME,
    meta: { status: 'completed' },
  })),
  ...approvalMessages,
]

const approvalItems = [
  ['/first-approval.txt', 'browser-approval-call-1'],
  ['/second-approval.txt', 'browser-approval-call-2'],
].map(([filePath, toolCallId], index) => {
  const originalArgs = { file_path: filePath, content: `写入 ${filePath}` }
  return {
    id: `browser-approval-${index + 1}`,
    interruptId: `${toolCallId}-interrupt`,
    toolCallId,
    toolName: 'write_file',
    params: JSON.stringify(originalArgs, null, 2),
    input: filePath,
    description: `需要人工审批：Agent 正准备写入 ${filePath}`,
    originalArgs,
    allowedDecisions: ['approve', 'reject'] as Array<'approve' | 'reject'>,
  }
})

const planQuestionForm = {
  title: '确认执行方式',
  description: '这些答案会影响后续规划',
  questions: [
    {
      id: 'browser-plan-question-item',
      answerType: 'single_choice',
      prompt: '主要运行平台是什么？',
      required: true,
      options: [
        { id: 'web', label: 'Web', attributes: { recommended: true } },
        { id: 'mobile', label: '移动端', attributes: { recommended: false } },
      ],
      allowFreeText: true,
    },
    {
      id: 'browser-plan-multiple',
      answerType: 'multiple_choice',
      prompt: '需要覆盖哪些平台？',
      required: true,
      options: [
        { id: 'web', label: 'Web', attributes: { recommended: true } },
        { id: 'mobile', label: '移动端', attributes: { recommended: true } },
      ],
      allowFreeText: true,
      minSelections: 2,
      maxSelections: 2,
    },
    {
      id: 'browser-plan-text',
      answerType: 'text',
      prompt: '还有哪些限制？',
      required: false,
    },
    {
      id: 'browser-plan-date',
      answerType: 'date',
      prompt: '期望完成日期是什么时候？',
      required: true,
    },
    {
      id: 'browser-plan-time',
      answerType: 'time',
      prompt: '期望几点上线？',
      required: true,
      timeZone: 'Asia/Shanghai',
      minimum: '09:00',
      maximum: '18:00',
    },
    {
      id: 'browser-plan-datetime',
      answerType: 'datetime',
      prompt: '回滚截止点是什么时候？',
      required: true,
      timeZone: 'Asia/Shanghai',
      minimum: '2026-09-01T09:00',
      maximum: '2026-09-30T18:00',
    },
  ] as JsonValue[],
} satisfies JsonObject

const planQuestionTabOrderForm = {
  title: '方案规划澄清',
  description: '回答以下问题将帮助确定本次方案的目标、范围与关键约束',
  questions: [
    ...Array.from({ length: 2 }, (_, index) => ({
      id: `browser-plan-tab-intro-${index + 1}`,
      answerType: 'single_choice',
      prompt: `前置问题 ${index + 1}`,
      required: true,
      options: [{
        id: `continue-${index + 1}`,
        label: `继续 ${index + 1}`,
        attributes: { recommended: true },
      }],
      allowFreeText: false,
    })),
    {
      id: 'browser-plan-tab-multiple',
      answerType: 'multiple_choice',
      prompt: '需要覆盖哪些浏览器？',
      required: true,
      options: ['Chrome', 'Safari', 'Firefox', 'Edge', 'Opera', 'Arc', 'Brave', 'Vivaldi']
        .map((label, index) => ({
          id: `browser-${index + 1}`,
          label,
          attributes: { recommended: index === 0 },
        })),
      allowFreeText: true,
      minSelections: 1,
      maxSelections: 7,
    },
    ...Array.from({ length: 5 }, (_, index) => ({
      id: `browser-plan-tab-tail-${index + 1}`,
      answerType: 'text',
      prompt: `后续问题 ${index + 1}`,
      required: false,
    })),
  ] as JsonValue[],
} satisfies JsonObject

const dateOnlyPlanQuestionForm = {
  title: '确认发布日期',
  description: '日期会影响计划安排',
  questions: [{
    id: 'browser-plan-date-only',
    answerType: 'date',
    prompt: '目标发布日期是哪一天？',
    required: true,
  }] as JsonValue[],
} satisfies JsonObject

const createPlanQuestionPayload = (form: JsonObject): JsonObject => ({
  schema: 'tinkerfin.runtime-interrupt',
  kind: 'tinkerfin:plan_clarification',
  message: 'Answer required questions and optionally refine the Plan.',
  responseSchema: { type: 'object' },
  metadata: {
    origin: 'plan',
    clarification: { form },
  },
})

const planReviewPayload: JsonObject = {
  schema: 'tinkerfin.runtime-interrupt',
  kind: 'tinkerfin:plan_review',
  message: 'Review the proposed Plan before execution begins.',
  responseSchema: {
    discriminator: {
      propertyName: 'type',
      mapping: {
        approve: '#/$defs/ApprovePlan',
        reject: '#/$defs/RejectPlan',
        cancel: '#/$defs/CancelPlan',
      },
    },
  },
  metadata: {
    origin: 'plan',
    review: {
      draft: {
        revision: 3,
        contentSchema: {
          fingerprint: '0'.repeat(64),
          mediaType: 'text/markdown',
        },
        content: {
          description: '保持现有会话行为并完成响应式验证',
          markdown: [
            '# 浏览器计划草稿',
            '',
            '- 保持现有会话行为',
            '- 完成响应式验证',
            '- 验证正常结束与异常恢复',
            '- 验证断连后的状态保持',
            '- 验证浅色与深色主题',
            '- 验证窄屏与桌面布局',
            '- 验证键盘和触控操作',
            '- 验证 Footer 不遮挡最终验收项',
          ].join('\n'),
        },
      },
    },
  },
}

const success = (data: unknown) => ({ code: 0, message: 'success', data })

const fulfillJson = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(success(data)),
})

interface MockStudioOptions {
  approval?: boolean
  conversationMessages?: Message[]
  emptyHistory?: boolean
  expectedMessageText?: string
  onHistoryRequest?: (request: { cursor: string | null; receivedAt: number }) => void
  paginatedHistory?: boolean
  paginationPageCount?: number
  paginationResponseDelayMs?: number
  pinError?: boolean
  planQuestion?: boolean
  planQuestionForm?: JsonObject
  planReview?: boolean
  runningActivity?: boolean
  taskTrace?: TaskTraceSnapshot
}

async function mockStudio(page: Page, {
  approval = false,
  conversationMessages,
  emptyHistory = false,
  expectedMessageText = '浏览器历史消息 150',
  onHistoryRequest,
  paginatedHistory = false,
  paginationPageCount = 2,
  paginationResponseDelayMs = 0,
  pinError = false,
  planQuestion = false,
  planQuestionForm: planQuestionFormOverride,
  planReview = false,
  runningActivity = false,
  taskTrace = { status: 'ready', todoGroups: [] },
}: MockStudioOptions = {}) {
  let historyRequestCount = 0
  const isWaitingForInput = approval || planQuestion || planReview
  const historyMessages = conversationMessages
    ?? (approval ? approvalMessages : runningActivity ? runningActivityMessages : messages)
  const buildTraceDetail = (includeTaskTrace = true): ConversationHistoryDetail => {
    const traceMessages = historyMessages.flatMap<TraceMessage>((message, index) => {
      if (message.role === 'user' || message.role === 'assistant') {
        return [{
          agui: null,
          id: message.id,
          traceSeq: (index * 2) + 1,
          sourceId: message.id,
          graphNamespace: [],
          runId: 'browser-run',
          role: message.role,
          content: message.content,
          contentOmitted: false,
          status: 'completed' as const,
          createdAt: message.createdAt,
          completedAt: message.meta?.completedAt ?? message.createdAt,
        }]
      }
      if (message.role === 'tool' && message.meta?.toolCallId) {
        return [{
          agui: null,
          id: message.id,
          traceSeq: (index * 2) + 1,
          sourceId: message.id,
          graphNamespace: [],
          runId: 'browser-run',
          role: 'tool' as const,
          content: message.meta.result ?? null,
          contentOmitted: message.meta.result == null,
          name: message.meta.toolName ?? null,
          toolCallId: message.meta.toolCallId,
          status: 'completed' as const,
          createdAt: message.createdAt,
          completedAt: message.meta.completedAt ?? message.createdAt,
        }]
      }
      return []
    })
    const subagentNodeIdsByRunId = new Map(
      historyMessages
        .filter((message) => message.role === 'subagent' && message.meta?.subRunId)
        .map((message) => [message.meta?.subRunId as string, message.id]),
    )
    const subagentNamespacesByRunId = new Map<string, string[]>(
      historyMessages
        .filter((message) => message.role === 'subagent' && message.meta?.subRunId)
        .map((message) => [
          message.meta?.subRunId as string,
          [`tools:${message.meta?.subRunId as string}`],
        ] as [string, string[]]),
    )
    const graphNodes = historyMessages.flatMap((message, index) => {
      if (
        message.role !== 'tool'
        && message.role !== 'subagent'
        && message.role !== 'process'
        && message.role !== 'error'
      ) return []
      const rawStatus = message.meta?.status
      const status = message.role === 'error'
        ? 'failed' as const
        : rawStatus === 'running'
          ? 'running' as const
          : rawStatus === 'paused'
            ? 'waiting' as const
            : rawStatus === 'failed'
              ? 'failed' as const
              : rawStatus === 'cancelled'
                ? 'cancelled' as const
                : 'succeeded' as const
      const request = message.role === 'subagent'
        ? message.meta?.input
        : message.meta?.params
      const result = message.meta?.result
      return [traceGraphNode({
        id: message.id,
        agui: message.role === 'tool' && message.meta?.toolCallId
          ? { kind: 'tool', toolCallId: message.meta.toolCallId }
          : message.role === 'subagent' && message.meta?.subRunId
            ? { kind: 'subagent', parentToolCallId: message.meta.toolCallId ?? message.id, subagentInvocationId: message.meta.subRunId }
            : null,
        parentSubagentId: message.role === 'tool'
          ? subagentNodeIdsByRunId.get(message.meta?.runId ?? '')
            ?? null
          : null,
        modelCallId: message.role === 'tool'
          ? message.meta?.batchId ?? null
          : null,
        kind: message.role === 'tool'
          ? 'tool'
          : message.role === 'subagent'
            ? 'subagent'
            : message.role === 'error'
              ? 'custom'
              : 'plan',
        status,
        name: message.meta?.toolName ?? message.meta?.agentName ?? message.content,
        runId: message.meta?.runId ?? 'browser-run',
        graphNamespace: message.role === 'subagent'
          ? subagentNamespacesByRunId.get(message.meta?.subRunId ?? '') ?? []
          : message.role === 'tool'
            ? subagentNamespacesByRunId.get(message.meta?.runId ?? '') ?? []
            : [],
        sourceId: message.role === 'tool'
          ? message.meta?.toolCallId ?? message.id
          : message.role === 'subagent'
            ? message.meta?.subRunId ?? message.id
            : message.id,
        startedAt: message.createdAt,
        startedSeq: (index * 2) + 1,
        updatedSeq: (index * 2) + 1,
        completedAt: status === 'running' || status === 'waiting'
          ? null
          : message.meta?.completedAt ?? message.createdAt,
        request: request ?? null,
        requestOmitted: request == null,
        result: result ?? null,
        resultOmitted: result == null,
        failure: message.role === 'error'
          ? { errorType: 'Error', message: message.content }
          : null,
      })]
    })
    const traceAsOfSeq = Math.max(1, (historyMessages.length * 2) + 1)
    const graph = traceGraphWithNodes(graphNodes, traceAsOfSeq)
    const interactions = approval
      ? [{
      agui: toolReviewInterrupts('browser-approval', approvalItems.map(item => ({ toolCallId: item.toolCallId, name: item.toolName, args: item.originalArgs, allowedDecisions: item.allowedDecisions }))),
          id: 'interaction-browser-approval',
          traceSeq: (historyMessages.length * 2) + 1,
          sourceId: 'browser-approval',
          graphNamespace: [],
          runId: 'browser-run',
          kind: 'tool_approval',
          status: 'pending' as const,
          toolCallIds: approvalItems.map((item) => item.toolCallId),
          payloadOmitted: false,
          payload: {
            action_requests: approvalItems.map((item) => ({
              name: item.toolName,
              arguments: {
                disposition: 'inline',
                safeSizeBytes: 100,
                value: Object.fromEntries(
                  Object.entries(item.originalArgs).map(([key, value]) => ['/' + key, value]),
                ),
              },
            })),
            review_configs: approvalItems.map((item) => ({
              action_name: item.toolName,
              allowed_decisions: item.allowedDecisions,
            })),
          },
          openedAt: BASE_TIME,
          resolvedAt: null,
        }]
      : planQuestion
        ? [{
      agui: [planInterrupt('browser-plan-question', createPlanQuestionPayload(planQuestionFormOverride ?? planQuestionForm))],
            id: 'interaction-browser-plan-question',
            traceSeq: (historyMessages.length * 2) + 1,
            sourceId: 'browser-plan-question',
            graphNamespace: [],
            runId: 'browser-run',
            kind: 'tinkerfin:plan_clarification',
            toolCallIds: [],
            status: 'pending' as const,
            payloadOmitted: false,
            payload: createPlanQuestionPayload(planQuestionFormOverride ?? planQuestionForm),
            openedAt: BASE_TIME,
            resolvedAt: null,
          }]
        : planReview
          ? [{
      agui: [planInterrupt('browser-plan-review', planReviewPayload)],
              id: 'interaction-browser-plan-review',
              traceSeq: (historyMessages.length * 2) + 1,
              sourceId: 'browser-plan-review',
              graphNamespace: [],
              runId: 'browser-run',
              kind: 'tinkerfin:plan_review',
              toolCallIds: [],
              status: 'pending' as const,
              payloadOmitted: false,
              payload: planReviewPayload,
              openedAt: BASE_TIME,
              resolvedAt: null,
            }]
          : []
    const execution = isWaitingForInput
      ? 'waiting' as const
      : runningActivity
        ? 'running' as const
        : 'succeeded' as const
    return { accessMode: 'write_approval',
      id: 1,
      threadId: THREAD_ID,
      title: '浏览器会话',
      titleSource: 'default',
      titleGenerationStatus: 'idle',
      titleSeq: 0,
      lastModel: 'GPT-5.5',
      pinned: false,
      asOfSeq: traceAsOfSeq,
      generation: `browser-generation:${THREAD_ID}`,
      observedAt: '2026-09-05T00:00:00.000000Z',
      headRunId: 'browser-run',
      runFailures: [],
      availableHeads: ['browser-run'],
      historyCursor: null,
      messageCount: traceMessages.filter((message) => message.role !== 'tool').length,
      toolCallCount: historyMessages.filter((message) => message.role === 'tool').length,
      messages: traceMessages,
      reasoning: [],
      graph: graphNodes.length > 0 ? graph : emptyTraceGraph(traceAsOfSeq),
      state: {
        root: planQuestion || planReview
          ? { tinkerfin_plan: { effectiveMode: 'plan' } }
          : {},
        subgraphs: {},
      },
      interactions,
      status: { execution, headRunId: 'browser-run' },
      completeness: {
        missingPrefix: false,
        missingTail: false,
        payloadOmitted: false,
      },
      taskTrace: includeTaskTrace ? taskTrace : null,
      createdAt: BASE_TIME,
      updatedAt: BASE_TIME,
    }
  }
  page.on('console', (message) => {
    if (message.type() === 'error') console.error(`browser console: ${message.text()}`)
  })
  page.on('pageerror', (error) => console.error(`browser pageerror: ${error.message}`))
  page.on('response', (response) => {
    if (response.status() >= 400) {
      console.error(`browser response: ${response.status()} ${response.url()}`)
    }
  })
  await page.addInitScript(({ storageKey, session }) => {
    window.localStorage.setItem(storageKey, JSON.stringify(session))
  }, {
    storageKey: 'tinkerfin.auth.session',
    session: {
      token: 'browser-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user,
    },
  })

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) {
      await route.continue()
      return
    }
    if (url.pathname === '/api/auth/me') {
      await fulfillJson(route, { expires_at: '2099-01-01T00:00:00.000Z', user })
      return
    }
    if (url.pathname === '/api/models') {
      await fulfillJson(route, {
        items: [
          { modelId: 'GPT-5.5', displayName: 'GPT-5.5', reasoningEnabled: false, isDefault: true },
          { modelId: 'Qwen-3.7', displayName: 'Qwen-3.7', reasoningEnabled: false, isDefault: false },
        ],
        defaultModelId: 'GPT-5.5',
      })
      return
    }
    if (url.pathname === '/api/conversation/config') {
      await fulfillJson(route, { dayRanges: [7, 30] })
      return
    }

    if (
      pinError
      && route.request().method() === 'PATCH'
      && url.pathname === `/api/conversation/${THREAD_ID}`
    ) {
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
      return
    }
    if (url.pathname === '/api/conversation/history') {
      const cursor = url.searchParams.get('cursor')
      onHistoryRequest?.({ cursor, receivedAt: Date.now() })
      const pageStart = cursor ? 20 + Math.max(0, historyRequestCount - 1) * 10 : 0
      const pageSize = paginatedHistory ? (cursor ? 10 : 20) : 1
      historyRequestCount += 1
      if (cursor && paginationResponseDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, paginationResponseDelayMs))
      }
      await fulfillJson(route, {
        items: emptyHistory ? [] : Array.from({ length: pageSize }, (_, offset) => {
          const index = pageStart + offset
          return { accessMode: 'full',
            id: index + 1,
            threadId: index === 0 ? THREAD_ID : `browser-history-${index}`,
            title: index === 0 ? '浏览器会话' : `分页验证会话 ${index}`,
            status: index === 0 && isWaitingForInput ? 'waiting_approval' : 'idle',
            lastRunId: index === 0 ? 'browser-run' : undefined,
            lastModel: 'GPT-5.5',
            messageCount: index === 0 ? historyMessages.length : 0,
            toolCallCount: index === 0 ? 1 : 0,
            hasPendingInterrupt: index === 0 && isWaitingForInput,
            pendingInteractionKind: index === 0 && isWaitingForInput
              ? planQuestion
                ? 'plan_clarification'
                : planReview
                  ? 'plan_review'
                  : 'tool_approval'
              : null,
            pinned: false,
            createdAt: BASE_TIME,
            updatedAt: BASE_TIME,
          }
        }),
        nextCursor: paginatedHistory && historyRequestCount < paginationPageCount
          ? `browser-page-${historyRequestCount + 1}`
          : null,
      })
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/history`) {
      await fulfillJson(
        route,
        buildTraceDetail(url.searchParams.get('includeTaskTrace') !== 'false'),
      )
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/trace`) {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `event: trace\ndata: ${JSON.stringify({
          type: 'snapshot',
          snapshot: buildTraceDetail(url.searchParams.get('includeTaskTrace') !== 'false'),
        })}\n\n`,
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({}) })
  })

  await page.goto('/')
  if (approval) await expect(page.getByRole('region', { name: '等待审批' })).toBeVisible()
  else if (planQuestion) await expect(page.getByRole('region', { name: 'Plan 澄清问题' })).toBeVisible()
  else if (planReview) await expect(page.getByRole('region', { name: 'Plan 审阅' })).toBeVisible()
  else await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  if (emptyHistory) await expect(page.locator('.composer-dock.is-hero')).toBeVisible()
  else if (!approval) {
    await expect(page.locator('.message-list')).toBeVisible()
    if (await page.getByText(expectedMessageText, { exact: true }).count()) {
      await expect(page.getByText(expectedMessageText, { exact: true })).toBeVisible()
    }
  }
}

const contrastRatios = async (page: Page, selector: string) => page.locator(selector).evaluateAll((elements) => {
  const parse = (value: string) => (value.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number)
  const luminance = (value: string) => {
    const channels = parse(value).map((channel) => {
      const normalized = channel / 255
      return normalized <= 0.04045
        ? normalized / 12.92
        : ((normalized + 0.055) / 1.055) ** 2.4
    })
    return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2])
  }
  return elements.map((element) => {
    const foreground = luminance(getComputedStyle(element).color)
    const background = luminance(getComputedStyle(element.parentElement!).backgroundColor)
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)
  })
})

const visibleSvgStrokeLeft = (locator: Locator) => locator.evaluate((element) => {
  const svg = element as SVGSVGElement
  const bounds = svg.getBBox()
  const matrix = svg.getScreenCTM()
  if (!matrix) throw new Error('SVG 屏幕变换不可用')
  const scale = Math.hypot(matrix.a, matrix.b)
  const strokeWidth = Number.parseFloat(getComputedStyle(svg).strokeWidth)
  return matrix.e + (bounds.x * scale) - ((strokeWidth * scale) / 2)
})

test('四个目标视口保持正确导航形态且没有页面级横向溢出', async ({ page }) => {
  await mockStudio(page)
  for (const [width, expectedMode] of [
    [320, 'overlay'],
    [768, 'rail'],
    [1024, 'expanded'],
    [1440, 'expanded'],
  ] as const) {
    await page.setViewportSize({ width, height: 900 })
    await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', expectedMode)
    const overflow = await page.evaluate(() => Math.max(
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
      document.body.scrollWidth - document.body.clientWidth,
    ))
    expect(overflow).toBeLessThanOrEqual(0)
  }
})

test('新会话点击后在浅深主题和四个视口都不显示品牌蓝选中态', async ({ page }) => {
  await mockStudio(page)
  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      if (width === 320) {
        await page.getByRole('button', { name: '打开导航' }).click()
      }
      const newChat = width === 768
        ? page.locator('.sidebar-rail').getByRole('button', { name: '新会话' })
        : page.locator('.sidebar-wide').getByRole('button', { name: '新会话' })
      await expect(newChat).toBeVisible()
      await expect(newChat).not.toHaveAttribute('title')
      await newChat.hover()
      await expect(page.getByRole('tooltip', { name: '新会话' })).toHaveCount(0)
      await newChat.click()
      await expect(newChat).not.toHaveClass(/is-selected/)
      await expect(newChat).not.toHaveAttribute('aria-pressed')
      const colors = await newChat.evaluate((element) => {
        const probe = document.createElement('span')
        probe.style.color = 'var(--color-brand-text)'
        document.body.append(probe)
        const brandText = getComputedStyle(probe).color
        probe.remove()
        return {
          color: getComputedStyle(element).color,
          brandText,
        }
      })
      expect(colors.color).not.toBe(colors.brandText)
    }
  }
})

test('折叠侧栏 tooltip 与 Rail 外边界保持稳定间距', async ({ page }) => {
  await mockStudio(page)
  const shell = page.locator('.app-shell')
  const rail = page.locator('.sidebar-rail')
  const searchButton = rail.getByRole('button', { name: '搜索会话' })
  const tooltip = rail.getByRole('tooltip').filter({ hasText: '搜索会话' })

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      if (await shell.getAttribute('data-sidebar-mode') !== 'rail') {
        await page.getByRole('button', { name: '收起侧边栏' }).click()
      }
      await expect(shell).toHaveAttribute('data-sidebar-mode', 'rail')
      await searchButton.hover()
      await expect(tooltip).toBeVisible()

      const railBounds = await rail.boundingBox()
      const buttonBounds = await searchButton.boundingBox()
      const tooltipBounds = await tooltip.boundingBox()
      if (!railBounds || !buttonBounds || !tooltipBounds) throw new Error('折叠侧栏 tooltip 几何不可用')
      expect(tooltipBounds.x - (railBounds.x + railBounds.width)).toBeGreaterThanOrEqual(8)
      expect(Math.abs(
        (tooltipBounds.y + (tooltipBounds.height / 2))
        - (buttonBounds.y + (buttonBounds.height / 2)),
      )).toBeLessThanOrEqual(1)
    }
  }
})

test('侧栏切换控件共享纵向锚点且 tooltip 避开相邻操作区', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockStudio(page)

  const sidebar = page.locator('#workspace-sidebar')
  const brand = sidebar.locator('.brand-logo')
  const searchButton = sidebar.getByRole('button', { name: '搜索会话' }).first()
  const conversationTab = page.getByRole('tab', { name: '对话' })
  const traceTab = page.getByRole('tab', { name: '链路' })
  const newChat = page.getByRole('button', { name: '新会话' }).first()
  const collapseButton = page.getByRole('button', { name: '收起侧边栏' })
  const collapseTooltip = sidebar.getByRole('tooltip').filter({ hasText: '收起侧边栏' })

  const [brandBounds, searchBounds, conversationTabBounds, traceTabBounds, collapseBounds] = await Promise.all([
    brand.boundingBox(),
    searchButton.boundingBox(),
    conversationTab.boundingBox(),
    traceTab.boundingBox(),
    collapseButton.boundingBox(),
  ])
  await collapseButton.hover()
  await expect(collapseTooltip).toBeVisible()
  const sidebarBounds = await sidebar.boundingBox()
  const newChatBounds = await newChat.boundingBox()
  const collapseTooltipBounds = await collapseTooltip.boundingBox()
  if (
    !sidebarBounds
    || !brandBounds
    || !searchBounds
    || !conversationTabBounds
    || !traceTabBounds
    || !newChatBounds
    || !collapseBounds
    || !collapseTooltipBounds
  ) {
    throw new Error('展开侧栏切换控件几何不可用')
  }
  const headerCenters = [
    brandBounds,
    searchBounds,
    collapseBounds,
    conversationTabBounds,
    traceTabBounds,
  ].map((bounds) => bounds.y + (bounds.height / 2))
  headerCenters.forEach((center) => expect(center).toBeCloseTo(32, 5))
  expect(newChatBounds.y).toBeCloseTo(68, 5)
  expect(collapseTooltipBounds.x).toBeGreaterThanOrEqual(sidebarBounds.x + sidebarBounds.width - 1)
  expect(
    collapseTooltipBounds.x < newChatBounds.x + newChatBounds.width
    && collapseTooltipBounds.x + collapseTooltipBounds.width > newChatBounds.x
    && collapseTooltipBounds.y < newChatBounds.y + newChatBounds.height
    && collapseTooltipBounds.y + collapseTooltipBounds.height > newChatBounds.y,
  ).toBe(false)

  await collapseButton.click()
  await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', 'rail')
  const rail = page.locator('.sidebar-rail')
  const expandButton = rail.getByRole('button', { name: '打开侧边栏' })
  const expandTooltip = rail.getByRole('tooltip').filter({ hasText: '打开侧边栏' })
  const nextRailButton = rail.getByRole('button', { name: '新会话' })
  await expect.poll(async () => (await rail.boundingBox())?.x).toBe(0)
  const expandBounds = await expandButton.boundingBox()
  const railBounds = await rail.boundingBox()
  await expandButton.hover()
  await expect(expandTooltip).toBeVisible()
  const expandTooltipBounds = await expandTooltip.boundingBox()
  const nextRailButtonBounds = await nextRailButton.boundingBox()
  if (!railBounds || !expandBounds || !expandTooltipBounds || !nextRailButtonBounds) {
    throw new Error('折叠侧栏切换控件几何不可用')
  }
  expect(Math.abs(
    (expandBounds.y + (expandBounds.height / 2))
    - (collapseBounds.y + (collapseBounds.height / 2)),
  )).toBeLessThanOrEqual(1)
  expect(expandBounds.y + (expandBounds.height / 2)).toBeCloseTo(32, 5)
  expect(nextRailButtonBounds.y).toBeCloseTo(newChatBounds.y, 5)
  expect(nextRailButtonBounds.y - (expandBounds.y + expandBounds.height)).toBeGreaterThanOrEqual(12)
  expect(expandTooltipBounds.y + expandTooltipBounds.height).toBeLessThanOrEqual(nextRailButtonBounds.y)
  expect(expandTooltipBounds.x).toBeGreaterThanOrEqual(railBounds.x + railBounds.width + 8)

  const overflow = await page.evaluate(() => Math.max(
    document.documentElement.scrollWidth - document.documentElement.clientWidth,
    document.body.scrollWidth - document.body.clientWidth,
  ))
  expect(overflow).toBeLessThanOrEqual(0)
})

test('操作与读取异常只显示一条全局 Toast，并保留独立恢复入口', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockStudio(page, { pinError: true })

  await page.getByRole('button', { name: '管理会话：浏览器会话' }).click()
  await page.getByRole('button', { name: '置顶', exact: true }).click()
  const notifications = page.getByRole('list', { name: '系统提示' })
  await expect(notifications.getByRole('alert')).toHaveText('置顶状态更新失败，请重试')
  await expect(page.getByText('置顶状态更新失败，请重试', { exact: true })).toHaveCount(1)
  await notifications.getByRole('button', { name: '关闭提示：置顶状态更新失败，请重试' }).click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.getByRole('tab', { name: '链路' }).click()

  const trace = page.getByRole('tabpanel', { name: '链路', exact: true })
  const retry = trace.getByRole('button', { name: '重新加载', exact: true })
  await expect(notifications.getByRole('alert')).toHaveText('链路加载失败')
  await expect(page.getByText('链路加载失败', { exact: true })).toHaveCount(1)
  await expect(trace.getByRole('alert')).toHaveCount(0)
  await expect(retry).toBeEnabled()
  const toast = page.locator('.toast-card').filter({ hasText: '链路加载失败' })
  await expect(toast).toHaveCSS('transform', 'none')
  await toast.getByRole('button').focus()

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme }, colorScheme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await expect(toast).toBeInViewport()
      await expect(retry).toBeInViewport()
      const toastBounds = (await toast.boundingBox())!
      expect(toastBounds.width).toBe(288)
      expect(toastBounds.height).toBe(48)
      expect(await page.evaluate(() => Math.max(
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
        document.body.scrollWidth - document.body.clientWidth,
      ))).toBe(0)
      if (width === 1440 && process.env.TINKERFIN_VISUAL_QA_DIR) {
        await page.screenshot({
          path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, `feedback-system-${colorScheme}-1440.png`),
          fullPage: true,
        })
      }
    }
  }
  await toast.getByRole('button').click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await retry.focus()
  await retry.press('Enter')
  await expect(notifications.getByRole('alert')).toHaveText('链路加载失败')
  await expect(page.getByText('链路加载失败', { exact: true })).toHaveCount(1)
})

test('首页与会话态使用相同的输入卡片高度', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockStudio(page, { emptyHistory: true })
  const heroHeight = await page.locator('.composer').evaluate((element) => (
    element.getBoundingClientRect().height
  ))

  await page.unroute('**/api/**')
  await mockStudio(page)
  await expect(page.locator('.composer-dock')).not.toHaveClass(/is-hero/)
  const conversationHeight = await page.locator('.composer').evaluate((element) => (
    element.getBoundingClientRect().height
  ))

  expect(heroHeight).toBe(conversationHeight)
})

test('macOS Composer 支持 Control+U 且不接管 Command+U', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' })
  })
  await mockStudio(page, { emptyHistory: true })
  expect(await page.evaluate(() => navigator.platform)).toContain('Mac')
  const input = page.getByRole('textbox', { name: '消息输入' })

  await input.fill('第一行\n第二行内容')
  await input.evaluate((element) => {
    const textarea = element as HTMLTextAreaElement
    textarea.setSelectionRange(textarea.value.length, textarea.value.length)
  })
  await input.press('Control+u')
  await expect(input).toHaveValue('第一行\n')

  await input.fill('保留内容')
  await input.press('Meta+u')
  await expect(input).toHaveValue('保留内容')

  await input.fill('换行内容')
  await input.press('Shift+Enter')
  await expect(input).toHaveValue('换行内容\n')
})

test('Composer 在已有文本前插入 Slash 时保持光标并安全取消建议', async ({ page }) => {
  await mockStudio(page, { emptyHistory: true })
  const input = page.getByRole('textbox', { name: '消息输入' })
  const expectCaret = async (position: number) => {
    await expect.poll(() => input.evaluate((element) => (
      element as HTMLTextAreaElement
    ).selectionStart)).toBe(position)
  }
  await input.fill('已有内容')
  await input.evaluate((element) => {
    const textarea = element as HTMLTextAreaElement
    textarea.setSelectionRange(0, 0)
  })

  await input.press('/')
  await expect(input).toHaveValue('/已有内容')
  await expectCaret(1)
  await expect(page.getByRole('listbox', { name: '命令和技能建议' })).toBeVisible()

  await input.press('x')
  await expect(input).toHaveValue('/已有内容')
  await expectCaret(1)

  await input.press('Escape')
  await expect(input).toHaveValue('已有内容')
  await expectCaret(0)

  await input.press('a')
  await expect(input).toHaveValue('a已有内容')
  await expectCaret(1)

  await input.fill('已有内容')
  await input.evaluate((element) => {
    const textarea = element as HTMLTextAreaElement
    textarea.setSelectionRange(0, 0)
  })
  await input.press('/')
  await expect(page.getByRole('listbox', { name: '命令和技能建议' })).toBeVisible()
  await page.locator('.empty-conversation').click({ position: { x: 20, y: 20 } })
  await expect(page.getByRole('listbox', { name: '命令和技能建议' })).toHaveCount(0)
  await expect(input).toHaveValue('已有内容')
  await expect(input).toBeFocused()
  await expectCaret(0)
})

test('Plan 澄清按后端题型渲染单选、多选、文本与日期控件', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 900 })
  await mockStudio(page, { planQuestion: true })

  await expect(page.getByRole('radio', { name: 'Web' })).not.toBeFocused()
  const singleOptionBounds = await page.locator('.plan-question-option').first().boundingBox()
  if (!singleOptionBounds) throw new Error('单选项几何不可用')
  await expect(page.getByRole('button', { name: '浏览下一题' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '下一题', exact: true })).toBeDisabled()
  await page.getByRole('radio', { name: 'Web' }).click()
  await expect(page.getByRole('heading', { name: '需要覆盖哪些平台？' })).toBeVisible()
  await expect(page.getByRole('checkbox', { name: 'Web' })).not.toBeFocused()
  await expect(page.getByRole('button', { name: '浏览下一题' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '下一题', exact: true })).toBeDisabled()
  await page.getByRole('checkbox', { name: 'Web' }).check()
  await page.getByRole('textbox', { name: '自定义回答：需要覆盖哪些平台？' }).fill('桌面端')
  await expect(page.getByRole('checkbox', { name: '移动端' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '浏览下一题' })).toBeEnabled()
  const nextQuestion = page.getByRole('button', { name: '下一题', exact: true })
  await expect(nextQuestion).toBeEnabled()
  await expect(nextQuestion).toHaveClass(/ui-button--capsule/)
  await expect(nextQuestion).toHaveClass(/ui-button--primary/)
  await expect(nextQuestion).toHaveCSS('background-color', 'rgb(57, 100, 254)')
  const clarificationFooter = page.locator('.plan-question-composer-footer')
  const clarificationBody = page.locator('.plan-question-composer-body')
  const [footerVisual, clarificationBodyBounds, clarificationFooterBounds] = await Promise.all([
    clarificationFooter.evaluate((element) => ({
      background: getComputedStyle(element).backgroundColor,
      content: getComputedStyle(element, '::before').content,
      gradient: getComputedStyle(element, '::before').backgroundImage,
    })),
    clarificationBody.boundingBox(),
    clarificationFooter.boundingBox(),
  ])
  expect(footerVisual.background).toBe('rgba(0, 0, 0, 0)')
  expect(footerVisual.content).not.toBe('none')
  expect(footerVisual.gradient).toContain('linear-gradient')
  if (!clarificationBodyBounds || !clarificationFooterBounds) {
    throw new Error('Plan 澄清正文与 Footer 几何不可用')
  }
  expect(clarificationBodyBounds.y + clarificationBodyBounds.height)
    .toBeLessThanOrEqual(clarificationFooterBounds.y + .5)

  await nextQuestion.click()

  const text = page.getByRole('textbox', { name: '自定义回答：还有哪些限制？' })
  await expect(text).toBeVisible()
  await expect(text).not.toBeFocused()
  await expect(page.locator('.plan-question-custom')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await text.fill('必须覆盖离线状态')
  await page.getByRole('button', { name: '下一题', exact: true }).click()

  const date = page.getByLabel('日期回答：期望完成日期是什么时候？')
  await expect(date).toHaveAttribute('aria-haspopup', 'grid')
  await expect(date).not.toBeFocused()
  const [dateRowBounds, dateIconBounds, dateLabelBounds, dateInputBounds] = await Promise.all([
    page.locator('.plan-question-date').boundingBox(),
    page.locator('.plan-question-date > .plan-question-option-index').boundingBox(),
    page.locator('.plan-question-date-label').boundingBox(),
    date.boundingBox(),
  ])
  if (!dateRowBounds || !dateIconBounds || !dateLabelBounds || !dateInputBounds) {
    throw new Error('日期选项几何不可用')
  }
  const dateRowCenter = dateRowBounds.y + (dateRowBounds.height / 2)
  expect(dateRowBounds.height).toBeCloseTo(singleOptionBounds.height, 5)
  expect(dateInputBounds.height).toBeCloseTo(32, 5)
  for (const bounds of [dateIconBounds, dateLabelBounds, dateInputBounds]) {
    expect(Math.abs(bounds.y + (bounds.height / 2) - dateRowCenter)).toBeLessThanOrEqual(.5)
  }
  await page.locator('.plan-question-composer-head').hover()
  const initialDateBackground = await date.evaluate((element) => getComputedStyle(element).backgroundColor)
  await date.hover()
  await expect(date).toHaveCSS('cursor', 'pointer')
  await expect.poll(async () => (
    date.evaluate((element) => getComputedStyle(element).backgroundColor)
  )).not.toBe(initialDateBackground)
  await page.locator('.plan-question-composer-head').hover()
  await date.focus()
  await page.keyboard.press('Shift+Tab')
  await page.keyboard.press('Tab')
  await expect(date).toBeFocused()
  await expect(date).toHaveCSS('border-color', 'rgba(0, 0, 0, 0.16)')
  await date.click()
  const calendar = page.getByRole('application', { name: '选择日期' })
  await expect(calendar).toBeVisible()
  await expect(calendar).toHaveCSS('border-radius', '22px')
  await expect.poll(async () => {
    const [triggerBounds, calendarBounds] = await Promise.all([
      date.boundingBox(),
      calendar.boundingBox(),
    ])
    if (!triggerBounds || !calendarBounds) return Number.POSITIVE_INFINITY
    return Math.abs(
      triggerBounds.x + triggerBounds.width - calendarBounds.x - calendarBounds.width,
    )
  }).toBeLessThanOrEqual(1)
  await expect.poll(async () => {
    const [positionedTriggerBounds, positionedCalendarBounds] = await Promise.all([
      date.boundingBox(),
      calendar.boundingBox(),
    ])
    if (!positionedTriggerBounds || !positionedCalendarBounds) return false
    const calendarGap = positionedTriggerBounds.y - (
      positionedCalendarBounds.y + positionedCalendarBounds.height
    )
    return calendarGap >= 6.5 && calendarGap <= 8.5
  }).toBe(true)
  await expect(page.locator('.plan-question-date input[type="date"]')).toHaveCount(0)
  const nextMonthValue = await page.evaluate(() => {
    const now = new Date()
    const nextMonth = new Date(now.getFullYear(), now.getMonth() + 1, 1)
    return [
      String(nextMonth.getFullYear()).padStart(4, '0'),
      String(nextMonth.getMonth() + 1).padStart(2, '0'),
      '01',
    ].join('-')
  })
  const [targetYear, targetMonth] = nextMonthValue.split('-').map(Number)
  await calendar.getByRole('button', { name: '选择月份和年份' }).click()
  await expect(calendar.locator('[data-month-value]')).toHaveCount(12)
  await calendar.getByRole('button', { name: '选择年份' }).click()
  await calendar.locator(`[data-year-value="${targetYear}"]`).click()
  await calendar.locator(`[data-month-value="${targetMonth}"]`).click()
  await calendar.locator(`[data-date-value="${nextMonthValue}"]`).click()
  const expectedDisplay = await page.evaluate((dateValue) => {
    const [year, month, day] = dateValue.split('-').map(Number)
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date(year, month - 1, day))
  }, nextMonthValue)
  await expect(date).toContainText(expectedDisplay)
  await expect(date).toBeFocused()

  await page.getByRole('button', { name: '下一题', exact: true }).click()
  const time = page.getByRole('button', { name: '时间回答：期望几点上线？' })
  await expect(time).toHaveAttribute('aria-haspopup', 'dialog')
  await expect(page.locator('.plan-question-time select')).toHaveCount(0)
  await expect(page.getByRole('region', { name: '期望几点上线？' })).not.toContainText('时区')
  const timeBounds = await time.boundingBox()
  if (!timeBounds) throw new Error('时间选择器几何不可用')
  expect(timeBounds.height).toBeCloseTo(dateInputBounds.height, 5)
  await time.click()
  const timeDialog = page.getByRole('dialog', { name: '选择时间' })
  await expect(timeDialog).toBeVisible()
  await expect(timeDialog).toHaveCSS('border-radius', '22px')
  await expect.poll(async () => {
    const [triggerBounds, dialogBounds] = await Promise.all([
      time.boundingBox(),
      timeDialog.boundingBox(),
    ])
    if (!triggerBounds || !dialogBounds) return Number.POSITIVE_INFINITY
    return Math.abs(
      triggerBounds.x + triggerBounds.width - dialogBounds.x - dialogBounds.width,
    )
  }).toBeLessThanOrEqual(1)
  for (const [colorScheme, expectedBackground] of [
    ['light', 'rgb(255, 255, 255)'],
    ['dark', 'rgb(35, 35, 36)'],
  ] as const) {
    await page.emulateMedia({ colorScheme })
    await page.evaluate((theme) => {
      document.documentElement.dataset.theme = theme
    }, colorScheme)
    const footerOverlay = await page.locator('.plan-question-composer-footer').evaluate(
      (element) => getComputedStyle(element, '::before').backgroundImage,
    )
    expect(footerOverlay).toContain('linear-gradient')
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const closeNavigation = page.getByRole('button', { name: '关闭导航' })
      if (await closeNavigation.isVisible()) await closeNavigation.click()
      await expect(timeDialog).toHaveCSS('background-color', expectedBackground)
      const [responsiveTriggerBounds, responsiveDialogBounds] = await Promise.all([
        time.boundingBox(),
        timeDialog.boundingBox(),
      ])
      if (!responsiveTriggerBounds || !responsiveDialogBounds) {
        throw new Error('时间选择器响应式几何不可用')
      }
      expect(responsiveDialogBounds.x).toBeGreaterThanOrEqual(0)
      expect(responsiveDialogBounds.x + responsiveDialogBounds.width).toBeLessThanOrEqual(width)
      expect(responsiveDialogBounds.y).toBeGreaterThanOrEqual(0)
      expect(responsiveDialogBounds.y + responsiveDialogBounds.height).toBeLessThanOrEqual(900)
      expect(responsiveTriggerBounds.x).toBeGreaterThanOrEqual(0)
      expect(responsiveTriggerBounds.x + responsiveTriggerBounds.width).toBeLessThanOrEqual(width)
    }
  }
  await page.emulateMedia({ colorScheme: 'light' })
  await page.evaluate(() => {
    document.documentElement.dataset.theme = 'light'
  })
  await page.setViewportSize({ width: 1024, height: 900 })
  const hourList = page.getByRole('listbox', { name: '小时' })
  const minuteList = page.getByRole('listbox', { name: '分钟' })
  await hourList.getByRole('option', { name: '09' }).click()
  await minuteList.getByRole('option', { name: '30' }).click()
  await expect(time).toContainText('09:30')
  await expect(time).toBeFocused()

  await page.getByRole('button', { name: '下一题', exact: true }).click()
  const dateTimeRegion = page.getByRole('region', { name: '回滚截止点是什么时候？' })
  await expect(dateTimeRegion).not.toContainText('时区')
  await expect(dateTimeRegion).not.toContainText('允许范围')
  const dateTimeDate = page.getByRole('button', { name: '日期回答：回滚截止点是什么时候？' })
  const dateTimeTime = page.getByRole('button', { name: '时间回答：回滚截止点是什么时候？' })
  await expect(dateTimeTime).toBeDisabled()
  await dateTimeDate.click()
  await page.locator('[data-date-value="2026-09-01"]').click()
  await expect(dateTimeTime).toBeEnabled()
  await expect(dateTimeTime).toContainText('09:00')
  await dateTimeTime.click()
  await page.getByRole('listbox', { name: '小时' }).getByRole('option', { name: '09' }).click()
  await page.getByRole('listbox', { name: '分钟' }).getByRole('option', { name: '30' }).click()
  await expect(dateTimeTime).toContainText('09:30')
  await expect(page.locator('.plan-question-datetime select')).toHaveCount(0)
})

test('Plan 澄清切换到长多选题后 Tab 从首项按视觉顺序移动', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 900 })
  await mockStudio(page, { planQuestion: true, planQuestionForm: planQuestionTabOrderForm })

  await page.getByRole('radio', { name: '继续 1' }).click()
  await page.getByRole('radio', { name: '继续 2' }).click()
  await expect(page.getByText('3 / 8')).toBeVisible()

  const options = page.getByRole('checkbox')
  await expect(options).toHaveCount(8)
  await page.keyboard.press('Tab')
  await expect(options.nth(0)).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(options.nth(1)).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(options.nth(0)).toBeFocused()

  await options.nth(7).click()
  await page.getByRole('button', { name: '下一题', exact: true }).click()
  await expect(page.getByRole('heading', { name: '后续问题 1' })).toBeVisible()
  await page.getByRole('button', { name: '浏览上一题' }).click()
  await expect(page.getByText('3 / 8')).toBeVisible()
  await expect(options.nth(0)).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(options.nth(1)).toBeFocused()
})

test('Plan 澄清返回已作答单选题时保持选中项焦点且移出悬浮项会复原', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 900 })
  await mockStudio(page, { planQuestion: true })

  await page.getByRole('radio', { name: '移动端' }).click()
  await expect(page.getByRole('heading', { name: '需要覆盖哪些平台？' })).toBeVisible()
  await page.getByRole('button', { name: '浏览上一题' }).click()

  const web = page.getByRole('radio', { name: 'Web' })
  const mobile = page.getByRole('radio', { name: '移动端' })
  await expect(mobile).toBeFocused()
  await expect(mobile.locator('.plan-question-option-index svg')).toBeVisible()
  for (const [colorScheme, hoverBackground, focusBackground] of [
    ['light', 'rgb(241, 243, 245)', 'rgb(235, 238, 242)'],
    ['dark', 'rgb(44, 44, 46)', 'rgb(53, 54, 56)'],
  ] as const) {
    await page.emulateMedia({ colorScheme })
    await page.evaluate((theme) => {
      document.documentElement.dataset.theme = theme
    }, colorScheme)
    const stableFooterOverlay = await page.locator('.plan-question-composer-footer').evaluate(
      (element) => getComputedStyle(element, '::before').backgroundImage,
    )
    expect(stableFooterOverlay).toContain('linear-gradient')

    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await mobile.focus()
      await page.keyboard.press('Tab')
      await page.keyboard.press('Shift+Tab')
      await expect(mobile).toBeFocused()
      await expect(mobile).toHaveCSS('outline-style', 'none')
      await expect(mobile).toHaveCSS('background-color', focusBackground)
      await web.hover()
      await expect(web).toHaveCSS('background-color', hoverBackground)
      await page.locator('.plan-question-toggle-surface').hover({ position: { x: 20, y: 20 } })
      await expect(web).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
      await expect(mobile).toBeFocused()
      const overflow = await page.evaluate(() => Math.max(
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
        document.body.scrollWidth - document.body.clientWidth,
      ))
      expect(overflow).toBeLessThanOrEqual(0)
    }
  }

  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' })
  await expect(web).toHaveCSS('transition-duration', '0s')
})

test('收起的 Plan 澄清和标准输入框同高同宽且内部布局同步', async ({ page }) => {
  type Geometry = {
    surface: { x: number; y: number; width: number; height: number }
    header?: { x: number; y: number; width: number; height: number }
    heading?: { x: number; y: number; width: number; height: number }
    title?: { x: number; y: number; width: number; height: number }
    actions?: { x: number; y: number; width: number; height: number }
    progress?: { x: number; y: number; width: number; height: number }
    progressBar?: { x: number; y: number; width: number; height: number }
  }
  const collectGeometry = async ({
    surfaceSelector,
    headerSelector,
    headingSelector,
    titleSelector,
    actionsSelector,
    progressSelector,
    progressBarSelector,
  }: {
    surfaceSelector: string
    headerSelector?: string
    headingSelector?: string
    titleSelector?: string
    actionsSelector?: string
    progressSelector?: string
    progressBarSelector?: string
  }) => {
    const result = new Map<string, Geometry>()
    for (const colorScheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme })
      await page.evaluate((theme) => {
        document.documentElement.dataset.theme = theme
      }, colorScheme)
      for (const width of [320, 768, 1024, 1440]) {
        await page.setViewportSize({ width, height: 900 })
        const closeNavigation = page.getByRole('button', { name: '关闭导航' })
        if (await closeNavigation.isVisible()) await closeNavigation.click()
        const readBounds = async (selector?: string) => selector
          ? page.locator(selector).first().boundingBox()
          : undefined
        const [surface, header, heading, title, actions, progress, progressBar] = await Promise.all([
          readBounds(surfaceSelector),
          readBounds(headerSelector),
          readBounds(headingSelector),
          readBounds(titleSelector),
          readBounds(actionsSelector),
          readBounds(progressSelector),
          readBounds(progressBarSelector),
        ])
        if (!surface) throw new Error(`缺少 ${surfaceSelector} 收起态几何`)
        result.set(`${colorScheme}-${width}`, {
          surface,
          header: header ?? undefined,
          heading: heading ?? undefined,
          title: title ?? undefined,
          actions: actions ?? undefined,
          progress: progress ?? undefined,
          progressBar: progressBar ?? undefined,
        })
      }
    }
    return result
  }

  await mockStudio(page)
  const normalGeometry = await collectGeometry({ surfaceSelector: '.composer' })

  await page.unroute('**/api/**')
  await mockStudio(page, { planQuestion: true })
  await page.locator('.plan-question-toggle-surface').click()
  const questionGeometry = await collectGeometry({
    surfaceSelector: '.plan-question-composer',
    headerSelector: '.plan-question-composer-head',
    headingSelector: '.plan-question-composer-heading',
    titleSelector: '.plan-question-composer-heading h2 > span',
    actionsSelector: '.plan-question-composer-head-actions',
    progressSelector: '.plan-question-progress',
    progressBarSelector: '.plan-question-progress-step.is-current > span',
  })

  for (const [key, normal] of normalGeometry) {
    for (const geometry of [questionGeometry.get(key)]) {
      if (!geometry) throw new Error(`缺少 ${key} 收起态几何`)
      expect(geometry.surface.width).toBeCloseTo(normal.surface.width, 5)
      expect(geometry.surface.height).toBeCloseTo(normal.surface.height, 5)
    }

    const question = questionGeometry.get(key)!
    if (!question.title) throw new Error(`缺少 ${key} 卡片标题几何`)

    if (!question.header || !question.heading || !question.actions || !question.progress || !question.progressBar) {
      throw new Error(`缺少 ${key} 澄清卡片内部几何`)
    }
    const questionHeaderCenter = question.header.y + (question.header.height / 2)
    expect(Math.abs(
      question.heading.y + (question.heading.height / 2) - questionHeaderCenter,
    )).toBeLessThanOrEqual(1)
    expect(Math.abs(
      question.actions.y + (question.actions.height / 2) - questionHeaderCenter,
    )).toBeLessThanOrEqual(1)
    expect(question.header.height + question.progress.height)
      .toBeCloseTo(question.surface.height, 5)
    expect(question.surface.y + question.surface.height - (
      question.progress.y + question.progress.height
    )).toBeCloseTo(0, 5)
  }
})

test('展开的审批、Plan 澄清与草稿使用统一单行标题规格和静态渐变', async ({ page }) => {
  type HeaderChrome = {
    headerHeight: number
    headingCenterDelta: number
    actionsCenterDelta: number | null
    descriptionCenterDelta: number | null
    titleFontSize: string
    titleLineHeight: string
    titleWeight: string
    descriptionFontSize: string | null
    descriptionLineHeight: string | null
    hasDescription: boolean
    bridgeHeight: number
    bridgePointerEvents: string
    bridgeBackground: string
    footerOverlayContent: string
    footerOverlayBackground: string
    bodyFooterOverlap: number
    footerInsideBody: boolean
  }
  const collectChrome = async ({
    cardSelector,
    headerSelector,
    actionsSelector,
  }: {
    cardSelector: string
    headerSelector: string
    actionsSelector?: string
  }) => {
    const result = new Map<string, HeaderChrome>()
    for (const colorScheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme })
      await page.evaluate((theme) => {
        document.documentElement.dataset.theme = theme
      }, colorScheme)
      for (const width of [320, 768, 1024, 1440]) {
        await page.setViewportSize({ width, height: 900 })
        const closeNavigation = page.getByRole('button', { name: '关闭导航' })
        if (await closeNavigation.isVisible()) await closeNavigation.click()
        const chrome = await page.locator(cardSelector).evaluate((card, selectors) => {
          const header = card.querySelector<HTMLElement>(selectors.header)
          const bridge = card.querySelector<HTMLElement>('.interaction-card-color-bridge')
          const body = card.querySelector<HTMLElement>(
            ':scope > :is(.approval-composer-body, .plan-question-composer-body, .plan-review-composer-body)',
          )
          const footer = card.querySelector<HTMLElement>(
            ':is(.approval-composer-footer, .plan-question-composer-footer, .plan-review-composer-footer)',
          )
          if (!header || !bridge || !body || !footer) {
            throw new Error('卡片标题与 Footer 渐变几何不可用')
          }
          const title = header.querySelector<HTMLElement>('h2')
          const description = header.querySelector<HTMLElement>('.plan-interaction-card-description')
          const actions = selectors.actions
            ? header.querySelector<HTMLElement>(selectors.actions)
            : null
          const heading = title?.parentElement
          if (!title || !heading || (selectors.actions && !actions)) throw new Error('卡片标题渐变几何不可用')
          const headerBounds = header.getBoundingClientRect()
          const headingBounds = heading.getBoundingClientRect()
          const actionsBounds = actions?.getBoundingClientRect()
          const bridgeBounds = bridge.getBoundingClientRect()
          const titleStyle = getComputedStyle(title)
          const descriptionBounds = description?.getBoundingClientRect()
          const descriptionStyle = description ? getComputedStyle(description) : null
          const bridgeStyle = getComputedStyle(bridge)
          const bodyBounds = body.getBoundingClientRect()
          const footerBounds = footer.getBoundingClientRect()
          const footerOverlayStyle = getComputedStyle(footer, '::before')
          return {
            headerHeight: headerBounds.height,
            headingCenterDelta: headingBounds.top + (headingBounds.height / 2)
              - headerBounds.top - (headerBounds.height / 2),
            actionsCenterDelta: actionsBounds
              ? actionsBounds.top + (actionsBounds.height / 2)
                - headerBounds.top - (headerBounds.height / 2)
              : null,
            descriptionCenterDelta: descriptionBounds
              ? descriptionBounds.top + (descriptionBounds.height / 2)
                - headerBounds.top - (headerBounds.height / 2)
              : null,
            titleFontSize: titleStyle.fontSize,
            titleLineHeight: titleStyle.lineHeight,
            titleWeight: titleStyle.fontWeight,
            descriptionFontSize: descriptionStyle?.fontSize ?? null,
            descriptionLineHeight: descriptionStyle?.lineHeight ?? null,
            hasDescription: Boolean(description),
            bridgeHeight: bridgeBounds.height,
            bridgePointerEvents: bridgeStyle.pointerEvents,
            bridgeBackground: bridgeStyle.backgroundImage,
            footerOverlayContent: footerOverlayStyle.content,
            footerOverlayBackground: footerOverlayStyle.backgroundImage,
            bodyFooterOverlap: bodyBounds.bottom - footerBounds.top,
            footerInsideBody: body.contains(footer),
          }
        }, { header: headerSelector, actions: actionsSelector })
        result.set(`${colorScheme}-${width}`, chrome)
      }
    }
    return result
  }

  await mockStudio(page, { approval: true })
  const approvalChrome = await collectChrome({
    cardSelector: '.approval-composer',
    headerSelector: '.approval-composer-head',
  })
  await page.unroute('**/api/**')
  await mockStudio(page, { planQuestion: true })
  const questionChrome = await collectChrome({
    cardSelector: '.plan-question-composer',
    headerSelector: '.plan-question-composer-head',
    actionsSelector: '.plan-question-composer-head-actions',
  })
  await page.unroute('**/api/**')
  await mockStudio(page, { planReview: true })
  const reviewChrome = await collectChrome({
    cardSelector: '.plan-review-composer',
    headerSelector: '.plan-review-composer-head',
    actionsSelector: '.plan-review-composer-head-actions',
  })

  for (const [key, approval] of approvalChrome) {
    const question = questionChrome.get(key)
    const review = reviewChrome.get(key)
    if (!question || !review) throw new Error(`缺少 ${key} 卡片标题渐变数据`)
    for (const chrome of [approval, question, review]) {
      expect(Math.abs(chrome.headingCenterDelta)).toBeLessThanOrEqual(1)
      if (chrome.actionsCenterDelta != null) {
        expect(Math.abs(chrome.actionsCenterDelta)).toBeLessThanOrEqual(1)
      }
      expect(chrome.titleFontSize).toBe('14px')
      expect(chrome.titleLineHeight).toBe('24px')
      expect(chrome.titleWeight).toBe('400')
      expect(chrome.bridgeHeight).toBeCloseTo(12, 5)
      expect(chrome.bridgePointerEvents).toBe('none')
      expect(chrome.bridgeBackground).toContain('linear-gradient')
      expect(chrome.footerOverlayContent).not.toBe('none')
      expect(chrome.footerOverlayBackground).toContain('linear-gradient')
    }
    for (const chrome of [approval, question, review]) {
      expect(chrome.headerHeight).toBeCloseTo(44, 5)
    }
    for (const chrome of [question]) {
      expect(Math.abs(chrome.descriptionCenterDelta ?? Number.POSITIVE_INFINITY))
        .toBeLessThanOrEqual(1)
      expect(chrome.descriptionFontSize).toBe('13px')
      expect(chrome.descriptionLineHeight).toBe('22px')
      expect(chrome.hasDescription).toBe(true)
    }
    for (const chrome of [approval, review]) {
      expect(chrome.headerHeight).toBeCloseTo(44, 5)
      expect(chrome.hasDescription).toBe(false)
      expect(chrome.descriptionCenterDelta).toBeNull()
    }
    expect(approval.actionsCenterDelta).toBeNull()
    expect(approval.footerInsideBody).toBe(false)
    expect(question.footerInsideBody).toBe(false)
    expect(review.footerInsideBody).toBe(true)
    expect(approval.bodyFooterOverlap).toBeLessThanOrEqual(.5)
    expect(question.bodyFooterOverlap).toBeLessThanOrEqual(.5)
    expect(review.bodyFooterOverlap).toBeGreaterThan(0)
    expect(Math.abs(review.actionsCenterDelta ?? Number.POSITIVE_INFINITY))
      .toBeLessThanOrEqual(1)
  }
})

test('展开的 Plan 澄清与草稿在四个视口保持可滚动且避开等待状态', async ({ page }) => {
  const viewports = [
    { width: 320, height: 640 },
    { width: 768, height: 900 },
    { width: 1024, height: 900 },
    { width: 1440, height: 900 },
  ]
  for (const mode of ['question', 'review'] as const) {
    await page.unroute('**/api/**')
    await mockStudio(page, mode === 'question' ? { planQuestion: true } : { planReview: true })
    const card = page.locator(
      mode === 'question' ? '.plan-question-composer' : '.plan-review-composer',
    )
    const body = card.locator(
      mode === 'question' ? '.plan-question-composer-body' : '.plan-review-composer-body',
    )
    for (const viewport of viewports) {
      await page.setViewportSize(viewport)
      const closeNavigation = page.getByRole('button', { name: '关闭导航' })
      if (await closeNavigation.isVisible()) await closeNavigation.click()
      const [cardBounds, bodyMetrics, dockBounds] = await Promise.all([
        card.boundingBox(),
        body.evaluate((element) => ({
          clientHeight: element.clientHeight,
          scrollHeight: element.scrollHeight,
        })),
        page.locator('.composer-dock').boundingBox(),
      ])
      if (!cardBounds || !dockBounds) throw new Error('Plan 卡片响应式几何不可用')
      expect(cardBounds.width).toBeGreaterThan(0)
      expect(cardBounds.y).toBeGreaterThanOrEqual(0)
      expect(cardBounds.y + cardBounds.height).toBeLessThanOrEqual(viewport.height)
      expect(bodyMetrics.clientHeight).toBeGreaterThan(0)
      expect(bodyMetrics.scrollHeight).toBeGreaterThanOrEqual(bodyMetrics.clientHeight)
      expect(cardBounds.y).toBeGreaterThanOrEqual(dockBounds.y)
    }
  }
})

test('Tool 审批按独立卡片顺序接管输入区且不暴露折叠或拖拽入口', async ({ page }) => {
  await mockStudio(page, { approval: true })
  const card = page.getByRole('region', { name: '等待审批' })
  await expect(card).toContainText('/first-approval.txt')
  await expect(card.locator('.approval-toggle-surface')).toHaveCount(0)
  await expect(page.getByRole('separator', { name: '调整交互卡片高度' })).toHaveCount(0)

  await card.getByRole('button', { name: '允许' }).click()
  await expect(card).toContainText('/second-approval.txt')
  const reject = card.getByRole('button', { name: '拒绝' })
  await expect(reject).toBeFocused()
  await reject.click()
  const reason = card.getByRole('textbox', { name: '拒绝原因（可选）' })
  await expect(reason).toBeFocused()
  await card.getByRole('button', { name: '取消', exact: true }).click()
  await expect(reject).toBeFocused()

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(card).toBeVisible()
  await page.emulateMedia({ forcedColors: 'active' })
  await expect(card.locator('.approval-status-dot')).toBeVisible()
})

test('HITL 始终展开并忽略旧的会话级收起缓存', async ({ page }) => {
  const collapseKey = `tinkerfin:approval-collapse:${THREAD_ID}`
  await page.addInitScript((key) => {
    window.sessionStorage.setItem(key, 'collapsed')
  }, collapseKey)
  await mockStudio(page, { approval: true })
  const card = page.getByRole('region', { name: '等待审批' })
  await expect(card).not.toHaveClass(/is-minimized/)
  await expect(card.getByRole('button', { name: '允许' })).toBeVisible()
  await expect(card.getByRole('button', { name: /展开审批卡片|收起审批卡片/ })).toHaveCount(0)
  await expect(page.getByRole('separator', { name: '调整交互卡片高度' })).toHaveCount(0)

  await page.reload()
  await expect(card).toBeVisible()
  await expect(card).not.toHaveClass(/is-minimized/)
  await expect(card.getByRole('button', { name: '允许' })).toBeVisible()
  await expect(card.getByRole('button', { name: /展开审批卡片|收起审批卡片/ })).toHaveCount(0)
  await expect(page.getByRole('separator', { name: '调整交互卡片高度' })).toHaveCount(0)
})

test('待审批会话忽略缓存位置一次到底且不锁住后续滚动', async ({ page }) => {
  await page.addInitScript((key) => {
    window.sessionStorage.setItem(key, JSON.stringify({
      scrollTop: 120,
      followLatest: false,
    }))
  }, `tinkerfin:conversation-scroll:${THREAD_ID}`)
  await mockStudio(page, {
    approval: true,
    conversationMessages: approvalScrollMessages,
  })

  const pane = page.getByRole('region', { name: '对话内容' })
  await expect(page.locator('.approval-wait-state > .activity-dots')).toBeVisible()
  await expect.poll(async () => pane.evaluate((element) => (
    element.scrollHeight - element.scrollTop - element.clientHeight
  ))).toBeLessThanOrEqual(1)
  expect(await pane.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)

  await pane.evaluate((element) => {
    element.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -120 }))
    element.scrollTop = 120
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  await expect.poll(async () => pane.evaluate((element) => element.scrollTop)).toBe(120)

  expect(await pane.evaluate((element) => element.scrollTop)).toBe(120)
})

test('普通用户与回答使用角色化节奏且卡片边界保持16px', async ({ page }) => {
  await mockStudio(page, {
    conversationMessages: spacingAuditMessages,
    expectedMessageText: '普通文本 B',
  })
  const locators = {
    userA: page.locator('#spacing-user-a .message-markdown'),
    userAAction: page.locator('#spacing-user-a .message-action-row--user'),
    userACopy: page.locator('#spacing-user-a .message-action-row--user .ui-icon-button'),
    assistantAMarkdown: page.locator('#spacing-assistant-a .message-markdown'),
    assistantABody: page.locator('#spacing-assistant-a .message-markdown > :first-child'),
    assistantASecondParagraph: page.locator('#spacing-assistant-a .message-markdown > p').nth(1),
    assistantA: page.locator('#spacing-assistant-a'),
    assistantAAction: page.locator('#spacing-assistant-a .message-action-row--assistant'),
    assistantACopy: page.locator('#spacing-assistant-a .message-action-row .ui-icon-button'),
    userB: page.locator('#spacing-user-b .message-markdown'),
    userBAction: page.locator('#spacing-user-b .message-action-row--user'),
    subagent: page.locator('#spacing-subagent'),
    subagentTerminal: page.locator('#spacing-subagent .subagent-output-node'),
    tool: page.locator('#spacing-tool'),
    toolDetail: page.locator('#spacing-tool .tool-detail-card'),
    assistantBMarkdown: page.locator('#spacing-assistant-b .message-markdown'),
    assistantBBody: page.locator('#spacing-assistant-b .message-markdown > :first-child'),
    assistantB: page.locator('#spacing-assistant-b'),
    assistantBCopy: page.locator('#spacing-assistant-b .message-action-row--assistant .ui-icon-button'),
    userC: page.locator('#spacing-user-c .message-markdown'),
    userCAction: page.locator('#spacing-user-c .message-action-row--user'),
    userD: page.locator('#spacing-user-d .message-markdown'),
  }
  const gap = (before: { y: number; height: number }, after: { y: number }) => (
    after.y - (before.y + before.height)
  )

  await page.locator('#spacing-subagent > summary').click()
  await page.locator('#spacing-tool > summary').click()

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const entries = Object.entries(locators)
      const bounds = Object.fromEntries(await Promise.all(entries.map(async ([name, locator]) => {
        const value = await locator.boundingBox()
        if (!value) throw new Error('缺少 ' + name + ' 几何')
        return [name, value]
      }))) as Record<keyof typeof locators, { x: number; y: number; height: number }>

      expect(gap(bounds.userA, bounds.userAAction)).toBeCloseTo(0, 5)
      expect(gap(bounds.userA, bounds.assistantABody)).toBeCloseTo(40, 5)
      expect(gap(bounds.userAAction, bounds.assistantAMarkdown)).toBeCloseTo(0, 5)
      expect(gap(bounds.assistantABody, bounds.assistantASecondParagraph)).toBeCloseTo(16, 5)
      expect(gap(bounds.assistantAMarkdown, bounds.assistantAAction)).toBeCloseTo(0, 5)
      expect(gap(bounds.assistantAMarkdown, bounds.userB)).toBeCloseTo(94, 5)
      expect(gap(bounds.assistantA, bounds.userB)).toBeCloseTo(48, 5)
      expect(gap(bounds.assistantACopy, bounds.userB)).toBeCloseTo(57, 5)
      expect(gap(bounds.userB, bounds.subagent)).toBeCloseTo(56, 5)
      expect(gap(bounds.userBAction, bounds.subagent)).toBeCloseTo(16, 5)
      expect(gap(bounds.subagent, bounds.tool)).toBeCloseTo(16, 5)
      expect(gap(bounds.subagentTerminal, bounds.tool)).toBeCloseTo(16, 5)
      expect(gap(bounds.tool, bounds.assistantBBody)).toBeCloseTo(16, 5)
      expect(gap(bounds.toolDetail, bounds.assistantBBody)).toBeCloseTo(16, 5)
      expect(gap(bounds.assistantBMarkdown, bounds.userC)).toBeCloseTo(94, 5)
      expect(gap(bounds.assistantB, bounds.userC)).toBeCloseTo(48, 5)
      expect(gap(bounds.assistantBCopy, bounds.userC)).toBeCloseTo(57, 5)
    }
  }
})

test('用户复制操作悬浮与聚焦显隐不改变消息几何', async ({ page }) => {
  await mockStudio(page, {
    conversationMessages: spacingAuditMessages,
    expectedMessageText: '普通文本 B',
  })
  await page.setViewportSize({ width: 1024, height: 900 })
  expect(await page.evaluate(() => matchMedia('(hover: hover) and (pointer: fine)').matches)).toBe(true)

  const message = page.locator('#spacing-user-a')
  const bubble = message.locator('.message-markdown')
  const action = message.locator('.message-action-row--user')
  const button = action.locator('.ui-icon-button')
  const icon = button.locator('svg')
  const answer = page.locator('#spacing-assistant-a .message-markdown')

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    await page.getByRole('region', { name: '对话内容' }).focus()
    await page.locator('.chat-header').hover()
    await bubble.scrollIntoViewIfNeeded()
    await expect(action).toHaveCSS('opacity', '0')
    await expect(action).toHaveCSS('pointer-events', 'none')

    const [bubbleBefore, actionBefore, buttonBefore, iconBefore, answerBefore] = await Promise.all([
      bubble.boundingBox(),
      action.boundingBox(),
      button.boundingBox(),
      icon.boundingBox(),
      answer.boundingBox(),
    ])
    if (!bubbleBefore || !actionBefore || !buttonBefore || !iconBefore || !answerBefore) {
      throw new Error('用户复制操作几何不可用')
    }
    expect(bubbleBefore.height).toBeCloseTo(44, 5)
    expect(actionBefore.height).toBeCloseTo(40, 5)
    expect(actionBefore.y).toBeCloseTo(bubbleBefore.y + bubbleBefore.height, 5)
    expect(buttonBefore.width).toBeCloseTo(32, 5)
    expect(buttonBefore.height).toBeCloseTo(32, 5)
    expect(buttonBefore.y - actionBefore.y).toBeCloseTo(4, 5)
    expect(buttonBefore.x + buttonBefore.width).toBeCloseTo(bubbleBefore.x + bubbleBefore.width, 5)
    expect(iconBefore.width).toBeCloseTo(20, 5)
    expect(iconBefore.height).toBeCloseTo(20, 5)
    expect(answerBefore.y - (bubbleBefore.y + bubbleBefore.height)).toBeCloseTo(40, 5)

    await bubble.hover()
    await expect(action).toHaveCSS('transition-duration', '0.3s')
    await expect(action).toHaveCSS('transition-delay', '0.3s')
    await expect(action).toHaveCSS('transition-timing-function', 'cubic-bezier(0.4, 0, 0.2, 1)')
    await expect(action).toHaveCSS('opacity', '1')
    await expect(action).toHaveCSS('pointer-events', 'auto')

    const [bubbleAfter, actionAfter, answerAfter] = await Promise.all([
      bubble.boundingBox(),
      action.boundingBox(),
      answer.boundingBox(),
    ])
    expect(bubbleAfter).toEqual(bubbleBefore)
    expect(actionAfter).toEqual(actionBefore)
    expect(answerAfter).toEqual(answerBefore)

    await page.locator('.chat-header').hover()
    await expect(action).toHaveCSS('opacity', '0')
    await button.focus()
    await expect(action).toHaveCSS('transition-duration', '0s')
    await expect(action).toHaveCSS('opacity', '1')
    await expect(action).toHaveCSS('pointer-events', 'auto')
  }

  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' })
  await bubble.hover()
  await expect(action).toHaveCSS('transition-duration', '0s')
})

test('文章型 Markdown 使用参考排版且表格保持可滚动', async ({ page }) => {
  await mockStudio(page, {
    conversationMessages: markdownLayoutMessages,
    expectedMessageText: '检查完整 Markdown 排版',
  })
  const markdown = page.locator('#markdown-layout-assistant .message-markdown')
  const userBubble = page.locator('#markdown-layout-user .message-markdown')
  const composer = page.locator('.composer-default:not(.is-taken-over) .composer')

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1200 })
      const layout = await markdown.evaluate((root) => {
        const element = (selector: string) => {
          const value = root.querySelector<HTMLElement>(selector)
          if (!value) throw new Error('缺少 Markdown 元素 ' + selector)
          return value
        }
        const style = (selector: string) => getComputedStyle(element(selector))
        const rect = (selector: string) => element(selector).getBoundingClientRect()
        const paragraphs = root.querySelectorAll<HTMLElement>(':scope > p')
        if (paragraphs.length < 2) throw new Error('缺少连续 Markdown 段落')
        const firstParagraph = paragraphs[0]!.getBoundingClientRect()
        const secondParagraph = paragraphs[1]!.getBoundingClientRect()
        const rootBounds = root.getBoundingClientRect()
        const tableWrapBounds = rect('.markdown-table-wrap')
        const h1 = style('h1')
        const h2 = style('h2')
        const h3 = style('h3')
        const h4 = style('h4')
        const h5 = style('h5')
        const h6 = style('h6')
        const list = style('ul')
        const quote = style('blockquote')
        const inlineCode = style('p code')
        const codeBlock = style('.markdown-code-block')
        const code = style('.markdown-code-block pre')
        const tableWrap = style('.markdown-table-wrap')
        const table = style('table')
        const th = style('th')
        const td = style('td')
        const hr = style('hr')
        return {
          root: [getComputedStyle(root).fontSize, getComputedStyle(root).lineHeight],
          paragraphGap: secondParagraph.top - firstParagraph.bottom,
          h1: [h1.fontSize, h1.lineHeight, h1.fontWeight, h1.marginTop, h1.marginBottom],
          h2: [h2.fontSize, h2.lineHeight, h2.fontWeight, h2.marginTop, h2.marginBottom],
          h3: [h3.fontSize, h3.lineHeight, h3.fontWeight, h3.marginTop, h3.marginBottom],
          h4: [h4.fontSize, h4.lineHeight, h4.fontWeight, h4.marginTop, h4.marginBottom],
          h5: [h5.fontSize, h5.lineHeight, h5.fontWeight, h5.marginTop, h5.marginBottom],
          h6: [h6.fontSize, h6.lineHeight, h6.fontWeight, h6.marginTop, h6.marginBottom],
          list: [list.fontSize, list.lineHeight, list.margin, list.paddingInlineStart],
          quote: [quote.lineHeight, quote.marginBottom, quote.paddingTop, quote.paddingRight, quote.paddingBottom, quote.paddingLeft, quote.borderLeftWidth],
          inlineCode: [inlineCode.fontSize, inlineCode.lineHeight, inlineCode.paddingTop, inlineCode.paddingRight, inlineCode.paddingBottom, inlineCode.paddingLeft, inlineCode.borderRadius],
          code: [code.fontSize, code.lineHeight, codeBlock.marginTop, codeBlock.borderRadius],
          tableWrap: [tableWrap.overflowX, tableWrapBounds.width - rootBounds.width],
          table: [table.fontSize, table.lineHeight],
          th: [th.fontSize, th.lineHeight, th.fontWeight, th.paddingTop, th.paddingRight, th.paddingBottom, th.paddingLeft, th.maxWidth],
          td: [td.fontSize, td.lineHeight, td.fontWeight, td.paddingTop, td.paddingRight, td.paddingBottom, td.paddingLeft, td.maxWidth],
          hr: [hr.marginTop, hr.marginBottom],
        }
      })

      expect(layout.root).toEqual(['16px', '26px'])
      expect(layout.paragraphGap).toBeCloseTo(16, 5)
      expect(layout.h1).toEqual(['24px', '32px', '600', '0px', '8px'])
      expect(layout.h2).toEqual(['20px', '28px', '600', '16px', '4px'])
      expect(layout.h3).toEqual(['18px', '28px', '600', '16px', '4px'])
      expect(layout.h4).toEqual(['16px', '24px', '600', '0px', '0px'])
      expect(layout.h5).toEqual(['16px', '26px', '600', '0px', '0px'])
      expect(layout.h6).toEqual(['16px', '26px', '400', '0px', '0px'])
      expect(layout.list).toEqual(['16px', '26px', '0px', '26px'])
      expect(layout.quote).toEqual(['24px', '8px', '8px', '0px', '8px', '24px', '0px'])
      expect(layout.inlineCode).toEqual(['14px', '26px', '2.4px', '4.8px', '2.4px', '4.8px', '4px'])
      expect(layout.code).toEqual(['14px', '24px', '8px', '22px'])
      await expect(userBubble).toHaveCSS('border-radius', '22px')
      await expect(composer).toHaveCSS('border-radius', '22px')
      expect(layout.tableWrap[0]).toBe('auto')
      expect(Number(layout.tableWrap[1])).toBeCloseTo(32, 5)
      expect(layout.table).toEqual(['14px', '24px'])
      expect(layout.th).toEqual(['14px', '16px', '600', '8px', '24px', '8px', '0px', '160px'])
      expect(layout.td).toEqual(['14px', '24px', '400', '10px', '24px', '10px', '0px', '160px'])
      expect(layout.hr).toEqual(['28px', '28px'])
    }
  }
})

test('同批 Todos 与单个普通 Tool 保持公共中间间距', async ({ page }) => {
  const selectedTodo: Message = {
    id: 'single-batch-todos',
    role: 'tool',
    content: 'write_todos',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'write_todos',
      toolCallId: 'single-batch-todos',
      runId: 'single-batch-run',
      batchId: 'single-batch',
      status: 'completed',
    },
  }
  const singleTool: Message = {
    id: 'single-batch-list',
    role: 'tool',
    content: 'ls',
    createdAt: BASE_TIME,
    meta: {
      toolName: 'ls',
      toolCallId: 'single-batch-list',
      runId: 'single-batch-run',
      batchId: 'single-batch',
      params: '{"path":"/memories"}',
      result: '读取完成',
      status: 'completed',
    },
  }
  await mockStudio(page, {
    conversationMessages: [selectedTodo, singleTool],
    taskTrace: {
      status: 'ready',
      todoGroups: [{
        id: 'todo-group:single-batch-run',
        userMessageId: 'single-batch-user',
        userMessagePreview: '整理记忆文件',
        groupToolCallId: selectedTodo.id,
        createdAt: BASE_TIME,
        status: 'completed',
        todos: [{ id: 'single-batch-todo-1', content: '整理记忆文件', status: 'completed' }],
      }],
    },
  })
  const todoSummary = page.locator('#single-batch-todos > summary')
  const toolSummary = page.locator('#single-batch-list > summary')

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const [todoBounds, toolBounds] = await Promise.all([
        todoSummary.boundingBox(),
        toolSummary.boundingBox(),
      ])
      if (!todoBounds || !toolBounds) throw new Error('Todos 或单个 Tool 几何不可用')
      expect(toolBounds.y - (todoBounds.y + todoBounds.height)).toBeCloseTo(16, 5)
    }
  }
})

test('展开 Tool 批次在卡片边框之间保持公共间距', async ({ page }) => {
  await mockStudio(page, {
    conversationMessages: spacingBatchMessages,
    expectedMessageText: '批量读取完成',
  })
  const cards = page.locator('.tool-batch > .tool-card')
  await cards.nth(0).locator('summary').click()
  await cards.nth(1).locator('summary').click()

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const [firstDetail, secondHeader, secondDetail, answer] = await Promise.all([
        cards.nth(0).locator('.tool-detail-card').boundingBox(),
        cards.nth(1).locator('summary').boundingBox(),
        cards.nth(1).locator('.tool-detail-card').boundingBox(),
        page.locator('#spacing-batch-assistant .message-markdown > :first-child').boundingBox(),
      ])
      if (!firstDetail || !secondHeader || !secondDetail || !answer) throw new Error('Tool 批次几何不可用')
      expect(secondHeader.y - (firstDetail.y + firstDetail.height)).toBeCloseTo(16, 5)
      expect(answer.y - (secondDetail.y + secondDetail.height)).toBeCloseTo(16, 5)
    }
  }
})

test('对话底部 Tool 在浅深主题与四个视口展开后避开 Composer', async ({ page }) => {
  const disclosureMessages: Message[] = [
    {
      id: 'tool-disclosure-user',
      role: 'user',
      content: '检查多个文件并汇总',
      createdAt: BASE_TIME,
    },
    ...Array.from({ length: 30 }, (_, index): Message => ({
      id: `tool-disclosure-context-${index + 1}`,
      role: 'assistant',
      content: `已有对话上下文 ${index + 1}`,
      createdAt: BASE_TIME,
      meta: { status: 'completed' },
    })),
    ...Array.from({ length: 6 }, (_, index): Message => ({
      id: `tool-disclosure-${index + 1}`,
      role: 'tool',
      content: 'glob',
      createdAt: BASE_TIME,
      meta: {
        toolName: 'glob',
        toolCallId: `tool-disclosure-call-${index + 1}`,
        params: JSON.stringify({ pattern: index === 5 ? '**/*' : `**/*.${index}` }),
        result: Array.from({ length: 12 }, (__, item) => `/workspace/result-${index}-${item}.md`).join('\n'),
        status: 'completed',
      },
    })),
  ]
  await mockStudio(page, { conversationMessages: disclosureMessages })
  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({
      colorScheme,
      reducedMotion: colorScheme === 'dark' ? 'reduce' : 'no-preference',
    })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await page.reload()
      await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
      const pane = page.getByRole('region', { name: '对话内容' })
      await pane.evaluate((element) => { element.scrollTop = element.scrollHeight })
      const row = page.locator('#tool-disclosure-6')
      const summary = row.locator(':scope > summary')
      const beforeOpen = await pane.evaluate((element) => element.scrollTop)

      await summary.click()
      await expect(row).toHaveAttribute('open', '')
      await expect.poll(async () => {
        const [detail, composer] = await Promise.all([
          row.locator('.tool-detail-card').boundingBox(),
          page.locator('.composer').boundingBox(),
        ])
        if (!detail || !composer) return Number.POSITIVE_INFINITY
        return (detail.y + detail.height) - composer.y
      }).toBeLessThanOrEqual(-8)
      const [detailBounds, composerBounds] = await Promise.all([
        row.locator('.tool-detail-card').boundingBox(),
        page.locator('.composer').boundingBox(),
      ])
      if (!detailBounds || !composerBounds) throw new Error('Tool 详情或 Composer 几何不可用')
      expect(detailBounds.y + detailBounds.height).toBeLessThanOrEqual(composerBounds.y - 8)
      await expect.poll(async () => pane.evaluate((element) => element.scrollTop)).toBeGreaterThan(beforeOpen)


      const beforeClose = await pane.evaluate((element) => element.scrollTop)
      await summary.click()
      await expect(row).not.toHaveAttribute('open', '')
      expect(await pane.evaluate((element) => element.scrollTop)).toBeLessThanOrEqual(beforeClose)
      const [summaryBounds, paneBounds] = await Promise.all([
        summary.boundingBox(),
        pane.boundingBox(),
      ])
      if (!summaryBounds || !paneBounds) throw new Error('Tool 摘要或对话滚动区几何不可用')
      expect(summaryBounds.y).toBeGreaterThanOrEqual(paneBounds.y)
      expect(summaryBounds.y + summaryBounds.height).toBeLessThanOrEqual(composerBounds.y - 8)
    }
  }
})

test('加载更早消息后从按钮热区底边继续公共消息间距', async ({ page }) => {
  await mockStudio(page)
  const loadEarlier = page.getByRole('button', { name: '加载更早消息' })
  const firstMessage = page.locator('.message-history-loader + :is(.user-message, .assistant-message, .subagent-card, .tool-card, .tool-batch, .error-message)').first()

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const [loaderBounds, messageBounds] = await Promise.all([
        loadEarlier.boundingBox(),
        firstMessage.boundingBox(),
      ])
      if (!loaderBounds || !messageBounds) throw new Error('历史加载入口几何不可用')
      expect(messageBounds.y - (loaderBounds.y + loaderBounds.height)).toBeCloseTo(16, 5)
    }
  }
})

test('提问等待状态与上一条气泡使用公共顶层间距', async ({ page }) => {
  await mockStudio(page, {
    planQuestion: true,
    conversationMessages: [{
      id: 'plan-spacing-user',
      role: 'user',
      content: '确认执行选项',
      createdAt: BASE_TIME,
    }],
    expectedMessageText: '确认执行选项',
  })
  const bubble = page.locator('#plan-spacing-user .message-markdown')
  const bubbleAction = page.locator('#plan-spacing-user .message-action-row--user')
  const waitState = page.locator('.plan-question-wait-state')
  const card = page.getByRole('region', { name: 'Plan 澄清问题' })
  const header = card.locator('.plan-question-composer-head')
  const title = card.locator('.plan-question-composer-heading h2 > span')
  const icon = card.locator('.plan-question-composer-heading h2 > svg')
  const bridge = card.locator('.interaction-card-color-bridge.is-plan')
  const attentionDot = page.locator(`[data-history-thread-id="${THREAD_ID}"] .conversation-attention-dot`)
  const statusRow = waitState.locator('.plan-question-status-row')
  const waitDots = waitState.locator('.activity-dots')
  const messageList = page.locator('.message-list')

  await expect(header.locator('.plan-interaction-card-description'))
    .toHaveText('这些答案会影响后续规划')
  for (const [colorScheme, headerBackground, contentBackground, accentColor] of [
    ['light', 'rgb(237, 243, 254)', 'rgb(255, 255, 255)', 'rgb(57, 100, 254)'],
    ['dark', 'rgb(40, 49, 66)', 'rgb(35, 35, 36)', 'rgb(103, 158, 254)'],
  ] as const) {
    await page.emulateMedia({ colorScheme })
    await page.evaluate((theme) => {
      document.documentElement.dataset.theme = theme
    }, colorScheme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await expect(card).toHaveCSS('border-top-width', '0px')
      await expect(header).toHaveCSS('background-color', headerBackground)
      await expect(title).toHaveCSS('color', accentColor)
      await expect(icon).toHaveCSS('color', accentColor)
      const bridgeBackground = await bridge.evaluate((element) => getComputedStyle(element).backgroundImage)
      expect(bridgeBackground).toContain(headerBackground)
      expect(bridgeBackground).toContain(contentBackground)
      await expect(attentionDot).toHaveClass(/is-plan/)
      await expect(attentionDot).toHaveCSS('color', accentColor)
      await expect(statusRow.locator('.plan-interaction-status-label')).toHaveCSS('font-weight', '400')
      const [bubbleBounds, bubbleActionBounds, waitBounds, statusBounds, dotsBounds, messageListBounds] = await Promise.all([
        bubble.boundingBox(),
        bubbleAction.boundingBox(),
        waitState.boundingBox(),
        statusRow.boundingBox(),
        waitDots.boundingBox(),
        messageList.boundingBox(),
      ])
      if (!bubbleBounds || !bubbleActionBounds || !waitBounds || !statusBounds || !dotsBounds || !messageListBounds) throw new Error('提问等待间距几何不可用')
      expect(waitBounds.y - (bubbleBounds.y + bubbleBounds.height)).toBeCloseTo(56, 5)
      expect(waitBounds.y - (bubbleActionBounds.y + bubbleActionBounds.height)).toBeCloseTo(16, 5)
      expect(dotsBounds.y - (statusBounds.y + statusBounds.height)).toBeCloseTo(16, 5)
      expect(dotsBounds.x - messageListBounds.x).toBeCloseTo(1, 5)
      expect(Math.abs(dotsBounds.x - await visibleSvgStrokeLeft(statusRow.locator('svg')))).toBeLessThanOrEqual(.5)
    }
  }
})

test('计划草稿以描述标题和三动作卡片接管输入区', async ({ page }) => {
  await mockStudio(page, {
    planReview: true,
    conversationMessages: [{
      id: 'plan-review-user',
      role: 'user',
      content: '审阅当前计划',
      createdAt: BASE_TIME,
    }],
    expectedMessageText: '审阅当前计划',
  })
  const card = page.getByRole('region', { name: 'Plan 审阅' })
  const header = card.locator('.plan-review-composer-head')
  const body = card.locator('.plan-review-composer-body')
  const footer = card.locator('.plan-review-composer-footer')
  const waitState = page.locator('.plan-review-wait-state')
  const statusRow = waitState.locator('.plan-review-status-row')
  const waitDots = waitState.locator('.activity-dots')
  const messageList = page.locator('.message-list')

  await expect(page.locator('.composer-takeover .plan-review-composer')).toHaveCount(1)
  await expect(page.locator('.message-list .plan-review-composer')).toHaveCount(0)
  await expect(card.locator('.plan-review-composer-heading h2'))
    .toHaveText('保持现有会话行为并完成响应式验证')
  await expect(card.locator('.plan-review-composer-heading p')).toHaveCount(0)
  await expect(card.locator('.plan-review-composer-heading small')).toHaveCount(0)
  await expect(card.locator('.plan-review-composer-heading h2')).not.toContainText('Plan')
  await expect(card).not.toContainText('第 3 版')
  await expect(card.locator('.plan-review-toggle-surface')).toHaveCount(0)
  await expect(card.locator('.plan-review-composer-head-button')).toHaveCount(1)
  await expect(card.getByRole('button', { name: '取消当前 Plan 草稿' })).toBeVisible()
  await expect(page.getByRole('separator', { name: '调整交互卡片高度' })).toBeVisible()
  await expect(waitState).toContainText('Plan')
  await expect(waitState).toContainText('等待审阅')
  await expect(waitDots).toBeVisible()
  await expect(card.getByRole('button', { name: '拒绝' })).toBeVisible()
  await expect(card.getByRole('button', { name: '反馈' })).toHaveCount(0)
  await expect(card.getByRole('button', { name: '批准' })).toBeVisible()
  await expect(card.getByRole('button', { name: '编辑' })).toHaveCount(0)
  await expect(card.getByRole('button', { name: /关闭|放弃/ })).toHaveCount(0)

  const title = card.locator('.plan-review-composer-heading h2 > span')
  const icon = card.locator('.plan-review-composer-heading h2 > svg')
  const bridge = card.locator('.interaction-card-color-bridge.is-warning')
  const finalItem = card.getByText('验证 Footer 不遮挡最终验收项')
  for (const [colorScheme, headerBackground, contentBackground] of [
    ['light', 'rgb(254, 245, 231)', 'rgb(255, 255, 255)'],
    ['dark', 'rgb(39, 36, 31)', 'rgb(35, 35, 36)'],
  ] as const) {
    await page.emulateMedia({ colorScheme })
    await page.evaluate((theme) => {
      document.documentElement.dataset.theme = theme
    }, colorScheme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      if (width === 320) {
        const closeNavigation = page.getByRole('button', { name: '关闭导航' })
        if (await closeNavigation.isVisible()) await closeNavigation.click()
      }
      await expect(card).toHaveCSS('border-top-width', '0px')
      await expect(header).toHaveCSS('background-color', headerBackground)
      await expect(header).toHaveCSS('padding-top', '10px')
      await expect(header).toHaveCSS('padding-bottom', '10px')
      await expect(footer).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
      const reviewFooterOverlay = await footer.evaluate(
        (element) => {
          const style = getComputedStyle(element, '::before')
          return { content: style.content, backgroundImage: style.backgroundImage }
        },
      )
      expect(reviewFooterOverlay.content).not.toBe('none')
      expect(reviewFooterOverlay.backgroundImage).toContain('linear-gradient')
      await body.evaluate((element) => { element.scrollTop = element.scrollHeight })
      await expect(finalItem).toBeVisible()
      await expect(title).toHaveCSS('color', 'rgb(245, 158, 11)')
      await expect(icon).toHaveCSS('color', 'rgb(245, 158, 11)')
      const bridgeBackground = await bridge.evaluate((element) => getComputedStyle(element).backgroundImage)
      expect(bridgeBackground).toContain(headerBackground)
      expect(bridgeBackground).toContain(contentBackground)
      await expect(statusRow.locator('.plan-interaction-status-label')).toHaveCSS('font-weight', '400')
      const [cardBounds, bodyBounds, footerBounds, finalItemBounds, statusBounds, dotsBounds, messageListBounds] = await Promise.all([
        card.boundingBox(),
        body.boundingBox(),
        footer.boundingBox(),
        finalItem.boundingBox(),
        statusRow.boundingBox(),
        waitDots.boundingBox(),
        messageList.boundingBox(),
      ])
      if (!cardBounds || !bodyBounds || !footerBounds || !finalItemBounds || !statusBounds || !dotsBounds || !messageListBounds) throw new Error('计划草稿卡片几何不可用')
      expect(footerBounds.y).toBeLessThan(bodyBounds.y + bodyBounds.height)
      expect(footerBounds.y + footerBounds.height)
        .toBeLessThanOrEqual(bodyBounds.y + bodyBounds.height + .5)
      expect(finalItemBounds.y + finalItemBounds.height).toBeLessThanOrEqual(footerBounds.y + .5)
      const actionBounds = await Promise.all([
        card.getByRole('button', { name: '取消当前 Plan 草稿' }).boundingBox(),
        card.getByRole('button', { name: '拒绝' }).boundingBox(),
        card.getByRole('button', { name: '批准' }).boundingBox(),
      ])
      if (actionBounds.some((bounds) => !bounds)) throw new Error('计划草稿操作按钮几何不可用')
      expect(cardBounds.width).toBeLessThanOrEqual(width)
      expect(cardBounds.x).toBeGreaterThanOrEqual(0)
      expect(statusBounds.height).toBeGreaterThan(0)
      expect(dotsBounds.x - messageListBounds.x).toBeCloseTo(1, 5)
      expect(Math.abs(dotsBounds.x - await visibleSvgStrokeLeft(statusRow.locator('svg')))).toBeLessThanOrEqual(.5)
      for (const bounds of actionBounds) {
        if (!bounds) continue
        expect(bounds.x).toBeGreaterThanOrEqual(cardBounds.x)
        expect(bounds.x + bounds.width).toBeLessThanOrEqual(cardBounds.x + cardBounds.width)
        expect(bounds.height).toBeLessThanOrEqual(44)
      }

    }
  }

  await page.emulateMedia({ colorScheme: 'light' })
  const reject = card.getByRole('button', { name: '拒绝' })
  await reject.click()
  const rejectionReason = card.getByRole('textbox', { name: '拒绝原因（可选）' })
  await expect(rejectionReason).toBeFocused()
  await expect(rejectionReason).not.toHaveAttribute('required')
  await expect(card.getByRole('button', { name: '确认拒绝' })).toBeEnabled()
  await rejectionReason.fill('补充断连恢复验证')
  await card.getByRole('button', { name: '取消', exact: true }).click()
  await expect(reject).toBeFocused()

  await expect(card.getByRole('region', { name: '计划草稿内容' })).toBeVisible()
  await expect(card).toContainText('保持现有会话行为并完成响应式验证')
  await expect(card).not.toContainText('第 3 版')
  await expect(card.getByRole('button', { name: /展开计划草稿|收起计划草稿/ })).toHaveCount(0)
})

test('Tool 与回答复制图标严格对齐正文左缘', async ({ page }) => {
  await mockStudio(page)
  const lastAssistant = page.locator('.assistant-message').last()
  const previousBody = page.locator('#browser-message-150 .message-markdown > :first-child')
  const body = lastAssistant.locator('.message-markdown > :first-child')
  const copyIcon = lastAssistant.locator('.message-action-row .ui-icon-button svg')
  const subagent = page.locator('#browser-subagent > summary')
  const tool = page.locator('.tool-card > summary')
  const toolIcon = tool.locator('.tool-row-icon svg')
  const messageList = page.locator('.message-list')

  await expect(page.locator('.message-action-row')).toHaveCount(1)
  await expect(lastAssistant.locator('.message-action-row--assistant')).toHaveCSS('margin-top', '0px')

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const [previousBodyBounds, bodyBounds, copyBounds, subagentBounds, toolBounds, toolIconBounds, messageListBounds] = await Promise.all([
        previousBody.boundingBox(),
        body.boundingBox(),
        copyIcon.boundingBox(),
        subagent.boundingBox(),
        tool.boundingBox(),
        toolIcon.boundingBox(),
        messageList.boundingBox(),
      ])
      if (!previousBodyBounds || !bodyBounds || !copyBounds || !subagentBounds || !toolBounds || !toolIconBounds || !messageListBounds) {
        throw new Error('对话几何不可用')
      }

      expect(bodyBounds.x).toBeCloseTo(messageListBounds.x, 5)
      expect(copyBounds.x).toBeCloseTo(bodyBounds.x, 5)
      expect(copyBounds.width).toBeCloseTo(20, 5)
      expect(copyBounds.height).toBeCloseTo(20, 5)
      expect(toolIconBounds.x).toBeCloseTo(bodyBounds.x, 5)
      expect(copyBounds.y - (bodyBounds.y + bodyBounds.height)).toBeCloseTo(11, 5)
      expect(subagentBounds.y - (previousBodyBounds.y + previousBodyBounds.height)).toBeCloseTo(16, 5)
      expect(toolBounds.y - (subagentBounds.y + subagentBounds.height)).toBeCloseTo(16, 5)
      expect(bodyBounds.y - (toolBounds.y + toolBounds.height)).toBeCloseTo(16, 5)
    }
  }
})

test('全部 Tool 图标与等待动画共用最小误差光学左缘', async ({ page }) => {
  await mockStudio(page, {
    conversationMessages: toolIconAuditMessages,
    expectedMessageText: '/icon-audit/task',
  })
  const messageList = page.locator('.message-list')
  const icons = page.locator('.message-list .tool-row-icon svg')
  await expect(icons).toHaveCount(toolIconAuditMessages.length)

  const messageListBounds = await messageList.boundingBox()
  if (!messageListBounds) throw new Error('Tool 图标审计基线不可用')
  const opticalInset = await page.evaluate(() => Number.parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue('--optical-activity-dots-inset'),
  ))
  expect(opticalInset).toBe(1)

  const visibleOffsets = await Promise.all(Array.from(
    { length: await icons.count() },
    (_, index) => visibleSvgStrokeLeft(icons.nth(index)),
  ))
  for (const visibleLeft of visibleOffsets) {
    expect(Math.abs(visibleLeft - (messageListBounds.x + opticalInset))).toBeLessThanOrEqual(.75)
  }
})

test('运行中 SubAgent 与普通 Tool 共用扫光且标题保持稳定', async ({ page }) => {
  await mockStudio(page, { runningActivity: true })
  const subagentHeader = page.locator('.subagent-card.running > summary')
  const toolHeader = page.locator('.tool-card.running > summary')
  const assistantBody = page.locator('#browser-running-stage .message-markdown > :first-child')
  const subagentIcon = subagentHeader.locator('.tool-row-icon svg')
  const toolIcon = toolHeader.locator('.tool-row-icon svg')
  const messageList = page.locator('.message-list')

  await expect(page.locator('.message-action-row--assistant')).toHaveCount(1)
  await expect(page.locator('.message-action-row--user')).toHaveCount(1)
  await expect(page.locator('#browser-running-stage .message-action-row')).toHaveCount(0)

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await expect(subagentHeader).toContainText('Task')
      await expect(subagentHeader).toContainText('SubAgent')
      const alignmentBounds = await Promise.all([
        assistantBody,
        subagentIcon,
        toolIcon,
        messageList,
      ].map((locator) => locator.boundingBox()))
      if (alignmentBounds.some((bounds) => !bounds)) throw new Error('运行中内容左缘几何不可用')
      const [assistantBounds, subagentIconBounds, toolIconBounds, messageListBounds] = alignmentBounds
      for (const bounds of [assistantBounds, subagentIconBounds, toolIconBounds]) {
        expect(bounds!.x).toBeCloseTo(messageListBounds!.x, 5)
      }
      const visibleIconLefts = await Promise.all([
        visibleSvgStrokeLeft(subagentIcon),
        visibleSvgStrokeLeft(toolIcon),
      ])
      for (const visibleLeft of visibleIconLefts) {
        expect(Math.abs(visibleLeft - (messageListBounds!.x + 1))).toBeLessThanOrEqual(.75)
      }
      expect(await subagentHeader.evaluate((element) => (
        element.getAnimations({ subtree: true })
          .some((animation) => animation instanceof CSSAnimation
            && animation.animationName === 'conversation-tool-row-sweep')
      ))).toBe(true)
      expect(await toolHeader.evaluate((element) => (
        element.getAnimations({ subtree: true })
          .some((animation) => animation instanceof CSSAnimation
            && animation.animationName === 'conversation-tool-row-sweep')
      ))).toBe(true)
    }
  }

  const sweepParameters = await Promise.all([subagentHeader, toolHeader].map((header) => (
    header.evaluate((element) => {
      const style = getComputedStyle(element, '::after')
      const animation = element.getAnimations({ subtree: true })
        .find((item) => item instanceof CSSAnimation
          && item.animationName === 'conversation-tool-row-sweep')
      const frames = animation?.effect instanceof KeyframeEffect
        ? animation.effect.getKeyframes()
        : []
      return {
        width: style.width,
        duration: style.animationDuration,
        easing: style.animationTimingFunction,
        iterations: style.animationIterationCount,
        frames: frames.map((frame) => ({ offset: frame.offset, left: frame.left })),
      }
    })
  )))
  const expectedSweepParameters = {
    width: '300px',
    duration: '2.6s',
    easing: 'ease-out',
    iterations: 'infinite',
    frames: [
      { offset: 0, left: '-300px' },
      { offset: 0.9, left: '100%' },
      { offset: 1, left: '100%' },
    ],
  }
  expect(sweepParameters).toEqual([expectedSweepParameters, expectedSweepParameters])

  const titleStability = await subagentHeader.evaluate((element) => {
    const animation = element.getAnimations({ subtree: true })
      .find((item) => item instanceof CSSAnimation
        && item.animationName === 'conversation-tool-row-sweep')
    if (!animation) throw new Error('SubAgent 运行态扫光不可用')
    const read = () => {
      const bounds = element.getBoundingClientRect()
      return {
        text: element.textContent,
        x: bounds.x,
        y: bounds.y,
        width: bounds.width,
        height: bounds.height,
      }
    }
    animation.currentTime = 0
    const before = read()
    animation.currentTime = 1300
    return { before, after: read() }
  })
  expect(titleStability.after).toEqual(titleStability.before)

  await page.emulateMedia({ reducedMotion: 'reduce' })
  for (const header of [subagentHeader, toolHeader]) {
    expect(await header.evaluate((element) => (
      element.getAnimations({ subtree: true })
        .some((animation) => animation instanceof CSSAnimation
          && animation.animationName === 'conversation-tool-row-sweep')
    ))).toBe(false)
  }
  await page.emulateMedia({ reducedMotion: 'no-preference' })

  await subagentHeader.click()
  await expect(page.locator('.subagent-output-node.is-running')).toContainText('执行中')
  await expect(page.locator('.subagent-output-node.is-running .subagent-output-pulse')).toBeVisible()
  await expect(page.locator('.subagent-output-node.is-running .activity-dots')).toHaveCount(0)
  const childTool = page.locator('.subagent-tool-row')
  await childTool.locator('summary').click()
  await toolHeader.click()
  const openToolSections = page.locator('.tool-detail-section:visible')
  await expect(openToolSections).toHaveCount(4)
  const sharedAlignment = await openToolSections.evaluateAll((sections) => sections.map((section) => ({
    alignItems: getComputedStyle(section).alignItems,
    labelAlignSelf: getComputedStyle(section.children[0]).alignSelf,
  })))
  expect(sharedAlignment).toEqual(Array.from({ length: 4 }, () => ({
    alignItems: 'baseline',
    labelAlignSelf: 'baseline',
  })))
})

test('搜索会话点击后保持标准输入高度且不显示容器描边', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockStudio(page)

  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await page.reload()
      if (width === 320) await page.getByRole('button', { name: '打开导航' }).click()
      await page.getByRole('button', { name: '搜索会话' }).click()

      const input = page.getByRole('textbox', { name: '搜索会话' })
      const search = page.locator('.sidebar-search')
      await expect(input).toBeFocused()
      const metrics = await search.evaluate((element) => {
        const bounds = element.getBoundingClientRect()
        const headerBounds = element.parentElement!.getBoundingClientRect()
        const styles = getComputedStyle(element)
        return {
          height: bounds.height,
          centerOffset: ((bounds.top + bounds.bottom) - (headerBounds.top + headerBounds.bottom)) / 2,
          borderTopWidth: styles.borderTopWidth,
          outlineStyle: styles.outlineStyle,
          outlineWidth: styles.outlineWidth,
        }
      })

      expect(metrics).toEqual({
        height: 44,
        centerOffset: 0,
        borderTopWidth: '0px',
        outlineStyle: 'none',
        outlineWidth: '0px',
      })
    }
  }
})

test('历史分页一次提交最终滑块比例，不产生中间位移动画', { tag: '@performance' }, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 })
  const historyRequests: Array<{ cursor: string | null; receivedAt: number }> = []
  await mockStudio(page, {
    onHistoryRequest: (request) => historyRequests.push(request),
    paginatedHistory: true,
    paginationResponseDelayMs: 500,
  })
  const history = page.getByRole('region', { name: '最近对话' })
  const slot = history.locator('.history-pagination-slot')
  const scrollbar = page.locator('.conversation-history > .ui-overlay-scrollbar')
  const thumb = scrollbar.locator('.ui-overlay-scrollbar__thumb')
  await expect(slot).toHaveCSS('height', '44px')
  const idleScrollHeight = await history.evaluate((element) => element.scrollHeight)

  const fastScrollStartedAt = Date.now()
  await history.evaluate((element) => {
    element.scrollTop = element.scrollHeight
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  await expect(page.getByText('正在加载更多历史会话')).toBeVisible()
  await expect.poll(() => historyRequests.length).toBe(2)
  expect(historyRequests[1]!.receivedAt - fastScrollStartedAt).toBeLessThan(100)
  expect(await history.evaluate((element) => element.scrollHeight)).toBe(idleScrollHeight)
  await expect(slot).toHaveCSS('height', '44px')
  const pendingGeometry = await thumb.evaluate(async (element) => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
    const bounds = element.getBoundingClientRect()
    return { height: bounds.height, top: bounds.top }
  })

  await expect(page.getByRole('button', { name: '打开会话：分页验证会话 29' })).toBeVisible()
  const finalGeometry = await thumb.evaluate(async (element) => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
    const frames: Array<{ height: number; top: number }> = []
    for (let index = 0; index < 4; index += 1) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
      const bounds = element.getBoundingClientRect()
      frames.push({ height: bounds.height, top: bounds.top })
    }
    return {
      animationCount: element.getAnimations().length,
      frames,
      transitionDuration: getComputedStyle(element).transitionDuration,
    }
  })
  expect(finalGeometry.transitionDuration).toBe('0s')
  expect(finalGeometry.animationCount).toBe(0)
  expect(finalGeometry.frames[0]!.top).toBeLessThan(pendingGeometry.top)
  expect(finalGeometry.frames[0]!.height).toBeLessThan(pendingGeometry.height)
  expect(new Set(
    finalGeometry.frames.map(({ top, height }) => `${top.toFixed(2)}:${height.toFixed(2)}`),
  ).size).toBe(1)
})

test('全局滚动条保持统一参数、分层显隐和直接拖拽映射', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 })
  await mockStudio(page, { paginatedHistory: true })
  const pane = page.getByRole('region', { name: '对话内容' })
  const history = page.getByRole('region', { name: '最近对话' })
  const mainScrollbar = page.locator('.conversation-region > .ui-overlay-scrollbar')
  const historyScrollbar = page.locator('.conversation-history > .ui-overlay-scrollbar')
  const mainThumb = mainScrollbar.locator('.ui-overlay-scrollbar__thumb')

  await expect(mainScrollbar).toHaveAttribute('data-scrollable', 'true')
  await expect(mainScrollbar).toHaveAttribute('data-visibility', 'persistent')
  await expect(mainScrollbar).toHaveClass(/is-visible/)
  await expect(mainScrollbar).toHaveCSS('width', '8px')
  await expect(historyScrollbar).toHaveAttribute('data-visibility', 'transient')
  await expect(historyScrollbar).not.toHaveClass(/is-visible/)
  const idleVisual = await mainThumb.evaluate((element) => {
    const styles = getComputedStyle(element, '::after')
    return { width: styles.width, color: styles.backgroundColor, radius: styles.borderRadius }
  })
  expect(idleVisual).toEqual({ width: '8px', color: 'rgb(229, 229, 229)', radius: '9999px' })

  await mainThumb.hover()
  await page.waitForTimeout(120)
  const hoveredVisual = await mainThumb.evaluate((element) => {
    const styles = getComputedStyle(element, '::after')
    return { width: styles.width, color: styles.backgroundColor }
  })
  expect(hoveredVisual).toEqual({ width: '10px', color: 'rgb(212, 212, 212)' })

  await history.hover()
  await expect(historyScrollbar).toHaveClass(/is-visible/)
  await page.mouse.move(900, 250)
  await page.waitForTimeout(1_100)
  await expect(historyScrollbar).not.toHaveClass(/is-visible/)

  await pane.evaluate((element) => {
    element.scrollTop = Math.min(200, element.scrollHeight - element.clientHeight)
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  const beforeDrag = await pane.evaluate((element) => element.scrollTop)
  const thumbBox = await mainThumb.boundingBox()
  if (!thumbBox) throw new Error('主对话滚动滑块不可见')
  await page.mouse.move(thumbBox.x + (thumbBox.width / 2), thumbBox.y + (thumbBox.height / 2))
  await page.mouse.down()
  await page.mouse.move(thumbBox.x + (thumbBox.width / 2), thumbBox.y + (thumbBox.height / 2) + 40, { steps: 4 })
  await page.mouse.up()
  expect(await pane.evaluate((element) => element.scrollTop)).toBeGreaterThan(beforeDrag)
})

test('账户菜单、modal 隔离和定时滚动控件保持完整键盘路径', async ({ page }) => {
  await page.clock.install()
  await mockStudio(page, {
    taskTrace: {
      status: 'ready',
      todoGroups: [{
        id: 'todo-group:browser-run',
        userMessageId: 'browser-message-1',
        userMessagePreview: '浏览器历史消息 1',
        groupToolCallId: 'browser-task-trace-tool',
        createdAt: BASE_TIME,
        status: 'completed',
        todos: [{
          id: 'todo-group:browser-run:todo:1',
          content: '验证输入区辅助操作',
          status: 'completed',
        }],
      }],
    },
  })
  const account = page.getByRole('button', { name: '打开用户菜单' })
  await account.click()
  await expect(page.getByRole('menuitem', { name: '设置' })).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(page.getByRole('menuitem', { name: '退出登录' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(account).toBeFocused()

  await account.click()
  await page.getByRole('menuitem', { name: '设置' }).click()
  const settingsDialog = page.getByRole('dialog', { name: '设置' })
  await expect(settingsDialog).toBeVisible()
  await expect(settingsDialog).toHaveCSS('border-radius', '22px')
  await page.setViewportSize({ width: 320, height: 900 })
  await expect(settingsDialog).toHaveCSS('border-radius', '22px')
  await page.keyboard.press('Meta+K')
  await expect(settingsDialog).toBeVisible()
  await expect(page.locator('.app-shell')).toHaveAttribute('inert', '')
  await page.getByRole('button', { name: '关闭对话框' }).click()

  const pane = page.getByRole('region', { name: '对话内容' })
  await pane.evaluate((element) => {
    element.scrollTop = 0
    element.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true }))
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  const scrollButton = page.getByRole('button', { name: '回到底部' })
  await expect(scrollButton).toBeVisible()
  const taskTraceButton = page.getByRole('button', { name: '任务轨迹 1' })
  await expect(taskTraceButton).toBeVisible()
  await scrollButton.focus()
  const auxiliaryAlignment = await page.evaluate(() => {
    const scroll = document.querySelector<HTMLElement>('.scroll-to-bottom')
    const taskTrace = document.querySelector<HTMLElement>('.todo-trace-launcher')
    const composer = document.querySelector<HTMLElement>('.composer')
    if (!scroll || !taskTrace || !composer) return null
    const scrollRect = scroll.getBoundingClientRect()
    const taskTraceRect = taskTrace.getBoundingClientRect()
    const composerRect = composer.getBoundingClientRect()
    const visual = (element: HTMLElement) => {
      const style = getComputedStyle(element)
      return {
        minHeight: style.minHeight,
        borderRadius: style.borderRadius,
        paddingLeft: style.paddingLeft,
        paddingRight: style.paddingRight,
        fontSize: style.fontSize,
        fontWeight: style.fontWeight,
        lineHeight: style.lineHeight,
      }
    }
    return {
      scroll: {
        top: scrollRect.top,
        height: scrollRect.height,
        centerY: scrollRect.top + (scrollRect.height / 2),
        centerX: scrollRect.left + (scrollRect.width / 2),
        right: scrollRect.right,
      },
      taskTrace: {
        top: taskTraceRect.top,
        height: taskTraceRect.height,
        centerY: taskTraceRect.top + (taskTraceRect.height / 2),
        left: taskTraceRect.left,
        right: taskTraceRect.right,
      },
      composerTop: composerRect.top,
      composerCenterX: composerRect.left + (composerRect.width / 2),
      composerRight: composerRect.right,
      scrollVisual: visual(scroll),
      taskTraceVisual: visual(taskTrace),
    }
  })
  expect(auxiliaryAlignment).not.toBeNull()
  expect(Math.abs((auxiliaryAlignment?.scroll.top ?? 0) - (auxiliaryAlignment?.taskTrace.top ?? 0)))
    .toBeLessThanOrEqual(0.5)
  expect(Math.abs((auxiliaryAlignment?.scroll.height ?? 0) - (auxiliaryAlignment?.taskTrace.height ?? 0)))
    .toBeLessThanOrEqual(0.5)
  expect(Math.abs((auxiliaryAlignment?.scroll.centerY ?? 0) - (auxiliaryAlignment?.taskTrace.centerY ?? 0)))
    .toBeLessThanOrEqual(0.5)
  expect(Math.abs((auxiliaryAlignment?.scroll.centerX ?? 0) - (auxiliaryAlignment?.composerCenterX ?? 0)))
    .toBeLessThanOrEqual(0.5)
  expect(Math.abs((auxiliaryAlignment?.taskTrace.right ?? 0) - (auxiliaryAlignment?.composerRight ?? 0)))
    .toBeLessThanOrEqual(0.5)
  expect(auxiliaryAlignment?.scroll.right).toBeLessThan(auxiliaryAlignment?.taskTrace.left ?? 0)
  expect((auxiliaryAlignment?.scroll.top ?? 0) + (auxiliaryAlignment?.scroll.height ?? 0))
    .toBeLessThanOrEqual(auxiliaryAlignment?.composerTop ?? 0)
  expect(auxiliaryAlignment?.scrollVisual).toEqual(auxiliaryAlignment?.taskTraceVisual)
  for (const width of [768, 1023, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    const responsiveAlignment = await page.evaluate(() => {
      const scroll = document.querySelector<HTMLElement>('.scroll-to-bottom')
      const taskTrace = document.querySelector<HTMLElement>('.todo-trace-launcher')
      const composer = document.querySelector<HTMLElement>('.composer')
      if (!scroll || !taskTrace || !composer) return null
      const scrollRect = scroll.getBoundingClientRect()
      const taskTraceRect = taskTrace.getBoundingClientRect()
      const composerRect = composer.getBoundingClientRect()
      return {
        centerDelta: Math.abs(
          (scrollRect.left + (scrollRect.width / 2))
          - (composerRect.left + (composerRect.width / 2)),
        ),
        rightDelta: Math.abs(taskTraceRect.right - composerRect.right),
        verticalDelta: Math.abs(
          (scrollRect.top + (scrollRect.height / 2))
          - (taskTraceRect.top + (taskTraceRect.height / 2)),
        ),
        overlap: Math.max(0, scrollRect.right - taskTraceRect.left),
        overflow: Math.max(
          document.documentElement.scrollWidth - document.documentElement.clientWidth,
          document.body.scrollWidth - document.body.clientWidth,
        ),
      }
    })
    expect(responsiveAlignment).not.toBeNull()
    expect(responsiveAlignment?.centerDelta).toBeLessThanOrEqual(0.5)
    expect(responsiveAlignment?.rightDelta).toBeLessThanOrEqual(0.5)
    expect(responsiveAlignment?.verticalDelta).toBeLessThanOrEqual(0.5)
    expect(responsiveAlignment?.overlap).toBe(0)
    expect(responsiveAlignment?.overflow).toBeLessThanOrEqual(0)
  }
  await page.clock.fastForward(2_000)
  await expect(scrollButton).toBeVisible()
  await pane.focus()
  await page.clock.fastForward(1_800)
  await expect(scrollButton).toHaveCount(0)
  await pane.evaluate((element) => {
    element.dispatchEvent(new WheelEvent('wheel', { deltaY: 120, bubbles: true }))
    element.scrollTop += 120
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  await expect(scrollButton).toBeVisible()
})

test('浅深主题的 Tool caption 对比度均达标，forced-colors 保留焦点', async ({ page }) => {
  await mockStudio(page)
  await page.getByText('Read', { exact: true }).click()
  expect(Math.min(...await contrastRatios(page, '.tool-field-label'))).toBeGreaterThanOrEqual(4.5)

  await page.getByRole('button', { name: '打开用户菜单' }).click()
  await page.getByRole('menuitem', { name: '设置' }).click()
  await page.getByRole('button', { name: '通用' }).click()
  await page.locator('.settings-theme-option').filter({ hasText: '深色' }).click()
  await page.getByRole('button', { name: '关闭对话框' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  expect(Math.min(...await contrastRatios(page, '.tool-field-label'))).toBeGreaterThanOrEqual(4.5)

  await page.emulateMedia({ forcedColors: 'active' })
  const attachment = page.getByRole('button', { name: '添加本地附件' })
  await page.getByRole('textbox', { name: '消息输入' }).focus()
  await page.keyboard.press('Tab')
  await expect(attachment).toBeFocused()
  const outline = await attachment.evaluate((element) => getComputedStyle(element).outlineStyle)
  expect(outline).not.toBe('none')
  await expect(page.locator('.ui-overlay-scrollbar').first()).toHaveCSS('display', 'none')
  const nativeScrollbar = await page.getByRole('region', { name: '对话内容' }).evaluate((element) => (
    getComputedStyle(element, '::-webkit-scrollbar').display
  ))
  expect(nativeScrollbar).toBe('block')
})

test('reduced-motion 跳过 Flip 布局动画', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await mockStudio(page)
  await page.getByRole('button', { name: '收起侧边栏' }).click()
  await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', 'rail')
  await expect(page.locator('.is-layout-flipping')).toHaveCount(0)
  const scrollbar = page.locator('.ui-overlay-scrollbar').first()
  await expect(scrollbar).toHaveCSS('transition-duration', '0s')
  await expect(scrollbar.locator('.ui-overlay-scrollbar__thumb')).toHaveCSS('transition-duration', '0s')
})

test('布局动效不逐帧触发布局且冷缓存只请求允许的西文字体', { tag: '@performance' }, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const fontResponses = new Map<string, Promise<Buffer>>()
  page.on('response', (response) => {
    if (new URL(response.url()).pathname.endsWith('.woff2')) {
      fontResponses.set(response.url(), response.body())
    }
  })
  await mockStudio(page, {
    taskTrace: {
      status: 'ready',
      todoGroups: [{
        id: 'todo-group:browser-run',
        userMessageId: 'browser-message-1',
        userMessagePreview: '浏览器历史消息 1',
        groupToolCallId: 'browser-task-trace-tool',
        createdAt: BASE_TIME,
        status: 'completed',
        todos: [{ id: 'browser-todo', content: '验证布局性能', status: 'completed' }],
      }],
    },
  })
  await page.evaluate(() => document.fonts.ready)

  const westernFonts = [...fontResponses.entries()].filter(([url]) => (
    url.includes('inter-') || url.includes('jetbrains-mono-')
  ))
  expect(westernFonts.length).toBeGreaterThan(0)
  expect(westernFonts.every(([url]) => (
    url.includes('-latin-wght-')
    && !/latin-ext|cyrillic|greek|vietnamese/.test(url)
  ))).toBe(true)
  const westernFontBytes = (await Promise.all(
    westernFonts.map(([, body]) => body.then((content) => content.byteLength)),
  )).reduce((total, size) => total + size, 0)
  expect(westernFontBytes).toBeLessThanOrEqual(140_492)

  const session = await page.context().newCDPSession(page)
  await session.send('Performance.enable')
  const traceComplete = new Promise<{ stream?: string }>((resolve) => {
    session.once('Tracing.tracingComplete', resolve)
  })
  await session.send('Tracing.start', {
    categories: 'devtools.timeline',
    transferMode: 'ReturnAsStream',
  })
  const layoutCount = async () => {
    const metrics = await session.send('Performance.getMetrics')
    return metrics.metrics.find((metric) => metric.name === 'LayoutCount')?.value ?? 0
  }
  const before = await layoutCount()
  await page.getByRole('button', { name: '收起侧边栏' }).click()
  await page.waitForTimeout(400)
  await page.getByRole('button', { name: '任务轨迹 1' }).click()
  await page.waitForTimeout(400)
  const layoutDelta = (await layoutCount()) - before
  await session.send('Tracing.end')
  const { stream } = await traceComplete
  if (stream === undefined) throw new Error('Chrome trace did not provide a result stream')
  let traceJson = ''
  let traceEof = false
  while (!traceEof) {
    const chunk = await session.send('IO.read', { handle: stream })
    traceJson += chunk.data
    traceEof = chunk.eof
  }
  await session.send('IO.close', { handle: stream })
  const trace = JSON.parse(traceJson) as {
    traceEvents: Array<{ name: string; ph: string }>
  }
  const tracedLayouts = trace.traceEvents.filter((event) => (
    event.name === 'Layout' && event.ph === 'X'
  ))

  expect(layoutDelta).toBeLessThanOrEqual(12)
  expect(tracedLayouts.length).toBeLessThanOrEqual(12)
})

test.describe('touch/coarse pointer', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('用户与回答复制热区满足触控尺寸并保持完整消息边界', async ({ page }) => {
    await mockStudio(page, {
      conversationMessages: spacingAuditMessages,
      expectedMessageText: '普通文本 B',
    })
    expect(await page.evaluate(() => matchMedia('(any-pointer: coarse)').matches)).toBe(true)

    const userBubble = page.locator('#spacing-user-a .message-markdown')
    const userAction = page.locator('#spacing-user-a .message-action-row--user')
    const userCopyButton = userAction.locator('.ui-icon-button')
    const userCopyIcon = userCopyButton.locator('svg')
    const copyButton = page.locator('#spacing-assistant-a .message-action-row--assistant .ui-icon-button')
    const copyIcon = copyButton.locator('svg')
    const body = page.locator('#spacing-assistant-a .message-markdown > :first-child')
    const nextBubble = page.locator('#spacing-user-b .message-markdown')

    for (const colorScheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme })
      const [userBubbleBounds, userActionBounds, userButtonBounds, userIconBounds, buttonBounds, iconBounds, bodyBounds, bubbleBounds] = await Promise.all([
        userBubble.boundingBox(),
        userAction.boundingBox(),
        userCopyButton.boundingBox(),
        userCopyIcon.boundingBox(),
        copyButton.boundingBox(),
        copyIcon.boundingBox(),
        body.boundingBox(),
        nextBubble.boundingBox(),
      ])
      if (!userBubbleBounds || !userActionBounds || !userButtonBounds || !userIconBounds
        || !buttonBounds || !iconBounds || !bodyBounds || !bubbleBounds) throw new Error('触控复制热区几何不可用')
      await expect(userAction).toHaveCSS('opacity', '1')
      await expect(userAction).toHaveCSS('pointer-events', 'auto')
      expect(userButtonBounds.width).toBeGreaterThanOrEqual(44)
      expect(userButtonBounds.height).toBeGreaterThanOrEqual(44)
      expect(userIconBounds.width).toBeCloseTo(20, 5)
      expect(userIconBounds.height).toBeCloseTo(20, 5)
      expect(userButtonBounds.x + userButtonBounds.width).toBeCloseTo(userBubbleBounds.x + userBubbleBounds.width, 5)
      expect(userActionBounds.y - (userBubbleBounds.y + userBubbleBounds.height)).toBeCloseTo(0, 5)
      expect(bodyBounds.y - (userBubbleBounds.y + userBubbleBounds.height)).toBeCloseTo(52, 5)
      expect(buttonBounds.width).toBeGreaterThanOrEqual(44)
      expect(buttonBounds.height).toBeGreaterThanOrEqual(44)
      expect(iconBounds.x).toBeCloseTo(bodyBounds.x, 5)
      expect(iconBounds.width).toBeCloseTo(20, 5)
      expect(iconBounds.height).toBeCloseTo(20, 5)
      expect(bubbleBounds.y - (buttonBounds.y + buttonBounds.height)).toBeCloseTo(57, 5)
    }
  })

  test('触控环境中的 HITL 保持内容驱动紧凑高度且没有拖拽入口', async ({ page }) => {
    await mockStudio(page, { approval: true })
    expect(await page.evaluate(() => matchMedia('(any-pointer: coarse)').matches)).toBe(true)

    const card = page.getByRole('region', { name: '等待审批' })
    const handle = page.locator('.interaction-card-resize-handle')
    await expect(handle).toHaveCount(0)
    await expect(page.getByRole('separator', { name: '调整交互卡片高度' })).toHaveCount(0)
    const bounds = await card.boundingBox()
    if (!bounds) throw new Error('触控审批卡片几何不可用')
    expect(bounds.height).toBeGreaterThanOrEqual(94)
    expect(bounds.height).toBeLessThan(260)
  })

  test('共享日期选择器在触控与浅深主题下保持可用尺寸和视口边界', async ({ page }) => {
    await mockStudio(page, { planQuestion: true, planQuestionForm: dateOnlyPlanQuestionForm })
    const closeNavigation = page.getByRole('button', { name: '关闭导航' })
    if (await closeNavigation.isVisible()) await closeNavigation.click()
    const trigger = page.getByRole('button', { name: '日期回答：目标发布日期是哪一天？' })

    for (const colorScheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme })
      await page.evaluate((theme) => {
        document.documentElement.dataset.theme = theme
      }, colorScheme)
      const triggerBounds = await trigger.boundingBox()
      if (!triggerBounds) throw new Error('触控日期入口几何不可用')
      expect(triggerBounds.height).toBeGreaterThanOrEqual(44)
      await expect(trigger).toHaveCSS('cursor', 'pointer')
      await trigger.click()

      const calendar = page.getByRole('application', { name: '选择日期' })
      const [calendarBounds, previousBounds, dayBounds] = await Promise.all([
        calendar.boundingBox(),
        calendar.getByRole('button', { name: '上个月' }).boundingBox(),
        calendar.locator('.ui-date-picker__day').first().boundingBox(),
      ])
      if (!calendarBounds || !previousBounds || !dayBounds) throw new Error('触控月历几何不可用')
      expect(calendarBounds.x).toBeGreaterThanOrEqual(0)
      expect(calendarBounds.y).toBeGreaterThanOrEqual(0)
      expect(calendarBounds.x + calendarBounds.width).toBeLessThanOrEqual(390)
      expect(calendarBounds.y + calendarBounds.height).toBeLessThanOrEqual(844)
      expect(Math.round(previousBounds.width)).toBeGreaterThanOrEqual(44)
      expect(Math.round(previousBounds.height)).toBeGreaterThanOrEqual(44)
      expect(Math.round(dayBounds.height)).toBeGreaterThanOrEqual(44)
      expect(dayBounds.width).toBeGreaterThanOrEqual(44)
      await page.keyboard.press('Escape')
      await expect(trigger).toBeFocused()
    }
  })

  test('Plan、模型、命令和 Rail 控件满足触控目标尺寸', async ({ page }) => {
    await mockStudio(page)
    expect(await page.evaluate(() => matchMedia('(any-pointer: coarse)').matches)).toBe(true)
    await expect(page.locator('.ui-overlay-scrollbar__thumb').first()).toHaveCSS('pointer-events', 'none')

    const input = page.getByRole('textbox', { name: '消息输入' })
    await input.fill('/plan')
    await input.press('Enter')
    await input.press('Enter')
    await expect(page.getByRole('button', { name: 'Plan 已开启，点击关闭' })).toBeVisible()

    await page.getByRole('button', { name: '选择模型' }).click()
    const modelHeights = await page.getByRole('option').evaluateAll((options) => (
      options.map((option) => option.getBoundingClientRect().height)
    ))
    expect(Math.min(...modelHeights)).toBeGreaterThanOrEqual(44)
    await page.keyboard.press('Escape')

    await input.fill('/')
    const commandHeights = await page.locator('.composer-suggestion-item').evaluateAll((items) => (
      items.map((item) => item.getBoundingClientRect().height)
    ))
    expect(commandHeights.length).toBeGreaterThan(0)
    expect(Math.min(...commandHeights)).toBeGreaterThanOrEqual(44)

    await page.setViewportSize({ width: 768, height: 844 })
    await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', 'rail')
    const railControls = await page.locator('.sidebar-rail > .ui-icon-button-wrap').evaluateAll((elements) => (
      elements.slice(0, 4).map((element) => {
        const bounds = element.getBoundingClientRect()
        return { top: bounds.top, right: bounds.right, bottom: bounds.bottom, left: bounds.left }
      })
    ))
    expect(railControls).toHaveLength(4)
    expect(Math.min(...railControls.map(({ right, left }) => right - left))).toBeGreaterThanOrEqual(44)
    expect(Math.min(...railControls.map(({ bottom, top }) => bottom - top))).toBeGreaterThanOrEqual(44)
    expect(Math.min(...railControls.slice(1).map(({ top }, index) => (
      top - railControls[index]!.bottom
    )))).toBeGreaterThanOrEqual(8)
  })
})


test('对话目录在四视口浅深主题定位已加载但未渲染的提问', async ({ page }) => {
  const conversationMessages: Message[] = Array.from({ length: 120 }, (_, index) => ([
    { id: `outline-question-${index}`, role: 'user' as const, content: `目录提问 ${index}`, createdAt: BASE_TIME },
    { id: `outline-answer-${index}`, role: 'assistant' as const, content: `目录回答 ${index}：这是公开回答摘要`, createdAt: BASE_TIME },
  ])).flat()
  await mockStudio(page, { conversationMessages, expectedMessageText: '目录提问 119' })
  for (const theme of ['light', 'dark']) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await page.emulateMedia({ reducedMotion: 'reduce' })
      const rail = page.getByRole('navigation', { name: '对话目录', exact: true })
      if (width === 1440) {
        await expect(rail).toBeVisible()
        await rail.getByRole('button', { name: '跳转到提问：目录提问 0', exact: true }).focus()
        await expect(page.getByRole('tooltip')).toContainText('目录回答 0')

        await rail.getByRole('button', { name: '跳转到提问：目录提问 0', exact: true }).press('Enter')
      } else {
        await page.getByRole('button', { name: '对话目录', exact: true }).click()
        const dialog = page.getByRole('dialog', { name: '对话目录' })
        await expect(dialog).toBeVisible()
        await expect(dialog).toContainText('仅显示已加载的对话')

        await dialog.getByRole('button').filter({ hasText: '目录提问 0' }).click()
        await expect(dialog).not.toBeVisible()
      }
      await expect(page.locator('#outline-question-0')).toBeFocused()
      const pane = page.getByRole('region', { name: '对话内容' })
      const targetBox = await page.locator('#outline-question-0').boundingBox()
      const paneBox = await pane.boundingBox()
      expect(targetBox!.y).toBeGreaterThanOrEqual(paneBox!.y)
      expect(targetBox!.y).toBeLessThan(paneBox!.y + 100)
      const insets = await pane.evaluate(element => {
        const bounds = element.getBoundingClientRect()
        const content = element.firstElementChild!.getBoundingClientRect()
        return { left: content.left - bounds.left, right: bounds.right - content.right }
      })
      expect(Math.abs(insets.left - insets.right)).toBeLessThanOrEqual(1)
      expect(insets.left).toBeGreaterThanOrEqual(24)

      await page.getByRole('button', { name: '回到底部', exact: true }).click()
      await expect(page.locator('#outline-question-119')).toBeVisible()
    }
  }
})


test('触屏对话目录保持完整点击区、模态隔离及关闭焦点恢复', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, hasTouch: true })
  try {
    const page = await context.newPage()
    await mockStudio(page, { conversationMessages: [
      { id: 'touch-first', role: 'user', content: '触屏第一个问题', createdAt: BASE_TIME },
      { id: 'touch-answer', role: 'assistant', content: '回答', createdAt: BASE_TIME },
      { id: 'touch-last', role: 'user', content: '触屏最后一个问题', createdAt: BASE_TIME },
    ], expectedMessageText: '触屏最后一个问题' })
    const trigger = page.getByRole('button', { name: '对话目录', exact: true })
    await expect(trigger).toBeVisible()
    const box = await trigger.boundingBox()
    expect(box!.width).toBeGreaterThanOrEqual(44)
    expect(box!.height).toBeGreaterThanOrEqual(44)
    await trigger.tap()
    await expect(page.locator('.app-shell')).toHaveAttribute('inert', '')
    const dialog = page.getByRole('dialog', { name: '对话目录' })
    await dialog.press('Escape')
    await expect(trigger).toBeFocused()
    await trigger.tap()
    await dialog.getByRole('button', { name: '触屏第一个问题 回答', exact: true }).tap()
    await expect(page.locator('#touch-first')).toBeFocused()
    await expect(page.locator('.app-shell')).not.toHaveAttribute('inert', '')
  } finally { await context.close() }
})

test('设置在窄屏按内容收紧并保持分类与内容相邻', async ({ page }) => {
  await mockStudio(page)
  await page.getByRole('button', { name: '打开用户菜单' }).click()
  await page.getByRole('menuitem', { name: '设置' }).click()
  const dialog = page.getByRole('dialog', { name: '设置' })
  for (const theme of ['light', 'dark']) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      for (const section of ['账号管理', '通用']) {
        await dialog.getByRole('button', { name: section, exact: true }).click()
        const bounds = await dialog.boundingBox()
        expect(bounds!.y).toBeGreaterThanOrEqual(0)
        expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(900)
        if (width === 320) {
          const nav = await dialog.getByRole('navigation', { name: '设置分类' }).boundingBox()
          const content = await dialog.getByRole('region', { name: '设置', exact: true }).boundingBox()
          expect(nav!.height).toBeLessThanOrEqual(64)
          expect(Math.abs(content!.y - nav!.y - nav!.height)).toBeLessThanOrEqual(1)
          expect(bounds!.height).toBeLessThan(650)
        }

      }
    }
  }
})

for (const { approval, touch } of [{ approval: false, touch: false }, { approval: true, touch: false }, { approval: true, touch: true }]) {
  test(`辅助操作显隐保持目录与阅读区稳定：${touch ? '触控审批' : approval ? '审批' : '普通输入'}`, async ({ page }) => {
    await page.clock.install()
    if (touch) {
      const cdp = await page.context().newCDPSession(page)
      await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
    }
    const conversationMessages: Message[] = Array.from({ length: 20 }, (_, index) => ([
      { id: `stable-question-${index}`, role: 'user' as const, content: `稳定布局提问 ${index}`, createdAt: BASE_TIME },
      { id: `stable-answer-${index}`, role: 'assistant' as const, content: `稳定布局回答 ${index}`, createdAt: BASE_TIME },
    ])).flat()
    await mockStudio(page, { approval, conversationMessages: approval ? [...conversationMessages, ...approvalMessages] : conversationMessages, expectedMessageText: '稳定布局提问 19' })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const pane = page.locator('.conversation-pane')
    const measure = () => page.evaluate(() => ['.composer-dock', '.conversation-region', '.conversation-navigation'].map(selector => {
      const rect = document.querySelector(selector)!.getBoundingClientRect()
      return { y: rect.y, height: rect.height }
    }))
    for (const theme of ['light', 'dark']) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value }, theme)
      for (const width of [320, 768, 1024, 1440]) {
        await page.setViewportSize({ width, height: 900 })
        await pane.evaluate(element => { element.scrollTop = element.scrollHeight; element.dispatchEvent(new Event('scroll', { bubbles: true })) })
        const button = page.getByRole('button', { name: '回到底部', exact: true })
        await expect(button).toHaveCount(0)
        const baseline = await measure()
        await pane.evaluate(element => {
          element.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true }))
          element.scrollTop = 0
          element.dispatchEvent(new Event('scroll', { bubbles: true }))
        })
        await expect(button).toBeVisible()
        await button.focus()
        expect(await measure()).toEqual(baseline)
        const control = await button.boundingBox()
        if (touch) {
          expect(control!.width).toBeGreaterThanOrEqual(44)
          expect(control!.height).toBeGreaterThanOrEqual(44)
        }
        const content = await page.locator(approval ? '.approval-composer' : '.composer').boundingBox()
        expect(control!.y + control!.height).toBeLessThanOrEqual(content!.y)
        expect(control!.x).toBeGreaterThanOrEqual(0)
        expect(control!.x + control!.width).toBeLessThanOrEqual(width)

        await page.getByRole('tab', { name: '对话', exact: true }).click()
        await page.clock.fastForward(2_000)
        await expect(button).toHaveCount(0)
        expect(await measure()).toEqual(baseline)
      }
    }
  })
}

test('审批确认按钮在浅深主题与四尺寸保持中性色实心胶囊样式', async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await mockStudio(page, { approval: true })
  const allow = page.getByRole('region', { name: '等待审批' }).getByRole('button', { name: '允许', exact: true })
  for (const theme of ['light', 'dark']) {
    await page.evaluate((value) => { document.documentElement.dataset.theme = value }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await expect(allow).toBeVisible()
      const styles = await allow.evaluate((node) => {
        const probe = document.createElement('span')
        probe.style.background = 'var(--color-text-primary)'
        probe.style.color = 'var(--color-layer-1)'
        node.append(probe)
        const expected = { background: getComputedStyle(probe).backgroundColor, color: getComputedStyle(probe).color }
        probe.remove()
        const current = getComputedStyle(node)
        return { expected, background: current.backgroundColor, color: current.color, radius: Number.parseFloat(current.borderRadius) }
      })
      expect(styles.background).toBe(styles.expected.background)
      expect(styles.color).toBe(styles.expected.color)
      expect(styles.radius).toBeGreaterThanOrEqual(999)
    }
    await page.screenshot({ path: testInfo.outputPath(`approval-solid-${theme}.png`) })
  }
})

test('工具图标、展开箭头和错误状态点共用第一行图标中心线', async ({ page }, testInfo) => {
  const tools = (prefix: string, runId: string): Message[] => (['completed', 'failed', 'cancelled'] as const).map((status, index) => ({
    id: `${prefix}-${index}`, role: 'tool', content: 'glob', createdAt: BASE_TIME,
    meta: {
      toolName: 'glob', toolCallId: `${prefix}-${index}`, runId,
      params: '{"pattern":"**/AGENTS.md"}', result: index ? 'Error: file_not_found' : '/AGENTS.md',
      status,
    },
  }))
  await mockStudio(page, {
    conversationMessages: [
      { id: 'align-user', role: 'user', content: '图标中心线检查', createdAt: BASE_TIME },
      ...tools('align-main', 'browser-run'),
      { id: 'align-agent', role: 'subagent', content: '', createdAt: BASE_TIME,
        meta: { agentName: 'researcher', status: 'completed', subRunId: 'align-subrun', result: '完成' } },
      ...tools('align-child', 'align-subrun'),
    ],
    expectedMessageText: '图标中心线检查',
  })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.locator('#align-agent > summary').click()
  for (const theme of ['light', 'dark']) {
    await page.evaluate(theme => {
      document.documentElement.dataset.theme = theme
      document.documentElement.style.colorScheme = theme
    }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      for (const prefix of ['align-main', 'align-child']) {
        const normal = page.locator(`#${prefix}-0 > summary`)
        const icon = normal.locator('.tool-row-icon svg')
        const center = async (target: Locator) => target.evaluate(el => {
          const rect = el.getBoundingClientRect()
          return rect.left + rect.width / 2
        })
        const baseline = await center(icon)
        expect(await icon.evaluate(el => el.getBoundingClientRect().left))
          .toBe(await normal.locator('.tool-row-leading').evaluate(el => el.getBoundingClientRect().left))
        for (const index of [1, 2]) {
          expect(await center(page.locator(`#${prefix}-${index} .tool-row-state-dot`))).toBe(baseline)
        }
        await normal.hover()
        const arrow = normal.locator('.tool-row-chevron')
        await expect(arrow).toHaveCSS('opacity', '1')
        expect(await center(arrow)).toBe(baseline)
        await normal.click()
        await expect(page.locator(`#${prefix}-0`)).toHaveAttribute('open', '')
        expect(await center(arrow)).toBe(baseline)
        await normal.click()
      }
      await page.mouse.move(0, 0)
      await page.screenshot({ path: testInfo.outputPath(`icon-centers-${theme}-${width}.png`) })
    }
  }
})

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef } from 'react'
import { expect, it, vi } from 'vitest'
import { buildEmptyConversation } from '../../../lib/workspace'
import type { Message } from '../../../types'
import { buildConversationDisplayEntries } from '../../conversation/todoTrace/displayEntries'
import { ConversationViewport } from './ConversationViewport'

it('已提交审批准备期间沿用运行指示，失败原因常驻', () => {
  const conversation = {
    ...buildEmptyConversation({ projectId: 'project', threadId: 'thread', now: '2026-10-12T00:00:00Z' }),
    runStatus: 'streaming' as const, runPhase: 'preparing' as const, activeRunId: 'resume',
    messages: [{ id: 'question', role: 'user' as const, content: '写入结果', createdAt: '2026-10-12T00:00:00Z' }],
    approval: { items: [], activeIndex: 0, submitted: true, mode: 'options' as const },
  }
  const props = {
    conversation, entries: [], hasEarlierMessages: false, childToolsByRunId: new Map(),
    paneRef: createRef<HTMLElement>(), messageEndRef: createRef<HTMLDivElement>(),
    historyStatus: 'ready' as const, isHistoryBootstrapped: true, isInitialHistoryUnavailable: false,
    isHydrating: false, isHydrationFailed: false, isRunning: true,
    onScroll: vi.fn(), onUserScrollIntent: vi.fn(), onRetryHistory: vi.fn(), onRetryHydration: vi.fn(), onLoadEarlierMessages: vi.fn(),
  }
  const { rerender } = render(<ConversationViewport {...props} />)
  expect(screen.getByRole('status', { name: '任务仍在继续' })).toBeVisible()
  expect(screen.queryByText('正在准备运行环境')).not.toBeInTheDocument()
  rerender(<ConversationViewport {...props} isRunning={false} conversation={{
    ...conversation, runStatus: 'error', runPhase: undefined,
    notice: { kind: 'error', content: '运行环境已暂停，请恢复沙箱后重试' },
  }} />)
  expect(screen.queryByRole('status', { name: '任务仍在继续' })).not.toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('运行环境已暂停，请恢复沙箱后重试')
})

it.each([
  { phase: 'generating', activeRunId: 'compact', answerRunning: false, copyable: true },
  { phase: 'saving', activeRunId: 'compact', answerRunning: false, copyable: true },
  { phase: 'compacted', activeRunId: undefined, answerRunning: false, copyable: true },
  { phase: 'compacted', activeRunId: 'chat', answerRunning: false, copyable: false },
  { phase: 'generating', activeRunId: 'compact', answerRunning: true, copyable: false },
] as const)('压缩状态 $phase、活动运行 $activeRunId、回答生成中 $answerRunning 时复制资格为 $copyable', async ({ phase, activeRunId, answerRunning, copyable }) => {
  const user = userEvent.setup()
  const writeText = vi.spyOn(navigator.clipboard, 'writeText')
  const createdAt = '2026-10-16T00:00:00Z'
  const messages: Message[] = [
    { id: 'question', role: 'user', content: '请说明自动化会话', createdAt },
    { id: 'answer', role: 'assistant', content: '后台执行、完成通知和关联会话', createdAt,
      meta: { status: answerRunning ? 'running' : 'completed' } },
  ]
  const conversation = {
    ...buildEmptyConversation({ projectId: 'project', threadId: 'thread', now: createdAt }),
    messages, activeRunId,
    runStatus: activeRunId ? 'streaming' as const : 'idle' as const,
    compactions: [{ runId: 'compact', createdAt, afterMessageId: 'answer', status: phase }],
  }
  render(<ConversationViewport
    conversation={conversation}
    entries={buildConversationDisplayEntries(conversation)}
    hasEarlierMessages={false}
    childToolsByRunId={new Map()}
    paneRef={createRef<HTMLElement>()}
    messageEndRef={createRef<HTMLDivElement>()}
    historyStatus="ready"
    isHistoryBootstrapped
    isInitialHistoryUnavailable={false}
    isHydrating={false}
    isHydrationFailed={false}
    isRunning={Boolean(activeRunId)}
    onScroll={vi.fn()}
    onUserScrollIntent={vi.fn()}
    onRetryHistory={vi.fn()}
    onRetryHydration={vi.fn()}
    onLoadEarlierMessages={vi.fn()}
  />)
  expect(screen.getByText(messages[1].content)).toBeVisible()
  if (copyable) {
    const copy = screen.getByRole('button', { name: '复制回答' })
    expect(copy).toBeVisible()
    await user.click(copy)
    expect(writeText).toHaveBeenCalledWith(messages[1].content)
    expect(screen.getByRole('button', { name: '回答已复制' })).toBeVisible()
  } else {
    expect(screen.queryByRole('button', { name: '复制回答' })).not.toBeInTheDocument()
  }
})

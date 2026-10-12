import { render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { expect, it, vi } from 'vitest'
import { buildEmptyConversation } from '../../../lib/workspace'
import { ConversationViewport } from './ConversationViewport'

it('已提交审批等待环境准备时显示可见进度，失败原因常驻', () => {
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
  expect(screen.getByText('正在准备运行环境')).toBeVisible()
  rerender(<ConversationViewport {...props} isRunning={false} conversation={{
    ...conversation, runStatus: 'error', runPhase: undefined,
    notice: { kind: 'error', content: '运行环境已暂停，请恢复沙箱后重试' },
  }} />)
  expect(screen.queryByText('正在准备运行环境')).not.toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('运行环境已暂停，请恢复沙箱后重试')
})

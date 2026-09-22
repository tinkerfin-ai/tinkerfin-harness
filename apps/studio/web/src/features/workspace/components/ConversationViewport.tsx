import { useMemo, type ReactNode, type RefObject } from 'react'

import { Button, ErrorBoundary, FeedbackState, OverlayScrollbar } from '../../../components/ui'
import type {
  Conversation,
  Message,
} from '../../../types'
import { ConversationRunFailure } from '../../conversation/components/ConversationRunFailure'
import { CompactionCard } from '../../conversation/compaction/CompactionCard'
import { ActivityDots } from '../../conversation/components/ActivityDots'
import { ApprovalStatusRow } from '../../conversation/components/ApprovalCard'
import { MessageBlock, ToolCallBatch } from '../../conversation/components/MessageBlock'
import { PlanQuestionStatusRow } from '../../conversation/components/PlanQuestionComposer'
import { PlanReviewStatusRow } from '../../conversation/components/PlanReviewCard'
import { TodoGroupRow } from '../../conversation/todoTrace/components/TodoGroupRow'
import type { ConversationDisplayEntry } from '../../conversation/todoTrace/displayEntries'
import { EmptyConversation } from './EmptyConversation'
import { useI18n } from '../../../i18n'

function collectCopyableAssistantIds(entries: ConversationDisplayEntry[], currentTurnRunning: boolean) {
  const copyableIds = new Set<string>()
  let candidate: Message | null = null
  const finishTurn = () => {
    if (candidate) copyableIds.add(candidate.id)
  }

  for (const entry of entries) {
    if (entry.type === 'run-failure' || entry.type === 'compaction') continue
    if (entry.type === 'tools') {
      candidate = null
      continue
    }
    if (entry.type === 'todo-group') {
      candidate = null
      continue
    }
    if (entry.message.role === 'user') {
      finishTurn()
      candidate = null
      continue
    }
    if (entry.message.role === 'process') continue
    if (entry.message.role === 'assistant') {
      candidate = entry.message.content && entry.message.meta?.status !== 'running'
        ? entry.message
        : null
      continue
    }
    candidate = null
  }

  // 当前轮结束前不暴露阶段性回答操作，历史轮次仍保留各自的最终回答操作
  if (!currentTurnRunning) finishTurn()
  return copyableIds
}

export function ConversationViewport({
  widthHandles,
  conversation,
  entries,
  hasEarlierMessages,
  childToolsByRunId,
  paneRef,
  messageEndRef,
  historyStatus,
  isHistoryBootstrapped,
  isInitialHistoryUnavailable,
  isHydrating,
  isHydrationFailed,
  isRunning,
  backgroundInert = false,
  navigation,
  onScroll,
  onUserScrollIntent,
  onRetryHistory,
  onRetryHydration,
  onRecoverConversation,
  onRetryRun,
  retryDisabled = false,
  onError,
  onLoadEarlierMessages,
  onRetryReadingPosition,
}: {
  widthHandles?: ReactNode
  conversation: Conversation
  entries: ConversationDisplayEntry[]
  hasEarlierMessages: boolean
  childToolsByRunId: Map<string, Message[]>
  paneRef: RefObject<HTMLElement | null>
  messageEndRef: RefObject<HTMLDivElement | null>
  historyStatus: 'loading' | 'ready' | 'error'
  isHistoryBootstrapped: boolean
  isInitialHistoryUnavailable: boolean
  isHydrating: boolean
  isHydrationFailed: boolean
  isRunning: boolean
  backgroundInert?: boolean
  navigation?: ReactNode
  onScroll: (pane: HTMLElement) => void
  onUserScrollIntent: () => void
  onRetryHistory: () => void
  onRetryHydration: () => void
  onRecoverConversation?: () => void
  onRetryRun?: (message: Message) => void
  retryDisabled?: boolean
  onError?: (message: string) => void
  onRetryReadingPosition?: () => void
  onLoadEarlierMessages: (trigger: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  const isEmpty = conversation.messages.length === 0 && !conversation.compactions?.length && !conversation.notice
  const copyableAssistantIds = useMemo(
    () => collectCopyableAssistantIds(entries, isRunning),
    [entries, isRunning],
  )

  return (
    <ErrorBoundary
      onError={() => onError?.(t('对话区域无法显示'))}
      resetKey={conversation.threadId || 'draft'}
      fallback={({ reset }) => (
        <Button type="button" variant="text" onClick={reset}>{t('重新加载')}</Button>
      )}
    >
      <div
        id="conversation-panel"
        className="conversation-region"
        role="tabpanel"
        aria-label={t('对话')}
        aria-hidden={backgroundInert || undefined}
        inert={backgroundInert || undefined}
      >
        {/* 命名 section 是主对话滚动区，键盘滚动需要取消自动定位 */}
        {/* eslint-disable jsx-a11y/no-noninteractive-tabindex, jsx-a11y/no-noninteractive-element-interactions */}
        <section
          ref={paneRef}
          className={`conversation-pane ui-scrollbar${isEmpty ? ' is-empty' : ''}`}
          aria-label={t('对话内容')}
          tabIndex={0}
          onScroll={(event) => onScroll(event.currentTarget)}
          onWheel={onUserScrollIntent}
          onTouchStart={onUserScrollIntent}
          onKeyDown={(event) => {
            if (event.defaultPrevented) return
            // 控件上的空格用于激活操作，不能先清除其重试所需的阅读位置
            if (event.key === ' ' && event.target instanceof Element
              && event.target.closest('button, summary, [role="button"]')) return
            if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)
              && event.target instanceof Element
              && !event.target.closest('input, textarea, select, [contenteditable="true"]')) {
              onUserScrollIntent()
            }
          }}
        >
        {!isHistoryBootstrapped || historyStatus === 'loading' ? (
          <FeedbackState kind="loading" title={t('正在加载历史会话')} />
        ) : isInitialHistoryUnavailable ? (
          <Button type="button" variant="text" onClick={onRetryHistory}>{t('重新加载')}</Button>
        ) : isHydrating ? (
          <FeedbackState kind="loading" title={t('正在加载会话')} />
        ) : isHydrationFailed ? (
          <Button type="button" variant="text" onClick={onRetryHydration}>{t('重新加载')}</Button>
        ) : isEmpty ? (
          <EmptyConversation />
        ) : (
          <div className="message-list">
            {onRetryReadingPosition && (
              <Button type="button" variant="text" onClick={onRetryReadingPosition}>
                {t('重试恢复阅读位置')}
              </Button>
            )}
            {hasEarlierMessages && (
              <div className="message-history-loader">
                <Button
                  variant="text"
                  onClick={(event) => onLoadEarlierMessages(event.currentTarget)}
                >
                  {t('加载更早消息')}
                </Button>
              </div>
            )}
            {entries.map((entry) => entry.type === 'compaction'
              ? <CompactionCard key={`compaction:${entry.operation.runId}`} operation={entry.operation} onReload={isRunning ? undefined : onRetryHydration} />
              : entry.type === 'run-failure'
              ? <ConversationRunFailure id={`failure:${entry.failure.runId}`} key={`failure:${entry.failure.runId}`} retryable={entry.failure.retryable && !entry.message.meta?.contentOmitted && Boolean(entry.message.content || entry.message.attachments?.length)} disabled={retryDisabled} onRetry={onRetryRun ? () => onRetryRun(entry.message) : undefined} />
              : entry.type === 'tools'
              ? <ToolCallBatch key={`batch-${entry.messages[0].id}`} messages={entry.messages} />
              : entry.type === 'todo-group'
                ? <TodoGroupRow key={entry.group.id} group={entry.group} message={entry.message} />
                : <MessageBlock
                  key={entry.message.id}
                  message={entry.message}
                  showActions={copyableAssistantIds.has(entry.message.id)}
                  childTools={entry.message.meta?.subRunId
                    ? childToolsByRunId.get(entry.message.meta.subRunId) ?? []
                    : []}
                />)}
            {conversation.approval && !conversation.approval.submitted && <ApprovalStatusRow />}
            {conversation.planInteraction?.kind === 'questions' && (
              <PlanQuestionStatusRow interaction={conversation.planInteraction} />
            )}
            {conversation.planInteraction?.kind === 'review' && (
              <PlanReviewStatusRow interaction={conversation.planInteraction} />
            )}
            {(conversation.runStatus === 'detached' || (!conversation.threadId && conversation.runStatus === 'error') || conversation.notice?.recovery === 'history') && onRecoverConversation && (
              <Button type="button" variant="text" onClick={onRecoverConversation}>
                {t(conversation.notice?.recovery === 'history' ? '重新加载' : conversation.runStatus === 'error' ? '重试' : '恢复连接')}
              </Button>
            )}
            {isRunning && !conversation.compactions?.some(operation => operation.runId === conversation.activeRunId) && <p className="message-stream-tail stream-pending-tail"><ActivityDots label={t('任务仍在继续')} /></p>}
            <div ref={messageEndRef} />
          </div>
        )}
        </section>
        {/* eslint-enable jsx-a11y/no-noninteractive-tabindex, jsx-a11y/no-noninteractive-element-interactions */}
        {widthHandles}
        {navigation}
        <OverlayScrollbar
          viewportRef={paneRef}
          onUserScrollIntent={onUserScrollIntent}
        />
      </div>
    </ErrorBoundary>
  )
}

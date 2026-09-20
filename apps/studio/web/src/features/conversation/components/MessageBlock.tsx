import { PlanHistoryCard } from './PlanHistoryCard'
import { AttachmentList } from '../attachments/AttachmentList'
import {
  Bot,
  Check,
  ChevronDown,
  Copy,
  Pause,
  TriangleAlert,
  X,
} from 'lucide-react'
import { memo, useEffect, useId, useRef, useState } from 'react'

import { IconButton } from '../../../components/ui'
import type { Message } from '../../../types'
import { MarkdownContent } from './MarkdownContent'
import { useTypewriterText } from './useTypewriterText'
import { ToolCallRow } from './ToolCallRow'
import { COPY_FEEDBACK_DURATION_MS } from './copyFeedback'
import { useI18n } from '../../../i18n'

type MessageStatus = NonNullable<Message['meta']>['status']

function PlaceholderField({ status }: { status?: MessageStatus }) {
  const { t } = useI18n()
  const pendingLabel = status === 'paused'
    ? t('等待审批后执行')
    : status === 'running'
      ? t('工具字段加载中')
      : undefined
  return pendingLabel
    ? (
      <span
        className="tool-field-pending"
        role="status"
        aria-label={pendingLabel}
        aria-busy={status === 'running' ? true : undefined}
      >
        <i aria-hidden="true" />
      </span>
      )
    : <span className="tool-field-placeholder">—</span>
}

function CodeField({ value, status }: { value?: string; status?: MessageStatus }) {
  return value
    ? <pre className="tool-code-field"><code>{value}</code></pre>
    : <PlaceholderField status={status} />
}

function RichField({ value, status, className = 'tool-rich-field' }: { value?: string; status?: MessageStatus; className?: string }) {
  return value
    ? <div className={className}><MarkdownContent content={value} variant="compact" /></div>
    : <PlaceholderField status={status} />
}

function ToolDetails({ message }: { message: Message }) {
  const { t } = useI18n()
  return (
    <div className="tool-detail-card">
      <div className="tool-detail-section tool-detail-section--params">
        <span className="tool-field-label">{t('输入')}</span>
        <CodeField value={message.meta?.params} status={message.meta?.status} />
      </div>
      {(message.meta?.result || !message.attachments?.length) && <>
        <span className="tool-detail-divider" aria-hidden="true" />
        <div className="tool-detail-section tool-detail-section--result">
          <span className="tool-field-label">{t('输出')}</span>
          <RichField value={message.meta?.result} status={message.meta?.status} />
        </div>
      </>}
    </div>
  )
}

type MessageActionKind = 'user' | 'assistant'

function MessageActionRow({ content, kind }: { content: string; kind: MessageActionKind }) {
  const { t } = useI18n()
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const resetTimer = useRef<number | null>(null)

  useEffect(() => () => {
    if (resetTimer.current != null) window.clearTimeout(resetTimer.current)
  }, [])

  const copyMessage = async () => {
    if (resetTimer.current != null) window.clearTimeout(resetTimer.current)
    try {
      await navigator.clipboard.writeText(content)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
    resetTimer.current = window.setTimeout(() => setCopyState('idle'), COPY_FEEDBACK_DURATION_MS)
  }

  const labels = kind === 'user'
    ? {
        copied: t('消息已复制'),
        failed: t('复制消息失败'),
        idle: t('复制消息'),
        group: t('消息操作'),
      }
    : {
        copied: t('回答已复制'),
        failed: t('复制回答失败'),
        idle: t('复制回答'),
        group: t('回答操作'),
      }
  const label = labels[copyState]

  return (
    <footer
      className={`message-action-row message-action-row--${kind}`}
      role="group"
      aria-label={labels.group}
    >
      <IconButton
        label={label}
        tooltip={label}
        icon={copyState === 'copied'
          ? <Check size={20} />
          : copyState === 'failed'
            ? <TriangleAlert size={20} />
            : <Copy size={20} />}
        onClick={() => void copyMessage()}
      />
      <span className="message-action-status" aria-live="polite">
        {copyState === 'copied' ? t('已复制') : copyState === 'failed' ? t('复制失败，请重试') : ''}
      </span>
    </footer>
  )
}

function SubagentToolTraceRow({
  message,
  open,
  onOpenChange,
}: {
  message: Message
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <>
    <ToolCallRow
      message={message}
      className="subagent-tool-row"
      open={open}
      onOpenChange={onOpenChange}
    >
      <ToolDetails message={message} />
    </ToolCallRow>
    <AttachmentList attachments={message.attachments} />
    </>
  )
}

function SubagentOutputNode({ message }: { message: Message }) {
  const { t } = useI18n()
  const status = message.meta?.status ?? 'completed'
  const result = message.meta?.result
  const label = status === 'running'
    ? t('执行中')
    : status === 'failed'
      ? t('执行失败')
      : status === 'cancelled'
        ? t('已取消')
      : status === 'paused'
        ? t('等待审批')
        : t('已完成')

  return (
    <li className={`subagent-trace-node subagent-output-node is-${status}`}>
      <span className="subagent-trace-junction" aria-hidden="true" />
      <span className="subagent-output-icon" aria-hidden="true">
        {status === 'completed'
          ? <Check size={12} strokeWidth={2.5} />
          : status === 'failed' || status === 'cancelled'
            ? <X size={12} strokeWidth={2.5} />
            : status === 'paused'
              ? <Pause size={11} strokeWidth={2.5} />
              : <span className="subagent-output-pulse" />}
      </span>
      <div className="subagent-output-copy">
        <strong>{label}</strong>
        {result ? <RichField value={result} status={status} className="subagent-trace-output" /> : null}
        <AttachmentList attachments={message.attachments} />
      </div>
    </li>
  )
}

function SubagentCard({ message, childTools }: { message: Message; childTools: Message[] }) {
  const { t } = useI18n()
  const [openToolIds, setOpenToolIds] = useState<Set<string>>(() => new Set())
  const [inputHovered, setInputHovered] = useState(false)
  const [inputFocused, setInputFocused] = useState(false)
  const inputTooltipId = useId()
  const status = message.meta?.status ?? 'completed'
  const input = message.meta?.input
  const inputDetailOpen = inputHovered || inputFocused
  const agentName = message.meta?.agentName ?? 'subagent'
  const statusLabel = status === 'running'
    ? t('正在运行')
    : status === 'failed'
      ? t('执行失败')
      : status === 'cancelled'
        ? t('已取消')
      : status === 'paused'
        ? t('等待审批')
        : t('已完成')

  const setToolOpen = (toolId: string, open: boolean) => {
    setOpenToolIds((current) => {
      if (current.has(toolId) === open) return current
      const next = new Set(current)
      if (open) next.add(toolId)
      else next.delete(toolId)
      return next
    })
  }

  return (
    <details id={message.id} className={`subagent-card ${status}`}>
      <summary className="subagent-card-head">
        <span className="tool-row-leading" aria-hidden="true">
          <span className="tool-row-icon">
            {status === 'failed' || status === 'paused' || status === 'cancelled'
              ? <span className={`tool-row-state-dot is-${status}`} />
              : <Bot size={14} strokeWidth={2} />}
          </span>
          <ChevronDown className="tool-row-chevron" size={14} strokeWidth={2} />
        </span>
        <span className="tool-row-title">Task</span>
        <span className="tool-row-separator" aria-hidden="true" />
        <span className="tool-row-summary">SubAgent</span>
        {childTools.length > 0 && (
          <span className="subagent-card-meta">
            <span className="subagent-tool-count">{t('{count} 个工具', { count: childTools.length })}</span>
          </span>
        )}
        <span className="subagent-visually-hidden">{agentName}，{statusLabel}</span>
      </summary>
      <div className="subagent-card-body">
        {input && (
          <div className="subagent-task-line">
            <span>{agentName}</span>
            <div
              className={`subagent-task-detail${inputDetailOpen ? ' is-open' : ''}`}
              onMouseEnter={() => setInputHovered(true)}
              onMouseLeave={() => setInputHovered(false)}
            >
              <button
                type="button"
                className="subagent-task-summary"
                aria-describedby={inputDetailOpen ? inputTooltipId : undefined}
                onFocus={() => setInputFocused(true)}
                onBlur={() => setInputFocused(false)}
              >
                {input}
              </button>
              {inputDetailOpen && (
                <span
                  id={inputTooltipId}
                  className="ui-tooltip subagent-task-tooltip"
                  role="tooltip"
                >
                  {input}
                </span>
              )}
            </div>
          </div>
        )}
        <ol
          className={`subagent-trace-list${childTools.length === 0 ? ' is-tool-empty' : ''}`}
          aria-label={`${agentName} ${t('工具轨迹')}`}
        >
          {childTools.map((tool) => (
            <li key={tool.id} className="subagent-trace-node subagent-tool-node">
              <span className="subagent-trace-junction" aria-hidden="true" />
              <SubagentToolTraceRow
                message={tool}
                open={openToolIds.has(tool.id)}
                onOpenChange={(open) => setToolOpen(tool.id, open)}
              />
            </li>
          ))}
          <SubagentOutputNode message={message} />
        </ol>
      </div>
    </details>
  )
}

function MessageBlockView({
  message,
  childTools = [],
  showActions = true,
}: {
  message: Message
  childTools?: Message[]
  showActions?: boolean
}) {
  if (message.role === 'user') {
    return (
      <article id={message.id} className="message user-message">
        <AttachmentList attachments={message.attachments} />
        <MarkdownContent content={message.content} className="message-markdown" />
        <MessageActionRow content={message.content} kind="user" />
      </article>
    )
  }
  if (message.role === 'process') {
    return message.meta?.planHistory ? <PlanHistoryCard interaction={message.meta.planHistory} /> : null
  }
  if (message.role === 'subagent') {
    return <SubagentCard message={message} childTools={childTools} />
  }
  if (message.role === 'tool') {
    return <ToolCallCard message={message} />
  }
  if (message.role === 'error') {
    return null
  }
  return <AssistantMessage message={message} showActions={showActions} />
}

function AssistantMessage({ message, showActions }: { message: Message; showActions: boolean }) {
  const content = useTypewriterText(message.content, message.liveText, message.meta?.status !== 'running')
  if (!message.content && !message.attachments?.length) return null
  return (
    <article id={message.id} className="message assistant-message">
      <MarkdownContent content={content} className="message-markdown" />
      <AttachmentList attachments={message.attachments} />
      {showActions && content === message.content && message.meta?.status !== 'running' && <MessageActionRow content={message.content} kind="assistant" />}
    </article>
  )
}

const sameMessageReferences = (left: Message[], right: Message[]) =>
  left.length === right.length && left.every((message, index) => message === right[index])

export const MessageBlock = memo(
  MessageBlockView,
  (previous, next) => previous.message === next.message
    && sameMessageReferences(previous.childTools ?? [], next.childTools ?? [])
    && (previous.showActions ?? true) === (next.showActions ?? true),
)

export function ToolCallCard({ message, className }: { message: Message; className?: string }) {
  const [open, setOpen] = useState(false)
  const { t } = useI18n()
  const isTodoUpdate = message.meta?.toolName === 'write_todos'
  const todoStatus = message.meta?.status === 'failed'
    ? t('任务清单更新失败')
    : message.meta?.status === 'cancelled'
      ? t('任务清单更新已取消')
      : message.meta?.status === 'running' || message.meta?.status === 'paused'
        ? t('正在更新任务清单')
        : t('未生成任务清单')
  return (
    <>
    <ToolCallRow message={message} open={open} onOpenChange={setOpen} className={`tool-card${className ? ` ${className}` : ''}`}>
      {isTodoUpdate
        ? <div className="todo-trace-tool-status">{todoStatus}</div>
        : <ToolDetails message={message} />}
    </ToolCallRow>
    <AttachmentList attachments={message.attachments} />
    </>
  )
}

function ToolCallBatchView({ messages }: { messages: Message[] }) {
  const { t } = useI18n()
  if (messages.length === 1) return <ToolCallCard message={messages[0]} />
  return (
    <section className="tool-batch" aria-label={t('工具调用批次')}>
      {messages.map((message) => <ToolCallCard key={message.id} message={message} />)}
    </section>
  )
}

export const ToolCallBatch = memo(
  ToolCallBatchView,
  (previous, next) => sameMessageReferences(previous.messages, next.messages),
)

import {
  Bot,
  ChevronDown,
  CircleHelp,
  FilePenLine,
  FilePlus2,
  FileText,
  FolderSearch,
  FolderTree,
  Globe2,
  ListChecks,
  Pause,
  Play,
  SquareTerminal,
  TextSearch,
  Trash2,
  Wrench,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'

import type { Message } from '../../../types'
import { useI18n } from '../../../i18n'
import { isTranslationKey } from '../../../i18n/messages'

type MessageStatus = NonNullable<Message['meta']>['status']

interface ToolPresentation {
  title: string
  icon: LucideIcon
  summaryKeys: readonly string[]
}

const TOOL_PRESENTATIONS: Record<string, ToolPresentation> = {
  ls: { title: 'List', icon: FolderTree, summaryKeys: ['path'] },
  read_file: { title: 'Read', icon: FileText, summaryKeys: ['file_path', 'path'] },
  write_file: { title: 'Write', icon: FilePlus2, summaryKeys: ['file_path', 'path'] },
  edit_file: { title: 'Edit', icon: FilePenLine, summaryKeys: ['file_path', 'path'] },
  delete: { title: 'Delete', icon: Trash2, summaryKeys: ['file_path', 'path'] },
  glob: { title: 'Glob', icon: FolderSearch, summaryKeys: ['pattern', 'path'] },
  grep: { title: 'Grep', icon: TextSearch, summaryKeys: ['pattern', 'query', 'path'] },
  execute: { title: 'Execute', icon: SquareTerminal, summaryKeys: ['description', 'command'] },
  web_search: { title: 'Search', icon: Globe2, summaryKeys: ['query'] },
  write_todos: { title: 'Todos', icon: ListChecks, summaryKeys: [] },
  task: { title: 'Task', icon: Bot, summaryKeys: ['description', 'subagent_type'] },
  ask_user_question: { title: '提问', icon: CircleHelp, summaryKeys: [] },
  create_automation: { title: '创建任务', icon: FilePlus2, summaryKeys: ['name'] },
  update_automation: { title: '修改任务', icon: FilePenLine, summaryKeys: ['task_id'] },
  pause_automation: { title: '暂停任务', icon: Pause, summaryKeys: ['task_id'] },
  enable_automation: { title: '启用任务', icon: Play, summaryKeys: ['task_id'] },
  delete_automation: { title: '删除任务', icon: Trash2, summaryKeys: ['task_id'] },
  run_automation_task_now: { title: '立即运行任务', icon: Play, summaryKeys: ['task_id'] },
  get_automation: { title: '查看任务', icon: FileText, summaryKeys: ['task_id'] },
  list_automations: { title: '查找任务', icon: ListChecks, summaryKeys: ['query'] },
  list_automation_runs: { title: '查询任务执行', icon: ListChecks, summaryKeys: ['query', 'task_id'] },
  get_automation_run: { title: '读取运行结果', icon: FileText, summaryKeys: ['execution_id'] },
  deliver_automation_files: { title: '发送任务文件', icon: FileText, summaryKeys: ['execution_id'] },
}

const firstLine = (value: string) => value.split(/\r?\n/, 1)[0]?.trim() ?? ''

const parseParams = (value: string): Record<string, unknown> | null => {
  try {
    const parsed: unknown = JSON.parse(value)
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

const firstStringValue = (params: Record<string, unknown>, keys: readonly string[]) => {
  for (const key of keys) {
    const value = params[key]
    if (typeof value === 'string' && value.trim()) return firstLine(value)
  }
  for (const value of Object.values(params)) {
    if (typeof value === 'string' && value.trim()) return firstLine(value)
  }
  return ''
}

const toolSummary = (
  message: Message,
  toolName: string,
  presentation: ToolPresentation | undefined,
) => {
  const params = message.meta?.params ?? ''
  if (toolName === 'write_todos') return ''
  const parsed = params ? parseParams(params) : null
  let summary = ''

  if (parsed && toolName === 'ask_user_question') {
    const form = parsed.form
    if (form && typeof form === 'object' && 'title' in form && typeof form.title === 'string') {
      summary = firstLine(form.title)
    }
  }
  if (parsed && toolName === 'web_search' && Array.isArray(parsed.queries)) {
    summary = parsed.queries
      .filter((query): query is string => typeof query === 'string' && Boolean(query.trim()))
      .map(firstLine)
      .join(', ')
  }
  if (!summary && parsed) {
    summary = firstStringValue(parsed, presentation?.summaryKeys ?? [])
  }
  if (!summary && !parsed && params) {
    const input = params.trimStart()
    // JSON 结构和未接收完整的参数只在展开详情中显示
    if (!input.startsWith('{') && !input.startsWith('[')) summary = firstLine(input)
  }
  if (!summary) return presentation ? '' : toolName

  return presentation ? summary : `${toolName} · ${summary}`
}

const statusText = (status: MessageStatus | undefined, t: ReturnType<typeof useI18n>['t']) => {
  if (status === 'failed') return t('执行失败')
  if (status === 'cancelled') return t('已取消')
  if (status === 'paused') return t('等待审批')
  if (status === 'running') return t('正在运行')
  return t('已完成')
}

export function ToolCallRow({
  message,
  className,
  open,
  onOpenChange,
  presentationOverride,
  children,
}: {
  message: Message
  className?: string
  open?: boolean
  onOpenChange?: (open: boolean) => void
  presentationOverride?: {
    title: string
    summary?: string
    icon?: ReactNode
  }
  /** 没有详情时传入 null，标题行不提供展开操作 */
  children: ReactNode
}) {
  const { t } = useI18n()
  const toolName = message.meta?.toolName?.trim() || 'tool'
  const presentation = TOOL_PRESENTATIONS[toolName]
  const title = presentation?.title ?? 'Tool call'
  const ToolIcon = presentation?.icon ?? Wrench
  const status = message.meta?.status ?? 'completed'
  const failure = status === 'failed' && Boolean(message.meta?.result)
    ? firstLine(message.meta?.result ?? '')
    : ''
  const summary = presentationOverride?.summary !== undefined
    ? presentationOverride.summary
    : failure || toolSummary(message, toolName, presentation)
  const hasDetails = children != null && children !== false
  const rowProps = {
    id: message.id,
    className: `tool-row ${status}${className ? ` ${className}` : ''}`,
    'data-tool-name': toolName,
  }
  const heading = <>
    <span className="tool-row-visually-hidden">{statusText(status, t)}</span>
    <span className="tool-row-leading" aria-hidden="true">
      <span className="tool-row-icon">
        {status === 'failed' || status === 'cancelled'
          ? <span className={`tool-row-state-dot is-${status}`} />
          : presentationOverride?.icon ?? <ToolIcon size={14} strokeWidth={2} />}
      </span>
      {hasDetails && <ChevronDown className="tool-row-chevron" size={14} strokeWidth={2} />}
    </span>
    <span className="tool-row-title">
      {presentationOverride?.title ?? (isTranslationKey(title) ? t(title) : title)}
    </span>
    {summary
      ? <>
          <span className="tool-row-separator" aria-hidden="true" />
          <span className={`tool-row-summary${failure ? ' is-error' : ''}`}>{summary}</span>
        </>
      : null}
  </>

  if (!hasDetails) return <div {...rowProps}><div className="tool-row-heading">{heading}</div></div>

  return (
    <details
      {...rowProps}
      open={open}
      onToggle={(event) => {
        const row = event.currentTarget
        onOpenChange?.(row.open)
        if (!row.open) return
        // 展开后的真实高度下一帧才稳定，只滚动到刚好避开输入区的位置
        window.requestAnimationFrame(() => {
          if (!row.open || !row.isConnected) return
          row.scrollIntoView?.({ behavior: 'auto', block: 'nearest' })
        })
      }}
    >
      <summary>{heading}</summary>
      {children}
    </details>
  )
}

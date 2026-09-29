import { ChevronDown } from 'lucide-react'
import type { ReactNode } from 'react'

/** 工具与上下文共用的详情入口；标题和状态含义由各自的消息类型提供 */
export function ConversationDetailRow({
  title, summary, icon, statusLabel, open, onOpenChange, children, className, ...attributes
}: {
  id: string
  title: string
  summary?: ReactNode
  icon: ReactNode
  statusLabel?: string
  open?: boolean
  onOpenChange?: (open: boolean) => void
  children: ReactNode
  className?: string
  'data-tool-name'?: string
}) {
  const hasDetails = children != null && children !== false
  const heading = <>
    {statusLabel && <span className="visually-hidden">{statusLabel}</span>}
    <span className="conversation-detail-leading" aria-hidden="true">
      <span className="conversation-detail-icon">{icon}</span>
      {hasDetails && <ChevronDown className="conversation-detail-chevron" size={14} strokeWidth={2} />}
    </span>
    <span className="conversation-detail-title">{title}</span>
    {summary ? <>
      <span className="conversation-detail-separator" aria-hidden="true" />
      <span className="conversation-detail-summary">{summary}</span>
    </> : null}
  </>
  const rowClass = `conversation-detail-row${className ? ` ${className}` : ''}`
  if (!hasDetails) return <div {...attributes} className={rowClass}><div className="conversation-detail-heading">{heading}</div></div>
  return <details {...attributes} className={rowClass} open={open} onToggle={event => {
    const row = event.currentTarget
    onOpenChange?.(row.open)
    if (!row.open) return
    window.requestAnimationFrame(() => {
      if (row.open && row.isConnected) row.scrollIntoView?.({ behavior: 'auto', block: 'nearest' })
    })
  }}>
    <summary>{heading}</summary>
    {children}
  </details>
}

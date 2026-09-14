import {
  ArrowUpRight,
  ChevronDown,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

import type { TodoGroup } from '../../../../api/conversation/taskTrace'
import { DrawerHeader, OverlayScrollbar } from '../../../../components/ui'
import { useI18n } from '../../../../i18n'
import { todoProgress } from '../domain'
import { useTodoGroupWindow } from '../useTodoGroupWindow'
import { TodoTree } from './TodoTree'

const relativeTime = (createdAt: string, now: number, locale: string) => {
  const deltaSeconds = (Date.parse(createdAt) - now) / 1000
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  if (Math.abs(deltaSeconds) < 3_600) return formatter.format(Math.round(deltaSeconds / 60), 'minute')
  if (Math.abs(deltaSeconds) < 86_400) return formatter.format(Math.round(deltaSeconds / 3_600), 'hour')
  return formatter.format(Math.round(deltaSeconds / 86_400), 'day')
}

export function TodoTraceDrawer({
  groups,
  open,
  usesOverlay,
  openEpoch,
  drawerRef,
  onClose,
  onLocate,
}: {
  groups: readonly TodoGroup[]
  open: boolean
  usesOverlay: boolean
  openEpoch: number
  drawerRef: RefObject<HTMLElement | null>
  onClose: () => void
  onLocate: (group: TodoGroup) => void
}) {
  const { t, locale } = useI18n()
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set())
  const [now, setNow] = useState(Date.now())
  const viewportRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const groupsRef = useRef(groups)
  const currentGroupId = groups[0]?.status === 'running' ? groups[0].id : undefined
  const firstHistoryId = groups.find((group) => group.id !== currentGroupId)?.id
  const previousCurrentIdRef = useRef(currentGroupId)
  const buttonRefs = useRef(new Map<string, HTMLButtonElement>())
  groupsRef.current = groups
  const windowed = useTodoGroupWindow({ groups, expandedIds, viewportRef })

  useEffect(() => {
    if (!open) return
    const latest = groupsRef.current[0]
    setExpandedIds(latest?.status === 'running' ? new Set([latest.id]) : new Set())
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(timer)
  }, [open, openEpoch])

  useEffect(() => {
    const previousCurrentId = previousCurrentIdRef.current
    previousCurrentIdRef.current = currentGroupId
    if (!open || previousCurrentId === currentGroupId) return
    setExpandedIds((current) => {
      const next = new Set(current)
      if (previousCurrentId) next.delete(previousCurrentId)
      if (currentGroupId) next.add(currentGroupId)
      return next
    })
  }, [currentGroupId, open])

  useEffect(() => {
    if (!open || currentGroupId) return
    setExpandedIds(new Set())
  }, [currentGroupId, open])

  useEffect(() => {
    if (!open || !usesOverlay) return
    const frame = window.requestAnimationFrame(() => closeRef.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [open, openEpoch, usesOverlay])

  useEffect(() => {
    const availableIds = new Set(groups.map((group) => group.id))
    setExpandedIds((current) => {
      const next = new Set([...current].filter((id) => availableIds.has(id)))
      return next.size === current.size ? current : next
    })
  }, [groups])

  const moveFocus = (currentIndex: number, key: string) => {
    const page = Math.max(1, Math.floor((viewportRef.current?.clientHeight ?? 720) / 88))
    const target = key === 'Home'
      ? 0
      : key === 'End'
        ? groups.length - 1
        : key === 'PageUp'
          ? currentIndex - page
          : key === 'PageDown'
            ? currentIndex + page
            : key === 'ArrowUp'
              ? currentIndex - 1
              : currentIndex + 1
    const index = Math.max(0, Math.min(groups.length - 1, target))
    const id = groups[index]?.id
    if (!id) return
    windowed.scrollToIndex(index)
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => buttonRefs.current.get(id)?.focus())
    })
  }

  const labels: Record<TodoGroup['status'], string> = {
    running: t('执行中'),
    completed: t('已完成'),
    incomplete: t('未确认完成'),
    failed: t('失败'),
    cancelled: t('已取消'),
  }
  const announcedGroup = groups.find((group) => expandedIds.has(group.id)) ?? groups[0]
  const announcedProgress = announcedGroup ? todoProgress(announcedGroup) : undefined

  return (
    <aside
      ref={drawerRef}
      data-workspace-layout-target="todo-trace-drawer"
      id="todo-trace-drawer"
      className={`todo-trace-drawer${open ? ' is-open' : ''}`}
      aria-label={t('任务轨迹')}
      aria-hidden={!open || undefined}
      inert={!open || undefined}
    >
      <p className="visually-hidden" aria-live="polite" aria-atomic="true">
        {announcedGroup && announcedProgress
          ? t('任务组状态：{status}', {
              status: `${labels[announcedGroup.status]} ${announcedProgress.completed}/${announcedProgress.total}`,
            })
          : t('没有已确认的任务轨迹')}
      </p>
      <DrawerHeader
        ref={closeRef}
        title={t('任务轨迹')}
        description={t('当前会话 · {count} 组', { count: groups.length })}
        closeLabel={t('关闭任务轨迹')}
        onClose={onClose}
      />
      <div className="todo-trace-drawer-region">
        <div
          ref={viewportRef}
          className="todo-trace-scroll ui-scrollbar"
          role="region"
          tabIndex={0}
          aria-label={t('任务轨迹')}
        >
          <ol
            className="todo-trace-group-list"
            style={{ height: windowed.totalHeight }}
          >
            {windowed.items.map(({ group, index, top }) => {
              const expanded = expandedIds.has(group.id)
              const progress = todoProgress(group)
              const isCurrent = group.id === currentGroupId
              const startsHistory = group.id === firstHistoryId
              const panelId = `todo-trace-panel-${encodeURIComponent(group.id)}`
              const fullTime = new Intl.DateTimeFormat(locale, {
                dateStyle: 'medium',
                timeStyle: 'medium',
              }).format(new Date(group.createdAt))
              const ago = relativeTime(group.createdAt, now, locale)
              return (
                <li
                  key={group.id}
                  ref={(element) => windowed.registerRow(group.id, element)}
                  className={`todo-trace-group is-${group.status}${expanded ? ' is-expanded' : ''}${isCurrent ? ' is-current' : ' is-history'}`}
                  style={{ transform: `translateY(${top}px)` }}
                  aria-setsize={groups.length}
                  aria-posinset={index + 1}
                >
                  {isCurrent && <h3 className="todo-trace-section-title">{t('当前')}</h3>}
                  {startsHistory && <h3 className="todo-trace-section-title">{t('历史')}</h3>}
                  <div className="todo-trace-group-surface">
                    <div className="todo-trace-group-head">
                      <button
                        ref={(element) => {
                          if (element) buttonRefs.current.set(group.id, element)
                          else buttonRefs.current.delete(group.id)
                        }}
                        type="button"
                        className="todo-trace-group-toggle"
                        aria-expanded={expanded}
                        aria-controls={panelId}
                        aria-label={t(expanded ? '收起任务组：{preview}' : '展开任务组：{preview}', {
                          preview: group.userMessagePreview,
                        })}
                        onClick={() => setExpandedIds((current) => {
                          const next = new Set(current)
                          if (expanded) next.delete(group.id)
                          else next.add(group.id)
                          return next
                        })}
                        onKeyDown={(event) => {
                          if (!['ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) return
                          event.preventDefault()
                          moveFocus(index, event.key)
                        }}
                      >
                        <ChevronDown size={14} className="todo-trace-group-chevron" aria-hidden="true" />
                        <span className="todo-trace-group-copy">
                          <strong className="todo-trace-group-title">{group.userMessagePreview}</strong>
                          {!isCurrent && (
                            <span className="todo-trace-group-meta">
                              <time
                                dateTime={group.createdAt}
                                aria-label={`${fullTime}，${ago}`}
                              >
                                {ago}
                              </time>
                              <span>{labels[group.status]}</span>
                            </span>
                          )}
                        </span>
                        <span className="todo-trace-group-progress">{progress.completed}/{progress.total}</span>
                      </button>
                    </div>
                    {expanded && (
                      <div id={panelId} className="todo-trace-group-panel">
                        <TodoTree group={group} />
                        <button
                          type="button"
                          className="todo-trace-locate"
                          aria-label={t('定位到对话：{preview}', {
                            preview: group.userMessagePreview,
                          })}
                          onClick={() => onLocate(group)}
                        >
                          <span>{t('定位到对话')}</span>
                          <ArrowUpRight size={14} aria-hidden="true" />
                        </button>
                      </div>
                    )}
                  </div>
                </li>
              )
            })}
          </ol>
        </div>
        <OverlayScrollbar viewportRef={viewportRef} />
      </div>
    </aside>
  )
}

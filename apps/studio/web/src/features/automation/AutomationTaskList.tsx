import { AlarmClock, MoreHorizontal, Play } from 'lucide-react'
import { useLayoutEffect, useRef, useState } from 'react'

import { Button, IconButton } from '../../components/ui'
import { useI18n } from '../../i18n'
import { beijingDate, beijingTime, type AutomationTask } from './model'
import { formatDate, formatSchedule } from './presentation'

export function AutomationTaskList({ tasks, query, status, busy, onEdit, onExecute, onToggle, onPause, onDelete, onClearFilter }: {
  tasks: AutomationTask[]
  query: string
  status: 'all' | 'enabled' | 'paused'
  busy: ReadonlySet<string>
  onEdit: (task: AutomationTask, trigger: HTMLElement) => void
  onExecute: (id: string) => void
  onToggle: (id: string) => void
  onPause: (ids: ReadonlySet<string>) => Promise<ReadonlySet<string>>
  onDelete: (ids: ReadonlySet<string>, trigger: HTMLElement) => Promise<ReadonlySet<string>>
  onClearFilter: () => void
}) {
  const { locale, t } = useI18n()
  const [batch, setBatch] = useState(false)
  const [selected, setSelected] = useState(new Set<string>())
  const allRef = useRef<HTMLInputElement>(null)
  const visible = tasks
  const selectedVisible = visible.filter((task) => selected.has(task.id)).length
  useLayoutEffect(() => {
    if (allRef.current) allRef.current.indeterminate = selectedVisible > 0 && selectedVisible < visible.length
  }, [selectedVisible, visible.length, batch])

  return <>
    <div className="automation-toolbar automation-task-toolbar">
      {batch && <div className="automation-batch-actions">
        <label className="automation-select"><input ref={allRef} type="checkbox"
          aria-label={t('全选当前任务')} checked={visible.length > 0 && selectedVisible === visible.length}
          onChange={(event) => setSelected((current) => {
            const next = new Set(current)
            visible.forEach((task) => event.target.checked ? next.add(task.id) : next.delete(task.id))
            return next
          })} /></label><span className="automation-caption">{t('已选 {count} 项', { count: selected.size })}</span>
      </div>}
      <div className="automation-batch-actions">
        {batch ? <>
          <Button type="button" size="sm" variant="ghost" disabled={!selected.size || busy.size > 0} onClick={() => { void onPause(selected).then(ids => setSelected(new Set(ids))) }}>{t('暂停')}</Button>
          <Button type="button" size="sm" variant="danger" disabled={!selected.size || busy.size > 0} onClick={event => { void onDelete(selected, event.currentTarget).then(ids => setSelected(new Set(ids))) }}>{t('删除')}</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => { setBatch(false); setSelected(new Set()) }}>{t('完成选择')}</Button>
        </> : <IconButton size="sm" label={t('批量选择')} icon={<MoreHorizontal size={18} />} onClick={() => setBatch(true)} />}
      </div>
    </div>
    {visible.length ? visible.map((task) => {
      const next = task.nextRunAt ? Date.parse(task.nextRunAt) : undefined
      return <article key={task.id} className="automation-task-row">
        {batch ? <label className="automation-select"><input type="checkbox" aria-label={t('选择 {name}', { name: task.name })}
          checked={selected.has(task.id)} onChange={(event) => setSelected((current) => {
            const next = new Set(current)
            if (event.target.checked) next.add(task.id)
            else next.delete(task.id)
            return next
          })} /></label> : null}
        <Button type="button" variant="ghost" className="automation-task-edit" aria-label={task.name}
          onClick={(event) => onEdit(task, event.currentTarget)}>
          <span className="automation-task-edit-content">
            {!batch && <AlarmClock size={18} className="automation-task-icon" aria-hidden="true" />}
            <span className="automation-task-copy">
              <span className="automation-task-name">{task.name}</span>
              <span className="automation-task-meta"><span>{formatSchedule(task.schedule, locale, t)}</span><span aria-hidden="true">·</span>
            <span>{!task.enabled ? t('已暂停') : next ? t('{date} {time}运行', { date: formatDate(beijingDate(next), locale), time: beijingTime(next) }) : t('有效期内无计划')}</span>
              </span>
            </span>
          </span>
        </Button>
        <div className="automation-task-actions">
          <Button type="button" variant="ghost" size="sm" leadingIcon={<Play size={14} />} aria-label={t('执行 {name}', { name: task.name })} disabled={busy.has(task.id)} onClick={() => onExecute(task.id)}>{t('执行')}</Button>
          <Button type="button" variant="ghost" className="automation-task-switch" role="switch" aria-checked={task.enabled}
            aria-label={t(task.enabled ? '暂停 {name}' : '启用 {name}', { name: task.name })} disabled={busy.has(task.id)} onClick={() => onToggle(task.id)}><span aria-hidden="true" className="automation-switch-track" /></Button>
        </div>
      </article>
    }) : <div className="automation-empty"><h3>{t(query || status !== 'all' ? '没有找到任务' : '还没有自动化')}</h3>
      <p>{t(query || status !== 'all' ? '换个关键词试试' : '创建一个任务，让重复工作按时完成')}</p>
      {(query || status !== 'all') && <Button type="button" variant="secondary" onClick={onClearFilter}>{t('清除筛选')}</Button>}
    </div>}
  </>
}

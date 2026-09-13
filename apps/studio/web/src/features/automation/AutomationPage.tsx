import { Plus, Search } from 'lucide-react'
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'

import { Button, Dialog, IconButton, SearchField, ViewTabs } from '../../components/ui'
import type { ToastKind } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import { WorkspaceHeader } from '../workspace/components/WorkspaceHeader'
import { AutomationEditor } from './AutomationEditor'
import { AutomationHistory } from './AutomationHistory'
import { AutomationStatusFilter, type AutomationFilter } from './AutomationStatusFilter'
import { AutomationRunDialog } from './AutomationRunDialog'
import { AutomationTaskList } from './AutomationTaskList'
import { batchTasks, commandTask } from './api'
import { runStatusLabels, todayInBeijing, weekDates, type AutomationRun, type AutomationTask } from './model'
import { useAutomation } from './useAutomation'
import './automation.css'

type ActiveDialog = { kind: 'editor'; task?: AutomationTask; trigger: HTMLElement }
  | { kind: 'result'; run: AutomationRun; trigger: HTMLElement }
  | { kind: 'delete'; tasks: AutomationTask[]; trigger: HTMLElement; resolve: (failed: ReadonlySet<string>) => void }
  | null
const filterLabels = { all: '全部状态', enabled: '已启用', paused: '已暂停', ...runStatusLabels } as const

/** 使用服务端任务与执行事实，所有命令先确认结果再更新界面 */
export function AutomationPage({ navigationTriggerRef, onOpenNavigation, onModalChange, onToast, defaultModelId, renderModelChoice }: {
  navigationTriggerRef: RefObject<HTMLButtonElement | null>
  onOpenNavigation: () => void
  onModalChange: (open: boolean) => void
  onToast: (kind: ToastKind, message: string) => void
  defaultModelId: string
  renderModelChoice: (value: string, onChange: (value: string) => void) => ReactNode
}) {
  const { t } = useI18n()
  const [page, setPage] = useState<'tasks' | 'history'>('history')
  const [view, setView] = useState<'week' | 'list'>('week')
  const [query, setQuery] = useState('')
  const [weekOffset, setWeekOffset] = useState(0)
  const [searchOpen, setSearchOpen] = useState(false)
  const [filter, setFilter] = useState<AutomationFilter>('all')
  const [dialog, setDialog] = useState<ActiveDialog>(null)
  const [busy, setBusy] = useState(new Set<string>())
  const busyRef = useRef(new Set<string>())
  const owned = useRef(new Set<AbortController>())
  const commandIds = useRef(new Map<string, string>())
  const mounted = useRef(true)
  const dialogRef = useRef(dialog)
  dialogRef.current = dialog
  const searchRef = useRef<HTMLInputElement>(null)
  const searchTriggerRef = useRef<HTMLButtonElement>(null)
  const wasSearchOpenRef = useRef(false)
  const today = todayInBeijing()
  const dates = useMemo(() => weekDates(today, weekOffset), [today, weekOffset])
  const data = useAutomation({ page, query, status: filter, dates, view })

  useEffect(() => {
    mounted.current = true
    const requests = owned.current
    return () => {
      mounted.current = false
      for (const request of requests) request.abort()
      const current = dialogRef.current
      if (current?.kind === 'delete') current.resolve(new Set(current.tasks.map(task => task.id)))
    }
  }, [])
  useLayoutEffect(() => {
    onModalChange(dialog !== null)
    return () => onModalChange(false)
  }, [dialog, onModalChange])
  useLayoutEffect(() => {
    if (searchOpen) searchRef.current?.focus()
    else if (wasSearchOpenRef.current) searchTriggerRef.current?.focus()
    wasSearchOpenRef.current = searchOpen
  }, [searchOpen])

  const clearFilters = () => { setQuery(''); setFilter('all') }
  const closeSearch = () => { setSearchOpen(false); clearFilters() }
  const requestId = (key: string) => {
    if (!commandIds.current.has(key)) commandIds.current.set(key, crypto.randomUUID())
    return commandIds.current.get(key)!
  }
  const begin = (ids: string[]) => {
    if (ids.some(id => busyRef.current.has(id))) return null
    ids.forEach(id => busyRef.current.add(id)); setBusy(new Set(busyRef.current))
    const controller = new AbortController(); owned.current.add(controller)
    return controller
  }
  const finish = (ids: string[], controller: AbortController) => {
    owned.current.delete(controller)
    ids.forEach(id => busyRef.current.delete(id))
    if (mounted.current) setBusy(new Set(busyRef.current))
  }
  const execute = async (id: string, operation: 'pause' | 'enable' | 'run') => {
    const task = data.tasks.find(item => item.id === id)
    if (!task) return
    const controller = begin([id]); if (!controller) return
    const key = `${id}:${task.revision}:${operation}`
    try {
      await commandTask(task, operation, requestId(key), controller.signal)
      if (controller.signal.aborted || !mounted.current) return
      commandIds.current.delete(key)
      onToast('info', t(operation === 'run' ? '已加入运行队列' : operation === 'pause' ? '任务已暂停' : '任务已启用'))
      data.reload()
    } catch (error) {
      if (!controller.signal.aborted && mounted.current) onToast('error', error instanceof Error ? error.message : t('操作失败，请重试'))
    } finally { finish([id], controller) }
  }
  const batch = async (tasks: AutomationTask[], operation: 'pause' | 'delete'): Promise<ReadonlySet<string>> => {
    const ids = tasks.map(task => task.id)
    if (!ids.length) return new Set()
    const controller = begin(ids); if (!controller) return new Set(ids)
    const keys = Object.fromEntries(tasks.map(task => [task.id, `${task.id}:${task.revision}:${operation}`]))
    try {
      const result = await batchTasks(tasks, operation, Object.fromEntries(ids.map(id => [id, requestId(keys[id])])), controller.signal)
      if (controller.signal.aborted || !mounted.current) return new Set(ids)
      result.filter(item => item.succeeded).forEach(item => commandIds.current.delete(keys[item.taskId]))
      const failed = result.filter(item => !item.succeeded)
      onToast(failed.length ? 'error' : 'info', failed.length ? t('有 {count} 个任务操作失败，请重试', { count: failed.length }) : t(operation === 'delete' ? '所选任务已删除，运行历史已保留' : '已暂停所选任务'))
      data.reload()
      return new Set(failed.map(item => item.taskId))
    } catch (error) {
      if (!controller.signal.aborted && mounted.current) onToast('error', error instanceof Error ? error.message : t('操作失败，请重试'))
      return new Set(ids)
    } finally { finish(ids, controller) }
  }
  const requestDelete = (ids: ReadonlySet<string>, trigger: HTMLElement) => new Promise<ReadonlySet<string>>(resolve => {
    setDialog({ kind: 'delete', tasks: data.tasks.filter(task => ids.has(task.id)), trigger, resolve })
  })
  const cancelDelete = () => {
    if (dialog?.kind === 'delete') dialog.resolve(new Set(dialog.tasks.map(task => task.id)))
    setDialog(null)
  }
  const filters: AutomationFilter[] = page === 'history' ? ['all', ...Object.keys(runStatusLabels) as (keyof typeof runStatusLabels)[]] : ['all', 'enabled', 'paused']
  const total = Object.values(data.counts).reduce((sum, value) => sum + value, 0)

  return <>
    <WorkspaceHeader conversationTitle={t('自动化')} overlayTriggerRef={navigationTriggerRef} onOpenOverlay={onOpenNavigation}
      navigation={<ViewTabs value={page} label={t('自动化视图')} options={[
        { value: 'tasks', label: t('任务'), controls: 'automation-panel' },
        { value: 'history', label: t('历史'), controls: 'automation-panel' },
      ]} onChange={value => { setPage(value); clearFilters() }} className="workspace-view-tabs" />}
      actions={<div className={`automation-header-actions${searchOpen ? ' is-search-open' : ''}`}>
        {searchOpen && <AutomationStatusFilter value={filter} options={filters.map(value => ({ value, label: t(filterLabels[value]), count: value === 'all' ? total : data.counts[value] ?? 0 }))} onChange={setFilter} />}
        <div className={`automation-search-control${searchOpen ? ' is-open' : ''}`}>
          <IconButton ref={searchTriggerRef} size="sm" label={t('搜索任务或运行历史')} icon={<Search size={18} />}
            aria-hidden={searchOpen || undefined} tabIndex={searchOpen ? -1 : undefined}
            aria-expanded={searchOpen} aria-controls="automation-search" onClick={() => setSearchOpen(true)} />
          {searchOpen && <SearchField className="automation-search-field" id="automation-search" ref={searchRef} appearance="plain" label={t('搜索任务或运行历史')} value={query} onChange={setQuery} onClose={closeSearch} closeLabel={t('关闭搜索')} placeholder={t('搜索任务或运行历史')} />}
        </div>
        <Button type="button" size="sm" variant="primary" leadingIcon={<Plus size={16} />} aria-label={t('新建自动化')}
          onClick={event => setDialog({ kind: 'editor', trigger: event.currentTarget })}><span className="automation-create-label">{t('新建')}</span></Button>
      </div>} />
    <div className="automation-scroll ui-scrollbar">
      <section className="automation-content">
        {data.error && <div role="alert">{t('自动化数据加载失败')}<Button variant="text" onClick={data.reload}>{t('重试')}</Button></div>}
        <div id="automation-panel" role="tabpanel" aria-label={t(page === 'history' ? '历史' : '任务')} aria-busy={data.loading}>
          {data.loading && !data.tasks.length && !data.runs.length ? <p role="status">{t('正在加载自动化')}</p> : page === 'history'
            ? <AutomationHistory offset={weekOffset} onOffsetChange={setWeekOffset} runs={data.runs} query={query} status={filter === 'enabled' || filter === 'paused' ? 'all' : filter} onOpenRun={(run, trigger) => setDialog({ kind: 'result', run, trigger })} view={view} onViewChange={setView} cursors={data.cursors} onLoadMore={data.loadMore} loading={data.loading} />
            : <><AutomationTaskList tasks={data.tasks} query={query} status={filter === 'enabled' || filter === 'paused' ? filter : 'all'} busy={busy} total={filter === 'all' ? total : data.counts[filter] ?? 0}
              onEdit={(task, trigger) => setDialog({ kind: 'editor', task, trigger })} onExecute={id => void execute(id, 'run')}
              onToggle={id => void execute(id, data.tasks.find(task => task.id === id)?.enabled ? 'pause' : 'enable')}
              onPause={ids => batch(data.tasks.filter(task => ids.has(task.id)), 'pause')} onDelete={requestDelete} onClearFilter={clearFilters} />
              {data.cursors.tasks && <Button variant="text" loading={data.loading} onClick={() => data.loadMore('tasks')}>{t('加载更多')}</Button>}</>}
        </div>
      </section>
    </div>
    {dialog?.kind === 'editor' && <AutomationEditor task={dialog.task} trigger={dialog.trigger} defaultModelId={defaultModelId} renderModelChoice={renderModelChoice} onClose={() => setDialog(null)} onSave={() => { setDialog(null); data.reload(); onToast('info', t('任务已保存')) }} />}
    {dialog?.kind === 'result' && <AutomationRunDialog run={dialog.run} trigger={dialog.trigger} onClose={() => setDialog(null)} />}
    {dialog?.kind === 'delete' && <Dialog open title={t('删除所选任务')} restoreFocusTo={dialog.trigger} closeDisabled={busy.size > 0} onClose={cancelDelete}>
      <p>{t('将删除 {count} 个任务并取消排队执行，运行历史会保留，此操作不可撤销', { count: dialog.tasks.length })}</p>
      <div className="automation-form-footer"><Button variant="ghost" disabled={busy.size > 0} onClick={cancelDelete}>{t('取消')}</Button>
        <Button variant="danger" loading={busy.size > 0} onClick={() => { const current = dialog; void batch(current.tasks, 'delete').then(failed => { current.resolve(failed); if (mounted.current) setDialog(null) }) }}>{t('删除任务')}</Button></div>
    </Dialog>}
  </>
}

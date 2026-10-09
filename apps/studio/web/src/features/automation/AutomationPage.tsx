import { Plus } from 'lucide-react'
import { useLayoutEffect, useMemo, useState, type ReactNode, type RefObject } from 'react'

import { Button, Dialog, ExpandableSearch, FeedbackState, ViewTabs } from '../../components/ui'
import type { ToastKind } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import { WorkspaceHeader } from '../workspace/components/WorkspaceHeader'
import { AutomationEditor } from './AutomationEditor'
import { AutomationHistory } from './AutomationHistory'
import { AutomationStatusFilter, type AutomationFilter } from './AutomationStatusFilter'
import { AutomationRunDialog } from './AutomationRunDialog'
import { AutomationTaskList } from './AutomationTaskList'
import { useAutomationTaskActions } from './useAutomationTaskActions'
import { runStatusLabels, todayInBeijing, weekDates, type AutomationRun, type AutomationTask } from './model'
import { useAutomation } from './useAutomation'
import './automation.css'

type ActiveDialog = { kind: 'editor'; task?: AutomationTask; trigger: HTMLElement }
  | { kind: 'result'; run: AutomationRun; trigger: HTMLElement }
  | null
const filterLabels = { all: '全部状态', enabled: '已启用', paused: '已暂停', ...runStatusLabels } as const

/** 使用服务端任务与执行事实，所有命令先确认结果再更新界面 */
export function AutomationPage({ projectId, navigationTriggerRef, onOpenNavigation, onModalChange, onToast, defaultModelId, renderModelChoice }: {
  projectId: string
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
  const today = todayInBeijing()
  const dates = useMemo(() => weekDates(today, weekOffset), [today, weekOffset])
  const data = useAutomation({ projectId, page, query, status: filter, dates, view,
    onLoadError: () => onToast('error', t('自动化数据加载失败')),
  })

  const actions = useAutomationTaskActions(projectId, data.tasks, data.reload, onToast)
  const { busy, deletion } = actions
  const modalOpen = dialog !== null || deletion !== null
  useLayoutEffect(() => {
    onModalChange(modalOpen)
    return () => onModalChange(false)
  }, [modalOpen, onModalChange])

  const clearFilters = () => { setQuery(''); setFilter('all') }
  const filters: AutomationFilter[] = page === 'history' ? ['all', ...Object.keys(runStatusLabels) as (keyof typeof runStatusLabels)[]] : ['all', 'enabled', 'paused']

  return <>
    <WorkspaceHeader conversationTitle={t('自动化')} overlayTriggerRef={navigationTriggerRef} onOpenOverlay={onOpenNavigation}
      navigation={<ViewTabs value={page} label={t('自动化视图')} options={[
        { value: 'tasks', label: t('任务'), controls: 'automation-panel' },
        { value: 'history', label: t('历史'), controls: 'automation-panel' },
      ]} onChange={value => { setPage(value); clearFilters() }} className="workspace-view-tabs" />}
      actions={<div className="workspace-search-actions automation-header-actions">
        <ExpandableSearch open={searchOpen} onOpenChange={setSearchOpen} label={t('搜索任务或运行历史')}
          placeholder={t('搜索任务或运行历史')} closeLabel={t('关闭搜索')} value={query} onChange={setQuery} />
        <AutomationStatusFilter value={filter} options={filters.map(value => ({ value, label: t(filterLabels[value]) }))} onChange={setFilter} />
        <Button type="button" size="sm" variant="primary" className="workspace-header-action" leadingIcon={<Plus size={16} />} aria-label={t('新建自动化')}
          onClick={event => setDialog({ kind: 'editor', trigger: event.currentTarget })}><span className="workspace-header-action-label">{t('新建')}</span></Button>
      </div>} />
    <div className="automation-scroll ui-scrollbar">
      <section className="automation-content">
        <div id="automation-panel" role="tabpanel" aria-label={t(page === 'history' ? '历史' : '任务')} aria-busy={data.loading}>
          {data.error && <div className={`automation-load-failure is-${page}`}><FeedbackState kind="error" appearance="retry" title={t('自动化数据加载失败')} retryLabel={t('重新加载')} onRetry={data.reload} /></div>}
          {data.loading && !data.tasks.length && !data.runs.length ? <p role="status">{t('正在加载自动化')}</p> : page === 'history'
            ? <AutomationHistory offset={weekOffset} onOffsetChange={setWeekOffset} runs={data.runs} query={query} status={filter === 'enabled' || filter === 'paused' ? 'all' : filter} onOpenRun={(run, trigger) => setDialog({ kind: 'result', run, trigger })} view={view} onViewChange={setView} cursors={data.cursors} onLoadMore={data.loadMore} loading={data.loading} loadFailed={data.error && !data.runs.length} />
            : data.error && !data.tasks.length ? null : <><AutomationTaskList tasks={data.tasks} query={query} status={filter === 'enabled' || filter === 'paused' ? filter : 'all'} busy={busy}
              onEdit={(task, trigger) => setDialog({ kind: 'editor', task, trigger })} onExecute={id => void actions.execute(id, 'run')}
              onToggle={id => void actions.execute(id, data.tasks.find(task => task.id === id)?.enabled ? 'pause' : 'enable')}
              onPause={actions.pause} onDelete={actions.requestDelete} onClearFilter={clearFilters} />
              {data.cursors.tasks && <Button variant="text" loading={data.loading} onClick={() => data.loadMore('tasks')}>{t('加载更多')}</Button>}</>}
        </div>
      </section>
    </div>
    {dialog?.kind === 'editor' && <AutomationEditor projectId={projectId} task={dialog.task} trigger={dialog.trigger} defaultModelId={defaultModelId} renderModelChoice={renderModelChoice} onClose={() => setDialog(null)} onSave={() => { setDialog(null); data.reload(); onToast('info', t('任务已保存')) }} />}
    {dialog?.kind === 'result' && <AutomationRunDialog projectId={projectId} run={dialog.run} trigger={dialog.trigger} onToast={onToast} onClose={() => setDialog(null)} />}
    {deletion && <Dialog open title={t('删除所选任务')} restoreFocusTo={deletion.trigger} closeDisabled={busy.size > 0} onClose={actions.cancelDelete}>
      <p>{t('将删除 {count} 个任务并取消排队执行，运行历史会保留，此操作不可撤销', { count: deletion.tasks.length })}</p>
      <div className="automation-form-footer"><Button variant="ghost" disabled={busy.size > 0} onClick={actions.cancelDelete}>{t('取消')}</Button>
        <Button variant="danger" loading={busy.size > 0} onClick={() => { void actions.confirmDelete() }}>{t('删除任务')}</Button></div>
    </Dialog>}
  </>
}

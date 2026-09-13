import { ChevronLeft, ChevronRight, CircleAlert } from 'lucide-react'
import { useLayoutEffect, useRef } from 'react'

import { Button, IconButton, OverlayScrollbar, ViewTabs } from '../../components/ui'
import { useI18n } from '../../i18n'
import { todayInBeijing, runStatusLabels, weekDates, type AutomationRun } from './model'
import { formatDate, formatDateRange } from './presentation'

export function RunStatus({ run }: { run: AutomationRun }) {
  const { t } = useI18n()
  return <span className={`automation-run-status is-${run.status}`}>
    {run.status === 'failed' && <CircleAlert size={14} aria-hidden="true" />}
    <span>{t(runStatusLabels[run.status])}</span>
  </span>
}

function HistoryDay({ date, runs, onOpenRun, hasMore, onLoadMore, loading }: {
  date: string
  hasMore: boolean
  onLoadMore: () => void
  loading: boolean
  runs: AutomationRun[]
  onOpenRun: (run: AutomationRun, trigger: HTMLElement) => void
}) {
  const { locale, t } = useI18n()
  const viewportRef = useRef<HTMLDivElement>(null)
  return <section className={`automation-history-day${date === todayInBeijing() ? ' is-today' : ''}`}>
    <h3 className="automation-day-heading"><span>{formatDate(date, locale, true)}</span><b>{Number(date.slice(8))}</b></h3>
    <div className="automation-day-body">
      <div ref={viewportRef} className="automation-day-scroll ui-scrollbar" role="region" tabIndex={0}
        aria-label={t('{date} 的运行记录，可上下滚动', { date })}>
        {runs.length ? runs.map((run) => <Button key={run.id} type="button" variant="ghost"
          className={`automation-run-chip is-${run.status}`}
          aria-label={t('查看运行：{name}，{date} {time}，{status}', {
            name: run.name, date: run.date, time: run.time,
            status: t(runStatusLabels[run.status]),
          })} onClick={(event) => onOpenRun(run, event.currentTarget)}>
          <span className="automation-event-content">
            <span className="automation-event-time"><time>{run.time}</time>
              {run.status === 'failed' && <span className="automation-event-failure" aria-hidden="true"><CircleAlert size={14} />{t('失败')}</span>}
            </span>
            <span className="automation-event-name">{run.name}</span>
          </span>
        </Button>) : <p className="automation-day-empty">{t('暂无记录')}</p>}
        {hasMore && <Button variant="text" loading={loading} onClick={onLoadMore}>{t('加载更多')}</Button>}
      </div>
      <OverlayScrollbar viewportRef={viewportRef} size="compact" visibility="persistent" />
    </div>
  </section>
}

/** 周历和列表只组织已经发生的运行，不提供调度或再次执行入口 */
export function AutomationHistory({ runs, query, status, onOpenRun, offset, onOffsetChange, view, onViewChange, cursors, onLoadMore, loading }: {
  runs: AutomationRun[]
  query: string
  status: 'all' | AutomationRun['status']
  offset: number
  view: 'week' | 'list'
  onViewChange: (view: 'week' | 'list') => void
  cursors: Record<string, string | null>
  onLoadMore: (day: string) => void
  loading: boolean
  onOffsetChange: (offset: number) => void
  onOpenRun: (run: AutomationRun, trigger: HTMLElement) => void
}) {
  const { locale, t } = useI18n()
  const weekRef = useRef<HTMLDivElement>(null)
  const previousWeekRef = useRef<HTMLButtonElement>(null)
  const dates = weekDates(todayInBeijing(), offset)
  const matching = [...runs].sort((a, b) => b.queuedAt.localeCompare(a.queuedAt))
  const filtered = Boolean(query || status !== 'all')

  useLayoutEffect(() => {
    const viewport = weekRef.current
    if (!viewport || view !== 'week') return
    const today = viewport.querySelector<HTMLElement>('.is-today')
    let wasScrollable = false
    const alignInitialDay = () => {
      const isScrollable = viewport.scrollWidth > viewport.clientWidth
      // 初次出现横向滚动时对齐今天，同为窄屏的尺寸变化保留用户阅读位置
      if (isScrollable && !wasScrollable) viewport.scrollLeft = today?.offsetLeft ?? 0
      wasScrollable = isScrollable
    }
    alignInitialDay()
    const observer = new ResizeObserver(alignInitialDay)
    observer.observe(viewport)
    return () => observer.disconnect()
  }, [offset, view])

  return <>
    <div className="automation-toolbar automation-history-toolbar">
      <div className="automation-date-nav">
        <div className="automation-date-switcher">
          <IconButton ref={previousWeekRef} type="button" size="sm" label={t('上一周')} icon={<ChevronLeft size={16} />} onClick={() => onOffsetChange(offset - 1)} />
          <h2 aria-live="polite">{formatDateRange(dates[0], dates[6], locale)}</h2>
          <IconButton type="button" size="sm" label={t('下一周')} icon={<ChevronRight size={16} />} disabled={offset === 0} onClick={() => onOffsetChange(Math.min(0, offset + 1))} />
        </div>
        {offset !== 0 && <Button type="button" variant="text" size="sm" onClick={() => {
          onOffsetChange(0)
          previousWeekRef.current?.focus()
        }}>{t('回到本周')}</Button>}
      </div>
      <ViewTabs value={view} label={t('运行历史显示方式')} density="compact" options={[
        { value: 'week', label: t('周历'), controls: 'automation-history-view' },
        { value: 'list', label: t('列表'), controls: 'automation-history-view' },
      ]} onChange={onViewChange} />
    </div>
    <div id="automation-history-view" role="tabpanel" aria-label={t(view === 'week' ? '周历' : '列表')}>
      {view === 'week' ? <>
        <div className="automation-calendar">
          <div ref={weekRef} className="automation-week ui-scrollbar" role="region" tabIndex={0} aria-label={t('本周运行历史，可横向滚动')}>
            {dates.map((date) => <HistoryDay key={date} date={date} hasMore={Boolean(cursors[date])} onLoadMore={() => onLoadMore(date)} loading={loading}
              runs={matching.filter((run) => run.date === date).reverse()} onOpenRun={onOpenRun} />)}
          </div>
        </div>
        <div className="automation-history-hint">
          <small><span className="automation-horizontal-hint">{t('左右查看其他日期')} · </span>{t('每日记录可上下滚动')}</small>
        </div>
      </> : matching.length ? [...dates].reverse().filter((date) => matching.some((run) => run.date === date)).map((date) => (
        <section key={date} className="automation-history-group">
          <h3>{formatDate(date, locale)}</h3>
          {matching.filter((run) => run.date === date).map((run) => <Button type="button" variant="ghost" key={run.id}
            className="automation-history-row" onClick={(event) => onOpenRun(run, event.currentTarget)}>
            <span className="automation-history-row-content">
              <time>{run.time}</time><span className="automation-run-identity"><strong>{run.name}</strong>
                <small>{t(run.trigger === 'manual' ? '手动运行' : '定时运行')} · {run.durationSeconds ? t('{seconds} 秒', { seconds: run.durationSeconds }) : t(runStatusLabels[run.status])}</small></span>
              <RunStatus run={run} /><ChevronRight size={16} aria-hidden="true" />
            </span>
          </Button>)}
        </section>
      )) : <div className="automation-empty"><h3>{t(filtered ? '没有找到运行记录' : '还没有运行记录')}</h3><p>{t(filtered ? '换个关键词试试' : '本周的运行结果会显示在这里')}</p></div>}
      {view === 'list' && cursors[dates[0]] && <Button variant="text" loading={loading} onClick={() => onLoadMore(dates[0])}>{t('加载更多')}</Button>}
    </div>
  </>
}

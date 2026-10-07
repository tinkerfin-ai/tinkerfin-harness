import type { Project } from '../projects/api'
import { ChevronDown, FolderOpen, Layers2, Plus } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { Button, Dialog, FeedbackState, SearchField, ViewTabs } from '../../components/ui'
import type { ToastKind } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import { WorkspaceHeader } from '../workspace/components/WorkspaceHeader'
import { SkillCard } from './SkillCard'
import { SkillDetailDialog } from './SkillDetailDialog'
import { SkillImportDialog } from './SkillImportDialog'
import { SkillPicker } from './SkillPicker'
import { SkillSourceIcon } from './SkillSourceIcon'
import { skillError, useRemoteSkills, useSkillLibrary } from './useSkillData'
import { useSkillsNavigation } from './useSkillsNavigation'
import { useSkillPagination } from './useSkillPagination'
import type { InstalledSkill, SkillSelection } from './model'
import './skills.css'

type ActiveDialog = { kind: 'detail'; selection: SkillSelection; trigger: HTMLElement }
  | { kind: 'import'; trigger: HTMLElement } | { kind: 'update' | 'uninstall'; skill: InstalledSkill; trigger: HTMLElement } | null

interface SkillsPageProps {
  project: Project
  navigationTriggerRef: RefObject<HTMLButtonElement | null>; onOpenNavigation: () => void
  onModalChange: (open: boolean) => void; onToast: (kind: ToastKind, message: string) => void
}

export function SkillsPage({ project, navigationTriggerRef, onOpenNavigation, onModalChange, onToast }: SkillsPageProps) {
  const { t } = useI18n()
  const [personal, setPersonal] = useState(false)
  const navigation = useSkillsNavigation()
  const catalog = useRemoteSkills(navigation.source, navigation.filters.query, navigation.view === 'discover')
  const projectId = navigation.view === 'mine' && personal ? null : project.id
  const [overlays, setOverlays] = useState<{ projectId: string | null; dialog: ActiveDialog; sourceDrawer: boolean }>({ projectId, dialog: null, sourceDrawer: false })
  // 浏览器历史也可切换管理范围，新范围不得继续显示原范围的导入或管理操作
  if (overlays.projectId !== projectId) setOverlays({ projectId, dialog: null, sourceDrawer: false })
  const { dialog, sourceDrawer } = overlays
  const setDialog = (dialog: ActiveDialog) => setOverlays(current => ({ ...current, dialog }))
  const setSourceDrawer = (sourceDrawer: boolean) => setOverlays(current => ({ ...current, sourceDrawer }))
  const modal = dialog !== null || sourceDrawer
  useLayoutEffect(() => { onModalChange(modal); return () => onModalChange(false) }, [modal, onModalChange])
  return <section className="skills-page" aria-label={t('技能库')}>
    <div className="skills-page-content" inert={modal || undefined} aria-hidden={modal || undefined}>
      <WorkspaceHeader conversationTitle={t('技能库')} overlayTriggerRef={navigationTriggerRef} onOpenOverlay={onOpenNavigation}
        navigation={<ViewTabs value={navigation.view} label={t('技能库视图')} options={[{ value: 'discover', label: t('发现') }, { value: 'mine', label: t('我的') }]}
          className="workspace-view-tabs" onChange={navigation.selectView} />}
        actions={<Button type="button" variant="primary" size="sm" className="workspace-header-action" leadingIcon={<Plus size={16} />} aria-label={t('导入技能')} onClick={event => setDialog({ kind: 'import', trigger: event.currentTarget })}><span className="workspace-header-action-label">{t('导入')}</span></Button>} />
      {navigation.view === 'mine' && <div className="skills-project-scope"><ViewTabs value={personal ? 'personal' : 'project'} label={t('技能范围')} options={[{ value: 'project', label: t('当前项目') }, { value: 'personal', label: t('个人共用') }]} onChange={value => setPersonal(value === 'personal')} /></div>}
      <ScopedSkillsPage key={projectId ?? 'personal'} project={project} navigation={navigation} catalog={catalog} projectId={projectId}
        dialog={dialog} setDialog={setDialog} sourceDrawer={sourceDrawer} setSourceDrawer={setSourceDrawer} onToast={onToast} />
    </div>
  </section>
}

function ScopedSkillsPage({ project, navigation, catalog, projectId, dialog, setDialog, sourceDrawer, setSourceDrawer, onToast }: {
  project: Project; navigation: ReturnType<typeof useSkillsNavigation>; catalog: ReturnType<typeof useRemoteSkills>
  projectId: string | null; dialog: ActiveDialog; setDialog: (dialog: ActiveDialog) => void
  sourceDrawer: boolean; setSourceDrawer: (open: boolean) => void; onToast: (kind: ToastKind, message: string) => void
}) {
  const { t } = useI18n()
  const library = useSkillLibrary(projectId)
  const sourceTrigger = useRef<HTMLButtonElement>(null)
  const scroll = useRef<HTMLDivElement>(null)
  const pagination = useRef<HTMLDivElement>(null)
  const search = useRef<HTMLInputElement>(null)
  const modal = dialog !== null || sourceDrawer
  useSkillPagination(scroll, pagination, navigation.view === 'discover' && catalog.status === 'ready' && Boolean(catalog.cursor) && !catalog.loadingMore && !catalog.error && !modal, catalog.more)
  useEffect(() => {
    if (navigation.view === 'discover' && !navigation.source && library.sources[0]) navigation.selectSource(library.sources[0].id, true)
  }, [navigation, library.sources])
  useLayoutEffect(() => {
    if (scroll.current) scroll.current.scrollTop = navigation.scroll.current.get(navigation.key) ?? 0
  }, [navigation.key, navigation.filters, navigation.scroll, catalog.status])
  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (event.key === '/' && !modal && !target?.closest('input,textarea,[contenteditable="true"]')) { event.preventDefault(); search.current?.focus() }
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [modal])
  const rememberScroll = () => { if (scroll.current) navigation.scroll.current.set(navigation.key, scroll.current.scrollTop) }
  const chooseSource = (id: string) => { rememberScroll(); navigation.selectSource(id); setSourceDrawer(false) }
  const sourceOptions = new Map(library.sources.map(source => [source.id, source.name]))
  if (navigation.view === 'mine') library.installed.forEach(skill => { if (skill.source_id) sourceOptions.set(skill.source_id, skill.source_name) })
  const rows = [...(navigation.view === 'mine' ? [{ id: 'all', name: t('全部来源') }, { id: 'imports', name: t('个人导入') }] : []), ...Array.from(sourceOptions, ([id, name]) => ({ id, name }))]
  const sourceLabel = rows.find(row => row.id === navigation.source)?.name ?? t('选择来源')
  const sourceContent = <>
    <div className="skills-source-heading"><span>{t('来源')}</span><span>{sourceOptions.size}</span></div>
    <nav className="skills-source-list ui-scrollbar" aria-label={t('技能来源')}>
      {rows.map(row => <button type="button" key={row.id} aria-pressed={navigation.source === row.id} onClick={() => chooseSource(row.id)}>
        {row.id === 'all' ? <Layers2 size={17} aria-hidden="true" /> : row.id === 'imports' ? <FolderOpen size={17} aria-hidden="true" /> : <SkillSourceIcon id={row.id} />}
        <span>{row.name}</span>
      </button>)}
    </nav>
    {library.sourceStatus === 'error' && <FeedbackState compact kind="error" title={t('来源加载失败')} onRetry={library.refresh} />}
  </>
  const localSource = library.installed.filter(skill => navigation.source === 'all' || (navigation.source === 'imports' ? skill.source_id === null : skill.source_id === navigation.source))
  const categories = [...new Set(localSource.flatMap(skill => skill.topics))].sort()
  const filtered = localSource.filter(skill => {
    const query = navigation.filters.query.toLocaleLowerCase().trim()
    return (!query || `${skill.name} ${skill.description} ${skill.author ?? ''}`.toLocaleLowerCase().includes(query))
      && (!navigation.filters.category || skill.topics.includes(navigation.filters.category))
      && (navigation.filters.status === 'all' || skill.enabled === (navigation.filters.status === 'enabled'))
  }).sort((left, right) => navigation.filters.sort === 'name' ? left.name.localeCompare(right.name) : right.updated_at.localeCompare(left.updated_at))
  const selections: SkillSelection[] = navigation.view === 'mine' ? filtered.map(skill => ({ kind: 'installed', skill })) : catalog.items.map(skill => ({ kind: 'remote', skill }))
  const status = navigation.view === 'mine' ? library.installedStatus : library.sourceStatus !== 'ready' ? library.sourceStatus : !navigation.source ? 'ready' : catalog.status
  const failure = navigation.view === 'mine' ? library.installedError : library.sourceStatus === 'error' ? library.sourceError : catalog.error
  const hasFilters = Boolean(navigation.filters.query || navigation.filters.category || navigation.filters.status !== 'all')
  const retry = navigation.view === 'mine' || library.sourceStatus === 'error' ? library.refresh : catalog.retry
  const close = () => setDialog(null)
  const update = (skill: InstalledSkill, trigger: HTMLElement) => {
    if (skill.source_kind === 'zip') { setDialog({ kind: 'update', skill, trigger }); return }
    void library.update(skill).then(result => { if (result) onToast('success', result.changed ? t('技能已更新，下次新运行生效') : t('已是最新内容')) })
  }
  return <>
      <div className="skills-layout"><aside className="skills-sources">{sourceContent}</aside>
        <div className="skills-results">
          <div className="skills-filters">
            <button ref={sourceTrigger} type="button" className="skills-mobile-source" onClick={() => setSourceDrawer(true)} aria-haspopup="dialog"><Layers2 size={16} aria-hidden="true" /><span>{sourceLabel}</span><ChevronDown size={14} aria-hidden="true" /></button>
            <div className="skills-search-row"><SearchField ref={search} appearance="soft" className={`skills-search${navigation.filters.query ? ' is-filled' : ''}`} label={t('搜索技能')} closeLabel={t('清除搜索')} placeholder={navigation.view === 'mine' ? t('搜索我的技能…') : t('搜索技能…')}
              value={navigation.filters.query} onChange={query => navigation.changeFilters({ query })} onClose={() => { navigation.changeFilters({ query: '' }); search.current?.focus() }} />
              {navigation.view === 'mine' && <SkillPicker value={navigation.filters.status} label={t('技能状态')} onChange={status => navigation.changeFilters({ status })} options={[{ value: 'all', label: t('全部状态') }, { value: 'enabled', label: t('已启用') }, { value: 'disabled', label: t('已停用') }]} />}
            </div>
            {navigation.view === 'mine' && categories.length > 0 && <div className="skills-categories" role="group" aria-label={t('技能分类')}>
              <button type="button" aria-pressed={!navigation.filters.category} onClick={() => navigation.changeFilters({ category: '' })}>{t('全部')}</button>
              {categories.map(category => <button type="button" key={category} aria-pressed={navigation.filters.category === category} onClick={() => navigation.changeFilters({ category })}>{category}</button>)}
            </div>}
            {navigation.view === 'mine' && <div className="skills-toolbar"><div><h2>{t('已安装技能')}</h2>{status === 'ready' && <span aria-live="polite">{t('{count}项', { count: selections.length })}</span>}</div>
              <SkillPicker value={navigation.filters.sort} label={t('技能排序')} onChange={sort => navigation.changeFilters({ sort })} options={[{ value: 'updated', label: t('最近更新') }, { value: 'name', label: t('按名称') }]} />
            </div>}
          </div>
          <div ref={scroll} className="skills-scroll ui-scrollbar" onScroll={rememberScroll}>
            {status === 'loading' ? <div className="skills-grid" role="status" aria-label={t('正在加载技能')}>{Array.from({ length: 6 }, (_, index) => <div className="skills-skeleton" key={index} aria-hidden="true"><span /><b /><i /><i /></div>)}</div>
              : status === 'error' && !(navigation.view === 'mine' && library.installed.length) ? <div className="skills-empty"><FeedbackState kind="error" title={skillError(failure, t('技能加载失败'))} onRetry={retry} /></div>
              : selections.length === 0 ? <div className="skills-empty"><h2>{navigation.view === 'mine' && library.installed.length === 0 ? t('把常用技能放在这里') : t('没有找到技能')}</h2><p>{navigation.view === 'mine' && library.installed.length === 0 ? t('从发现中安装，或导入自己的技能') : t('尝试其他关键词或来源')}</p>
                {hasFilters ? <Button type="button" size="sm" onClick={() => navigation.changeFilters({ query: '', category: '', status: 'all' })}>{t('清除筛选')}</Button>
                  : navigation.view === 'mine' && <Button type="button" size="sm" onClick={() => navigation.selectView('discover')}>{t('发现技能')}</Button>}
              </div> : <div className="skills-grid">{selections.map(selection => {
                const skill = selection.skill
                const key = selection.kind === 'installed' ? skill.id : `${selection.skill.source_id}/${skill.id}`
                return <SkillCard project={project} scopeId={projectId} key={key} selection={selection} sourceName={selection.kind === 'installed' ? selection.skill.source_name : sourceOptions.get(selection.skill.source_id) ?? ''}
                  installedIn={library.installed.filter(item => item.source_id === selection.skill.source_id && item.external_id === skill.id).map(item => item.project_id)} busy={library.busy.has(key)}
                  error={library.errors[key] ? skillError(library.errors[key], t('操作失败，请重试')) : undefined}
                  onOpen={(selection, trigger) => setDialog({ kind: 'detail', selection, trigger })} onInstall={async (skill, destination) => { const done = await library.install(skill, destination); if (done) onToast('success', t('技能已安装')); return done }}
                  onToggle={skill => { void library.toggle(skill) }} onUpdate={update} onUninstall={(skill, trigger) => setDialog({ kind: 'uninstall', skill, trigger })} />
              })}</div>}
            {status === 'error' && navigation.view === 'mine' && library.installed.length > 0 && <FeedbackState compact kind="error" title={skillError(failure, t('技能加载失败'))} onRetry={retry} />}
            {navigation.view === 'discover' && catalog.cursor && status === 'ready' && <div ref={pagination} className="skills-pagination">
              {catalog.loadingMore ? <p role="status">{t('正在加载技能')}</p> : Boolean(catalog.error) && <FeedbackState compact kind="error" title={skillError(catalog.error, t('加载更多失败'))} onRetry={() => void catalog.more()} />}
            </div>}
          </div>
        </div>
      </div>
    {sourceDrawer && <Dialog open title={t('技能来源')} className="skills-source-dialog" restoreFocusTo={sourceTrigger.current} onClose={() => setSourceDrawer(false)}>{sourceContent}</Dialog>}
    {dialog?.kind === 'detail' && <SkillDetailDialog projectId={projectId} selection={dialog.selection} trigger={dialog.trigger} onClose={close} />}
    {dialog?.kind === 'import' && <SkillImportDialog project={project} initialProjectId={projectId} trigger={dialog.trigger} onClose={close} onCompleted={() => { library.refresh(); onToast('success', t('技能已安装')) }} />}
    {dialog?.kind === 'update' && <SkillImportDialog project={project} initialProjectId={projectId} target={dialog.skill} trigger={dialog.trigger} onClose={close} onCompleted={changed => { library.refresh(); onToast('success', changed ? t('技能已更新，下次新运行生效') : t('已是最新内容')) }} />}
    {dialog?.kind === 'uninstall' && <Dialog open title={t('卸载技能')} className="skills-delete-dialog" restoreFocusTo={dialog.trigger} onClose={close} closeDisabled={library.busy.has(dialog.skill.id)}>
      <div className="skills-delete-content"><p>{t('卸载「{name}」？', { name: dialog.skill.name })}</p><p>{t('已开始的运行继续使用原技能内容，你可以随时重新安装')}</p>{Boolean(library.errors[dialog.skill.id]) && <p className="skills-field-error" role="alert">{skillError(library.errors[dialog.skill.id], t('卸载失败，请重试'))}</p>}</div>
      <footer className="skills-dialog-footer"><Button type="button" size="sm" variant="ghost" disabled={library.busy.has(dialog.skill.id)} onClick={close}>{t('取消')}</Button><Button type="button" size="sm" variant="danger" loading={library.busy.has(dialog.skill.id)} onClick={() => { void library.uninstall(dialog.skill).then(done => { if (done) { close(); onToast('success', t('技能已卸载')) } }) }}>{t('卸载')}</Button></footer>
    </Dialog>}
  </>
}

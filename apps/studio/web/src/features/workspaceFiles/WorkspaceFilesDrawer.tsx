import { ArrowLeft, FolderOpen, LayoutGrid, List, RefreshCw } from 'lucide-react'
import type { ReactNode } from 'react'
import { Breadcrumbs, Drawer, ErrorBoundary, FeedbackState, IconButton, OverlayScrollbar, type BreadcrumbItem } from '../../components/ui'
import { WorkspaceFilePreview } from './WorkspaceFilePreview'
import { useI18n } from '../../i18n'
import type { WorkspaceFilesState } from './useWorkspaceFiles'
import { WorkspaceFileBrowser } from './WorkspaceFileBrowser'
import { WorkspaceFileInformation } from './WorkspaceFileInformation'
import { useWorkspaceFileNavigation } from './useWorkspaceFileNavigation'
import './workspaceFiles.css'

export function WorkspaceFilesDrawer({ state, open, fullPage, resizeHandle, onClose, onToast }: {
  state: WorkspaceFilesState
  open: boolean
  fullPage: boolean
  resizeHandle?: ReactNode
  onClose: () => void
  onToast: (kind: 'success' | 'error', message: string) => void
}) {
  const { t } = useI18n()
  const navigation = useWorkspaceFileNavigation(state, open)
  const selected = state.selected
  const parts = state.directoryPath.split('/').filter(Boolean)
  const breadcrumbs: BreadcrumbItem[] = [{ label: t('工作区'), onNavigate: () => navigation.openDirectory('/') }, ...parts.map((label, index) => ({ label, onNavigate: () => navigation.openDirectory('/' + parts.slice(0, index + 1).join('/')) }))]
  if (selected) breadcrumbs.push({ label: selected.name })
  const unavailable = state.availability === 'paused' ? t('工作区已暂停') : t('工作区暂不可用，请稍后重试')
  return <Drawer id="workspace-files-drawer" title={t('工作区')}
    open={open} fullPage={fullPage} className="workspace-files-drawer" closeLabel={t('关闭工作区')}
    onClose={onClose} resizeHandle={resizeHandle} data-workspace-layout-target="workspace-files-drawer"
    actions={<IconButton type="button" variant="ghost" size="sm" label={t('刷新文件')} icon={<RefreshCw size={18} />} onClick={state.refresh} />}>
    <div ref={navigation.content} className="workspace-files-content">
      <div className="workspace-files-toolbar">
        {selected && <IconButton ref={navigation.backButton} type="button" variant="ghost" size="sm" label={t('返回文件目录')} icon={<ArrowLeft size={18} />} onClick={navigation.closeFile} />}
        {(state.directoryPath !== '/' || selected) && <Breadcrumbs label={t('文件路径')} current={selected ? 'page' : 'location'} items={breadcrumbs} />}
        {selected ? open && <WorkspaceFileInformation key={selected.path} file={selected} />
          : <div className="workspace-files-view-switch" role="group" aria-label={t('文件视图')}>
            <IconButton type="button" variant="ghost" size="sm" label={t('列表视图')} aria-pressed={navigation.view === 'list'} icon={<List size={16} />} onClick={() => navigation.changeView('list')} />
            <IconButton type="button" variant="ghost" size="sm" label={t('图标视图')} aria-pressed={navigation.view === 'grid'} icon={<LayoutGrid size={16} />} onClick={() => navigation.changeView('grid')} />
          </div>}
      </div>
      <div className="workspace-files-body-region">
        <div ref={navigation.viewport} className="workspace-files-body ui-scrollbar" role="region" aria-label={selected ? t('文件内容') : t('文件目录')} tabIndex={0} onScroll={navigation.onScroll}>
          <ErrorBoundary resetKey={`${state.directoryPath}:${selected?.path ?? ''}`} fallback={({ reset }) => <FeedbackState kind="error" title={t('工作区显示失败')} appearance="retry" onRetry={reset} compact />}>
            {state.availability === 'loading' ? <div className="workspace-files-feedback"><FeedbackState kind="loading" title={t('正在加载工作区')} compact /></div>
              : state.availability === 'paused' || state.availability === 'unavailable' ? <div className="workspace-files-feedback"><FeedbackState kind="error" title={unavailable} appearance="retry" onRetry={state.refresh} compact /></div>
                : state.availability === 'uninitialized' ? <div className="workspace-files-empty"><FolderOpen size={32} aria-hidden="true" /><h3>{t('工作区尚无文件')}</h3><p>{t('会话中创建的文件会显示在这里')}</p></div>
                  : !selected ? <WorkspaceFileBrowser state={state} view={navigation.view} onDirectory={navigation.openDirectory} onFile={navigation.openFile} />
                    : <WorkspaceFilePreview key={`${selected.path}:${open}:${state.preview?.file.etag ?? ''}`} state={state} open={open} onToast={onToast} />}
          </ErrorBoundary>
        </div>
        <OverlayScrollbar viewportRef={navigation.viewport} />
        <OverlayScrollbar viewportRef={navigation.viewport} axis="horizontal" />
      </div>
    </div>
  </Drawer>
}

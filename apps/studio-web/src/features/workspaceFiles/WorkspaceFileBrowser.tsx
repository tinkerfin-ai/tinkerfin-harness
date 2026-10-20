import { File, Folder, FolderOpen } from 'lucide-react'
import { useRef, type KeyboardEvent } from 'react'
import { Button, FeedbackState, Tooltip } from '../../components/ui'
import { restoreFocus } from '../../components/ui/focus'
import { useI18n } from '../../i18n'
import type { WorkspaceFile } from './api'
import type { WorkspaceFilesState } from './useWorkspaceFiles'
import { formatWorkspaceFileSize, workspaceFileFormat } from './workspaceFilePresentation'

export type WorkspaceFileView = 'list' | 'grid'

function WorkspaceEntryIcon({ file }: { file: WorkspaceFile }) {
  if (file.kind === 'directory') {
    const color = file.name === 'skills' || file.path.startsWith('/skills/') ? 'amber' : file.name === 'archive' ? 'green' : 'blue'
    return <Folder className="workspace-file-folder" data-color={color} aria-hidden="true" />
  }
  const format = workspaceFileFormat(file.name)
  const color = ['MD', 'TXT'].includes(format) ? 'document' : ['HTML', 'HTM', 'XML', 'SVG'].includes(format) ? 'markup'
    : ['CSV', 'XLS', 'XLSX'].includes(format) ? 'sheet' : 'source'
  return <span className="workspace-file-icon" data-color={color} aria-hidden="true"><File /><span>{format}</span></span>
}

function WorkspaceFileName({ file, view }: { file: WorkspaceFile; view: WorkspaceFileView }) {
  const name = useRef<HTMLSpanElement>(null)
  const stem = useRef<HTMLSpanElement>(null)
  const dot = file.kind === 'directory' ? -1 : file.name.lastIndexOf('.')
  return <Tooltip content={file.name} overflowOnly overflowRef={view === 'list' ? stem : name} className="workspace-files-tooltip">
    <span ref={name} className="workspace-file-name"><span ref={stem} className="workspace-file-stem">{dot > 0 ? file.name.slice(0, dot) : file.name}</span>
      {dot > 0 && <span className="workspace-file-extension">{file.name.slice(dot)}</span>}
    </span>
  </Tooltip>
}

export function WorkspaceFileBrowser({ state, view, onDirectory, onFile }: {
  state: WorkspaceFilesState
  view: WorkspaceFileView
  onDirectory: (path: string) => void
  onFile: (file: WorkspaceFile) => void
}) {
  const { t, locale } = useI18n()
  const root = useRef<HTMLDivElement>(null)
  const directory = state.directories[state.directoryPath]
  const entries = directory?.entries ?? []
  const loading = !directory || directory.phase === 'loading'
  const navigate = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    const buttons = Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[data-file-path]') ?? [])
    const index = buttons.indexOf(event.currentTarget)
    const list = root.current?.querySelector<HTMLElement>('[role="list"]')
    const item = buttons[0]?.getBoundingClientRect()
    const columns = view === 'grid' && list && item?.width ? Math.max(1, Math.round(list.clientWidth / item.width)) : 1
    const step = event.key === 'ArrowDown' ? columns : event.key === 'ArrowUp' ? -columns : event.key === 'ArrowLeft' ? -1 : 1
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : Math.max(0, Math.min(buttons.length - 1, index + step))
    event.preventDefault()
    restoreFocus(buttons[next], { preventScroll: true })
    buttons[next]?.scrollIntoView({ block: 'nearest' })
  }
  if (loading && !entries.length) return <div className="workspace-files-feedback"><FeedbackState kind="loading" title={t('正在加载文件')} compact /></div>
  if (directory?.phase === 'error' && !entries.length) return <div className="workspace-files-feedback"><FeedbackState kind="error" title={t('文件加载失败')} appearance="retry" onRetry={() => state.retryDirectory(state.directoryPath)} compact /></div>
  if (!entries.length) return <div className="workspace-files-empty"><FolderOpen size={32} aria-hidden="true" /><h3>{t('此文件夹为空')}</h3></div>
  return <div ref={root} className="workspace-files-browser" data-view={view} aria-busy={loading || undefined}>
    <div className="workspace-files-list-head" aria-hidden="true"><span>{t('名称')}</span><span>{t('大小')}</span><span>{t('修改时间')}</span></div>
    <div className="workspace-files-entries" role="list" aria-label={t('当前目录文件')}>
      {entries.map(file => <div key={file.path} role="listitem">
          <Button type="button" variant="ghost" size="sm" className="workspace-file-entry" data-file-path={file.path} data-kind={file.kind} aria-label={file.name}
            onKeyDown={navigate} onClick={() => file.kind === 'directory' ? onDirectory(file.path) : onFile(file)}>
            <span className="workspace-file-name-cell"><WorkspaceEntryIcon file={file} /><WorkspaceFileName file={file} view={view} /></span>
            <span className="workspace-file-size">{file.kind === 'directory' ? '' : formatWorkspaceFileSize(file.sizeBytes, locale)}</span>
            <span className="workspace-file-modified">{new Date(file.modifiedAt).toLocaleString(locale, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })}</span>
          </Button>
        </div>)}
    </div>
    {directory?.phase === 'error' && <Button type="button" variant="text" onClick={() => state.retryDirectory(state.directoryPath)}>{t('加载失败，重试')}</Button>}
    {directory?.nextCursor && <Button type="button" variant="text" loading={loading} onClick={() => state.loadMore(state.directoryPath)}>{t('加载更多文件')}</Button>}
  </div>
}

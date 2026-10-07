import { ChevronDown, ChevronRight, FileCode2, Folder, FolderOpen, LoaderCircle } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react'
import { Button, Tooltip } from '../../components/ui'
import { restoreFocus } from '../../components/ui/focus'
import { useI18n } from '../../i18n'
import type { WorkspaceFile } from './api'
import type { WorkspaceFilesState } from './useWorkspaceFiles'

type TreeRow = { key: string; parent: string; level: number } & (
  { kind: 'entry'; file: WorkspaceFile } | { kind: 'loading' | 'empty' | 'error' | 'more' }
)

export function WorkspaceFileTree({ state, onSelect }: { state: WorkspaceFilesState; onSelect: (file: WorkspaceFile) => void }) {
  const { t } = useI18n()
  const root = useRef<HTMLDivElement>(null)
  const [focused, setFocused] = useState<string | null>(null)
  const ownedFocus = useRef(false)
  const rows: TreeRow[] = []
  const collect = (path: string, level: number) => {
    const directory = state.directories[path]
    for (const file of directory?.entries ?? []) {
      rows.push({ key: file.path, parent: path, level, kind: 'entry', file })
      if (file.kind === 'directory' && state.expanded.has(file.path)) collect(file.path, level + 1)
    }
    const kind = !directory || (directory.phase === 'loading' && directory.entries.length === 0) ? 'loading'
      : directory.phase === 'error' ? 'error' : directory.nextCursor ? 'more' : directory.entries.length === 0 ? 'empty' : null
    if (kind) rows.push({ key: `${path}:${kind}`, parent: path, level, kind })
  }
  collect('/', 1)
  const interactive = rows.filter(row => row.kind === 'entry'
    || ((row.kind === 'error' || row.kind === 'more') && state.directories[row.parent]?.phase !== 'loading'))
  const activeKey = interactive.some(row => row.key === focused) ? focused : interactive[0]?.key
  const focusRow = (key: string | undefined) => {
    if (!key) return
    setFocused(key)
    const target = Array.from(root.current?.querySelectorAll<HTMLElement>('[role="treeitem"]') ?? []).find(node => node.dataset.treeKey === key)
    restoreFocus(target ?? null, { preventScroll: true })
    target?.scrollIntoView({ block: 'nearest' })
  }
  useLayoutEffect(() => {
    if (ownedFocus.current && document.activeElement === document.body && activeKey) focusRow(activeKey)
  })

  const navigate = (event: KeyboardEvent, row: TreeRow) => {
    const index = interactive.findIndex(item => item.key === row.key)
    if (['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      focusRow(interactive[event.key === 'Home' ? 0 : event.key === 'End' ? interactive.length - 1
        : Math.max(0, Math.min(interactive.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))]?.key)
    } else if (event.key === 'ArrowRight' && row.kind === 'entry' && row.file.kind === 'directory') {
      event.preventDefault()
      if (!state.expanded.has(row.file.path)) state.toggleDirectory(row.file.path)
      else focusRow(interactive[index + 1]?.parent === row.file.path ? interactive[index + 1].key : undefined)
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault()
      if (row.kind === 'entry' && row.file.kind === 'directory' && state.expanded.has(row.file.path)) state.toggleDirectory(row.file.path)
      else focusRow(row.parent)
    }
  }
  return <div ref={root} role="tree" aria-label={t('工作区文件')} className="workspace-file-tree"
    onFocusCapture={() => { ownedFocus.current = true }} onBlurCapture={event => {
      if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) ownedFocus.current = false
    }}>
    {rows.map(row => {
      const style = { '--workspace-file-level': row.level - 1 } as CSSProperties
      if (row.kind === 'loading' || row.kind === 'empty') return <div key={row.key} role="treeitem" aria-level={row.level} aria-selected={false} aria-disabled="true" className="workspace-file-tree-note" style={style}>
        {row.kind === 'loading' ? <><LoaderCircle size={14} aria-hidden="true" />{t('正在加载')}</> : t('空目录')}
      </div>
      if (row.kind === 'error' || row.kind === 'more') return <Button key={row.key} variant="text" size="sm" role="treeitem" aria-level={row.level} aria-selected={false}
        loading={state.directories[row.parent]?.phase === 'loading'}
        data-tree-key={row.key} tabIndex={activeKey === row.key ? 0 : -1} style={style} className="workspace-file-tree-action"
        onFocus={() => setFocused(row.key)} onKeyDown={event => navigate(event, row)}
        onClick={() => row.kind === 'error' ? state.retryDirectory(row.parent) : state.loadMore(row.parent)}>
        {row.kind === 'error' ? t('加载失败，重试') : t('加载更多文件')}
      </Button>
      if (row.kind !== 'entry') return null
      const file = row.file
      const directory = file.kind === 'directory'
      const expanded = directory && state.expanded.has(file.path)
      const Icon = directory ? expanded ? FolderOpen : Folder : FileCode2
      return <Button key={row.key} variant="ghost" size="sm" role="treeitem" data-tree-key={row.key}
        aria-label={file.name} aria-level={row.level} aria-selected={state.selected?.path === file.path}
        aria-expanded={directory ? expanded : undefined} tabIndex={activeKey === row.key ? 0 : -1}
        className={`workspace-file-row${state.selected?.path === file.path ? ' is-selected' : ''}`} style={style}
        onFocus={() => setFocused(row.key)} onKeyDown={event => navigate(event, row)}
        onClick={() => directory ? state.toggleDirectory(file.path) : onSelect(file)}>
        <span className="workspace-file-chevron">{directory && (expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />)}</span>
        <Icon size={16} aria-hidden="true" />
        <Tooltip content={file.name} overflowOnly><span className="workspace-file-name">{file.name}</span></Tooltip>
      </Button>
    })}
  </div>
}

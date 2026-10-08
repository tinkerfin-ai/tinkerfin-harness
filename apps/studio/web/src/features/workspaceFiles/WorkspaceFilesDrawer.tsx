import { ArrowLeft, Check, Copy, File, FolderOpen, RefreshCw } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { Button, Drawer, FeedbackState, IconButton, OverlayScrollbar, Tooltip } from '../../components/ui'
import { CodeText } from '../../components/ui/CodeText'
import { restoreFocus } from '../../components/ui/focus'
import { useI18n } from '../../i18n'
import type { WorkspaceFilesState } from './useWorkspaceFiles'
import { WorkspaceFileTree } from './WorkspaceFileTree'
import './workspaceFiles.css'

export function WorkspaceFilesDrawer({ projectName, state, open, fullPage, resizeHandle, onClose, onToast }: {
  projectName: string
  state: WorkspaceFilesState
  open: boolean
  fullPage: boolean
  resizeHandle?: ReactNode
  onClose: () => void
  onToast: (kind: 'success' | 'error', message: string) => void
}) {
  const { t, locale } = useI18n()
  const treeViewport = useRef<HTMLDivElement>(null)
  const previewViewport = useRef<HTMLDivElement>(null)
  const backButton = useRef<HTMLButtonElement>(null)
  const [detailVisible, setDetailVisible] = useState(false)
  const returnToTree = useRef(false)
  const selectedPath = state.selected?.path
  const copyAttempt = useRef(0)
  useLayoutEffect(() => {
    copyAttempt.current += 1
    return () => { copyAttempt.current += 1 }
  }, [open, selectedPath])
  const previousPath = useRef(selectedPath)
  const readerPosition = useRef({ top: 0, left: 0 })
  useLayoutEffect(() => {
    if (open && detailVisible && backButton.current?.offsetParent) restoreFocus(backButton.current, { preventScroll: true })
    if (open && !detailVisible && returnToTree.current) {
      returnToTree.current = false
      const row = Array.from(treeViewport.current?.querySelectorAll<HTMLElement>('[role="treeitem"]') ?? []).find(item => item.dataset.treeKey === selectedPath)
      restoreFocus(row ?? treeViewport.current, { preventScroll: true })
    }
  }, [open, detailVisible, selectedPath])
  useLayoutEffect(() => {
    const viewport = previewViewport.current
    if (previousPath.current !== selectedPath) readerPosition.current = { top: 0, left: 0 }
    previousPath.current = selectedPath
    if (viewport) { viewport.scrollTop = readerPosition.current.top; viewport.scrollLeft = readerPosition.current.left }
  }, [selectedPath, state.preview])
  const copyPath = async () => {
    if (!open || !selectedPath) return
    const attempt = ++copyAttempt.current
    try {
      await navigator.clipboard.writeText(selectedPath)
      if (attempt === copyAttempt.current) onToast('success', t('已复制路径'))
    } catch {
      if (attempt === copyAttempt.current) onToast('error', t('复制失败'))
    }
  }
  const backToTree = () => {
    returnToTree.current = true
    setDetailVisible(false)
  }
  const selected = state.selected
  const fileLabel = selected?.name.split('.').at(-1)?.toUpperCase() || t('文件')
  const status = state.availability === 'uninitialized' ? t('等待文件') : state.connection === 'ready' ? t('自动更新') : state.connection === 'connecting' ? t('正在连接') : t('自动更新暂不可用')
  const unavailable = state.availability === 'paused' ? t('工作区已暂停') : t('工作区暂不可用，请稍后重试')
  const empty = state.availability === 'uninitialized' || (!selected && state.availability === 'ready' && state.directories['/']?.phase === 'ready' && state.directories['/'].entries.length === 0)

  return <Drawer id="workspace-files-drawer" title={t('工作区')} description={projectName}
    open={open} fullPage={fullPage} className="workspace-files-drawer" closeLabel={t('关闭工作区')}
    backLabel={t('返回对话')} onClose={onClose} resizeHandle={resizeHandle} data-workspace-layout-target="workspace-files-drawer"
    actions={<IconButton size="xs" label={t('刷新文件')} tooltip={t('刷新文件')} icon={<RefreshCw size={18} />} onClick={state.refresh} />}>
    <div className="workspace-files-content">
      {state.availability === 'loading' ? <div className="workspace-files-feedback"><FeedbackState kind="loading" title={t('正在加载工作区')} compact /></div>
        : state.availability === 'paused' || state.availability === 'unavailable' ? <div className="workspace-files-feedback"><FeedbackState kind="error" title={unavailable} appearance="retry" onRetry={state.refresh} compact /></div>
          : empty ? <div className="workspace-files-empty"><FolderOpen size={28} aria-hidden="true" /><h3>{t('工作区尚无文件')}</h3><p>{t('会话中创建的文件会显示在这里')}</p></div>
            : <div className="workspace-files-layout" data-detail-visible={Boolean(detailVisible && selected)}>
              <div className="workspace-files-tree-region">
                <div ref={treeViewport} className="workspace-files-tree-scroll ui-scrollbar" role="region" aria-label={t('文件目录')} tabIndex={0}>
                  <WorkspaceFileTree state={state} onSelect={file => { state.selectFile(file); setDetailVisible(true) }} />
                </div>
                <OverlayScrollbar viewportRef={treeViewport} />
              </div>
              <section className="workspace-file-preview" aria-label={t('文件预览')}>
                {selected ? <>
                  <div className="workspace-file-preview-head">
                    <IconButton ref={backButton} className="workspace-files-back" variant="ghost" size="sm" label={t('返回文件目录')} icon={<ArrowLeft size={16} />} onClick={backToTree} />
                    <Tooltip content={selected.path} overflowOnly><span className="workspace-file-preview-name">{selected.name}</span></Tooltip>
                    <IconButton variant="ghost" size="sm" label={t('复制路径')} tooltip={t('复制路径')} icon={<Copy size={16} />} onClick={() => void copyPath()} />
                  </div>
                  {state.updated && <div className="workspace-file-updated" role="status"><span>{t('文件已变化')}</span><Button variant="text" size="xs" onClick={state.refreshPreview}>{t('刷新预览')}</Button></div>}
                  {state.previewPhase === 'loading' && !state.preview && <FeedbackState kind="loading" title={t('正在读取文件')} compact />}
                  {state.previewPhase === 'missing' && <div className="workspace-files-empty"><File size={28} aria-hidden="true" /><p>{t('文件已被移除')}</p><Button variant="text" onClick={state.refresh}>{t('刷新文件')}</Button></div>}
                  {state.previewPhase === 'error' && <FeedbackState kind="error" title={t('文件预览失败')} appearance="retry" onRetry={state.refreshPreview} compact />}
                  <div className="workspace-file-preview-region">
                    <div ref={previewViewport} className="workspace-file-preview-scroll ui-scrollbar" role="region" aria-label={t('文件内容')} tabIndex={0}
                      onScroll={event => { readerPosition.current = { top: event.currentTarget.scrollTop, left: event.currentTarget.scrollLeft } }}>
                      {state.preview?.kind === 'text' && (state.preview.text ? <pre className="workspace-file-source"><CodeText language={selected.name.split('.').at(-1)}>{state.preview.text}</CodeText></pre> : <p className="workspace-file-note">{t('此文件为空')}</p>)}
                      {state.preview?.kind === 'unsupported' && <div className="workspace-file-info"><File size={28} aria-hidden="true" /><p>{t('此格式仅显示文件信息')}</p><dl><dt>{t('文件类型')}</dt><dd>{fileLabel}</dd><dt>{t('文件大小')}</dt><dd>{selected.sizeBytes === null ? '—' : t('{count} 字节', { count: selected.sizeBytes.toLocaleString(locale) })}</dd><dt>{t('修改时间')}</dt><dd>{new Date(selected.modifiedAt).toLocaleString(locale)}</dd></dl></div>}
                    </div>
                    <OverlayScrollbar viewportRef={previewViewport} />
                    <OverlayScrollbar viewportRef={previewViewport} axis="horizontal" />
                  </div>
                  {state.preview?.kind === 'text' && state.preview.truncated && <p className="workspace-file-note" role="status">{t('仅预览前 200 行或 100 KiB')}</p>}
                </> : <div className="workspace-files-empty"><File size={28} aria-hidden="true" /><p>{t('选择文件查看源码')}</p></div>}
              </section>
            </div>}
      <div className="workspace-files-footer" role="status"><span>{state.connection === 'ready' && <Check size={13} aria-hidden="true" />}{status}</span><span>{t('只读')}</span></div>
    </div>
  </Drawer>
}

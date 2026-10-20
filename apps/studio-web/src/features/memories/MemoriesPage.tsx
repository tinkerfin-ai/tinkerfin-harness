import { FileText, Plus, Trash2 } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { Button, Dialog, ExpandableSearch, FeedbackState, IconButton, TextField } from '../../components/ui'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import type { Project } from '../projects/api'
import { WorkspaceHeader } from '../workspace/components/WorkspaceHeader'
import { useMemoryEditor } from './useMemoryEditor'
import { useMemories } from './useMemories'
import './memories.css'

export function MemoriesPage({ project, navigationTriggerRef, onOpenNavigation, onModalChange, onToast }: {
  project: Project; navigationTriggerRef: RefObject<HTMLButtonElement | null>; onOpenNavigation: () => void
  onModalChange: (open: boolean) => void; onToast: ToastHandler
}) {
  const { t, locale } = useI18n()
  const [query, setQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const library = useMemories(project.id, query)
  const { editor, removal, loading, saving, error, conflict, discarding, open, closeEditor, save, remove,
    setEditor, setRemoval, setDiscarding, create, requestRemoval, loadConflict } = useMemoryEditor(project.id, library.refresh, onToast)
  const input = useRef<HTMLInputElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const modal = editor !== null || removal !== null
  useLayoutEffect(() => { onModalChange(modal); return () => onModalChange(false) }, [modal, onModalChange])
  return <>
    <WorkspaceHeader conversationTitle={t('记忆管理')} overlayTriggerRef={navigationTriggerRef} onOpenOverlay={onOpenNavigation}
      actions={<div className="workspace-search-actions">
        <ExpandableSearch open={searchOpen} onOpenChange={setSearchOpen} label={t('搜索记忆')}
          placeholder={t('搜索名称或内容')} closeLabel={t('关闭搜索')} value={query} onChange={setQuery} />
        <Button type="button" size="sm" variant="primary" className="workspace-header-action" leadingIcon={<Plus size={17} />} aria-label={t('新增记忆')}
          onClick={event => create(event.currentTarget)}><span className="workspace-header-action-label">{t('新增')}</span></Button>
      </div>} />
    <section className="memories-page ui-scrollbar">
      {library.status === 'loading' ? <FeedbackState kind="loading" title={t('正在加载记忆')} /> : library.status === 'error' ? <FeedbackState kind="error" title={t('记忆加载失败')} onRetry={library.refresh} /> : <>
        {library.items.length === 0 && <p className="memories-empty" role="status">{query ? t('没有匹配的记忆') : t('还没有记忆')}</p>}
        <ul className="memories-list">{library.items.map(item => <li key={item.path}>
          <button type="button" className="memory-open" disabled={!item.editable || loading === item.path} onClick={event => void open(item, event.currentTarget)} aria-label={t('编辑记忆：{name}', { name: item.path.slice(1) })}>
            <FileText size={19} aria-hidden="true" /><span className="memory-summary"><strong>{item.path.slice(1)}</strong><span>{item.editable ? item.preview || t('空白记忆') : t('此文件不支持文本编辑')}</span><small>{new Date(item.updatedAt).toLocaleString(locale)}</small></span>
          </button>
          <IconButton label={t('删除记忆：{name}', { name: item.path.slice(1) })} icon={<Trash2 size={16} />} onClick={() => requestRemoval(item)} />
        </li>)}</ul>
        {library.nextOffset !== null && <Button type="button" variant="text" loading={library.loadingMore} onClick={() => void library.more()}>{t(library.moreError ? '加载失败，重试' : '加载更多')}</Button>}
      </>}
    </section>
    <Dialog open={editor !== null} title={t(discarding ? '放弃未保存的修改？' : editor?.original ? '编辑记忆' : '新增记忆')} className="memory-dialog" closeDisabled={saving} initialFocusRef={editor?.original ? textarea : input} restoreFocusTo={editor?.trigger} onClose={closeEditor}>
      {discarding ? <div className="memory-form"><p>{t('关闭后将丢失本次修改')}</p><div className="memory-actions"><Button type="button" onClick={() => setDiscarding(false)}>{t('继续编辑')}</Button><Button type="button" variant="danger" onClick={() => { setEditor(null); setDiscarding(false) }}>{t('放弃修改')}</Button></div></div> : editor && <form className="memory-form" onSubmit={event => { event.preventDefault(); void save() }}>
        <TextField ref={input} label={t('记忆名称')} value={editor.path} placeholder={t('例如：研究偏好.md')} maxLength={511} disabled={saving || editor.original !== null} onChange={event => setEditor({ ...editor, path: event.target.value })} />
        <label className="memory-content-label">{t('内容')}<textarea ref={textarea} value={editor.content} disabled={saving} onChange={event => setEditor({ ...editor, content: event.target.value })} maxLength={262144} spellCheck={false} /></label>
        {error && <p className="memory-error" role="alert">{error}</p>}
        {conflict && <div className="memory-conflict"><strong>{t('记忆已更新，请对照最新内容')}</strong><pre>{conflict.content}</pre><Button type="button" variant="text" disabled={saving} onClick={loadConflict}>{t('载入最新内容')}</Button></div>}
        <div className="memory-actions"><Button type="button" disabled={saving} onClick={closeEditor}>{t('取消')}</Button><Button type="submit" variant="primary" loading={saving}>{t(conflict ? '以当前内容保存' : '保存')}</Button></div>
      </form>}
    </Dialog>
    <Dialog open={removal !== null} title={t('删除记忆')} closeDisabled={saving} onClose={() => { setRemoval(null); library.refresh() }}><div className="memory-form"><p>{t('删除“{name}”后，后续会话将不再使用这条记忆', { name: removal?.path.slice(1) ?? '' })}</p>{error && <p className="memory-error" role="alert">{error}</p>}<div className="memory-actions"><Button type="button" disabled={saving} onClick={() => setRemoval(null)}>{t('取消')}</Button><Button type="button" variant="danger" loading={saving} onClick={() => void remove()}>{t('删除')}</Button></div></div></Dialog>
  </>
}

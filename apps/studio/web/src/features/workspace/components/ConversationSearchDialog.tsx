import { Check, ChevronDown, MessageSquare, Search } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

import { Button, Dialog, FeedbackState, ListboxPicker, OverlayScrollbar, TextField } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { Conversation } from '../../../types'
import './conversation-search.css'

const scopes = ['project', 'all'] as const
export interface ConversationSearchDialogProps {
  query: string
  scope: 'project' | 'all'
  results: Conversation[]
  projectNames: Record<string, string>
  loading: boolean
  loadingMore: boolean
  error: string | null
  hasMore: boolean
  restoreFocusTo: HTMLElement | null
  onQueryChange: (value: string) => void
  onScopeChange: (value: 'project' | 'all') => void
  onSelect: (conversation: Conversation) => void
  onLoadMore: () => void
  onRetry: () => void
  onClose: () => void
}

/** 搜索与近期结果使用同一弹框，范围选择与对话框复用全局键盘、焦点和关闭行为 */
export function ConversationSearchDialog(props: ConversationSearchDialogProps) {
  const { t, locale } = useI18n()
  const inputRef = useRef<HTMLInputElement>(null)
  const viewportRef = useRef<HTMLDivElement>(null)
  const listId = useId()
  const [scopeOpen, setScopeOpen] = useState(false)
  const [selection, setSelection] = useState<{ query: string; scope: string; id: string } | null>(null)
  const activeIndex = selection?.query === props.query && selection.scope === props.scope
    ? Math.max(0, props.results.findIndex(item => item.threadId === selection.id))
    : 0
  const active = !props.loading ? props.results[activeIndex] : undefined
  const optionId = (index: number) => `${listId}-${index}`
  const labels = { project: t('当前项目'), all: t('全部项目') }
  const handleKeys = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
    if (event.defaultPrevented || event.target !== inputRef.current || scopeOpen || props.loading) return
    if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && props.results.length) {
      event.preventDefault()
      const nextIndex = (activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + props.results.length) % props.results.length
      const item = props.results[nextIndex]
      setSelection({ query: props.query, scope: props.scope, id: item.threadId })
      document.getElementById(optionId(nextIndex))?.scrollIntoView({ block: 'nearest' })
    } else if (event.key === 'Enter' && active) {
      event.preventDefault()
      props.onSelect(active)
    }
  }

  return <Dialog open title={t('搜索会话')} className="modal-dialog--action conversation-search-dialog"
    initialFocusRef={inputRef} restoreFocusTo={props.restoreFocusTo} onClose={props.onClose} onKeyDown={handleKeys}>
    <div className="conversation-search-body">
      <TextField ref={inputRef} type="search" role="combobox" aria-expanded="true" aria-autocomplete="list"
        aria-controls={listId} aria-activedescendant={active ? optionId(activeIndex) : undefined}
        label={<span className="visually-hidden">{t('搜索会话')}</span>} placeholder={t('搜索对话')}
        rootClassName="conversation-search-field" shape="standard" value={props.query} onChange={event => props.onQueryChange(event.target.value)}
        leadingContent={<Search size={16} />} trailingContent={
          <ListboxPicker value={props.scope} options={scopes} open={scopeOpen} onOpenChange={setScopeOpen}
            onChange={props.onScopeChange}
            triggerLabel={t('选择搜索范围')} listboxLabel={t('搜索范围')}
            rootClassName="ui-compact-picker ui-compact-picker--flat" triggerClassName="ui-compact-picker-trigger" listboxClassName="ui-compact-picker-options ui-compact-picker-options--down ui-compact-picker-options--compact"
            renderTrigger={value => <><span className="conversation-search-scope-label">
              {scopes.map(scope => <span key={scope} aria-hidden={scope !== value}>{labels[scope]}</span>)}
            </span><ChevronDown size={16} /></>}
            renderOption={(value, selected) => <><span className="ui-compact-option-label">{labels[value]}</span><span className="ui-compact-option-check">{selected && <Check size={14} />}</span></>} />
        } />
      <p className="conversation-search-heading">{t(props.query.trim() ? '搜索结果' : '最近会话')}</p>
    </div>
    <div className="conversation-search-results-shell">
      <div ref={viewportRef} className="conversation-search-results ui-scrollbar">
        {props.loading && <FeedbackState compact kind="loading" title={t('正在搜索会话')} />}
        {!props.loading && !props.results.length && !props.error && <p className="conversation-search-empty" role="status">{t(props.query.trim() ? '没有匹配的对话' : '暂无最近对话')}</p>}
        <div id={listId} className="conversation-search-list" role="listbox" aria-label={t('会话搜索结果')}>
          {!props.loading && props.results.map((item, index) => <Button key={item.threadId} type="button" variant="ghost"
            className="conversation-search-result" role="option" id={optionId(index)} tabIndex={-1}
            aria-selected={active?.threadId === item.threadId} onClick={() => props.onSelect(item)}
            leadingIcon={<MessageSquare size={18} />}>
            <span className="conversation-search-result-copy"><span>{item.title}</span><small>
              {props.projectNames[item.projectId]} · <time dateTime={item.updatedAt}>{new Intl.DateTimeFormat(locale, { month: 'numeric', day: 'numeric' }).format(new Date(item.updatedAt))}</time>
            </small></span>
          </Button>)}
        </div>
        {!props.loading && props.error && <FeedbackState compact kind="error" appearance="retry" title={props.error} onRetry={props.onRetry} />}
        {!props.loading && props.hasMore && !props.error && <Button type="button" variant="text" className="conversation-search-more"
          loading={props.loadingMore} onClick={props.onLoadMore}>{t('加载更多')}</Button>}
      </div>
      <OverlayScrollbar viewportRef={viewportRef} />
    </div>
    <footer className="conversation-search-footer">{t('方向键选择，Enter 打开，Esc 关闭')}</footer>
  </Dialog>
}

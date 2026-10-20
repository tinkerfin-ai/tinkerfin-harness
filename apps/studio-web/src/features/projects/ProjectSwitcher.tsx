import { Check, ChevronsUpDown, FolderKanban, Pencil, Plus, Search } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Button, Dialog, IconButton, TextField } from '../../components/ui'
import { useI18n } from '../../i18n'
import { ProjectNameForm } from './ProjectNameForm'
import type { ProjectWorkspaceScope } from './ProjectsWorkspace'

/** 项目编辑与列表共用面板；关闭时保存重命名，失败则保留编辑目标和输入 */
export function ProjectSwitcher({ scope }: { scope: ProjectWorkspaceScope }) {
  const { t } = useI18n()
  const trigger = scope.pickerTriggerRef
  const search = useRef<HTMLInputElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const [query, setQuery] = useState('')
  const form = scope.editorForm
  const editing = form !== null
  const editingId = form?.projectId
  const saving = form?.saving ?? false
  const projects = scope.projects.filter(project => project.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))

  useLayoutEffect(() => {
    if (!scope.pickerOpen) return
    if (editing) {
      input.current?.focus({ preventScroll: true })
      const length = input.current?.value.length ?? 0
      input.current?.setSelectionRange(length, length)
    } else search.current?.focus({ preventScroll: true })
  }, [scope.pickerOpen, editing, editingId])

  const close = async () => {
    if (saving) return
    if (form?.projectId && !await form.submit()) return
    if (form && !form.projectId) form.cancel()
    setQuery('')
    scope.closePicker()
  }
  const cancel = () => { form?.cancel(); setQuery('') }
  const create = () => { scope.create(query.trim()) }
  const handleKeys = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return
    if (event.key === 'Escape' && form) {
      event.preventDefault()
      if (!saving) cancel()
    }
    if (form) return
    const options = Array.from(list.current?.querySelectorAll<HTMLButtonElement>('[data-project-choice]') ?? [])
    if (event.key === 'Enter' && event.target === search.current) {
      event.preventDefault()
      if (options.length) options[0].click()
      else create()
    }
    if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && options.length) {
      event.preventDefault()
      const index = options.indexOf(event.target as HTMLButtonElement)
      const next = index < 0 ? event.key === 'ArrowDown' ? 0 : options.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length
      options[next]?.focus()
    }
  }
  const title = t(form ? form.projectId ? '重命名项目' : '创建项目' : '项目管理')
  return <div className="project-switcher" onKeyDownCapture={event => {
    if (event.nativeEvent.isComposing) event.stopPropagation()
  }}>
    <button ref={trigger} type="button" className="project-switcher-trigger" aria-label={t('切换项目：{name}', { name: scope.project.name })}
      aria-haspopup="dialog" aria-expanded={scope.pickerOpen} onClick={scope.openPicker}
      onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); scope.openPicker() } }}>
      <FolderKanban className="project-switcher-icon" size={18} aria-hidden="true" /><span>{scope.project.name}</span><ChevronsUpDown size={14} aria-hidden="true" />
    </button>
    <Dialog open={scope.pickerOpen} title={title} className="modal-dialog--action modal-dialog--search project-picker" closeDisabled={saving} initialFocusRef={search} restoreFocusTo={trigger.current}
      onClose={() => { void close() }} onKeyDown={handleKeys}
      headerActions={!form && <Button type="button" variant="ghost" className="project-picker-create" leadingIcon={<Plus size={16} />} onClick={create}>{t('新建项目')}</Button>}>
      {form ? <ProjectNameForm form={form} inputRef={input} onCancel={cancel} onSubmit={() => { void form.submit().then(saved => { if (saved) { setQuery(''); if (!form.projectId) scope.closePicker() } }) }} /> : <>
        <div className="modal-dialog-search-body">
          <TextField ref={search} type="search" role="searchbox" label={<span className="visually-hidden">{t('搜索项目')}</span>} placeholder={t('搜索项目')}
            rootClassName="ui-dialog-search-field" shape="standard" value={query} onChange={event => setQuery(event.target.value)} leadingContent={<Search size={18} />} />
        </div>
        <div ref={list} className="project-picker-list ui-scrollbar" role="group" aria-label={t('项目')}>
          {projects.map(project => {
            const active = project.id === scope.project.id
            return <div key={project.id} className="project-picker-row">
              <Button type="button" variant="ghost" className="project-picker-choice" data-project-choice aria-current={active ? 'true' : undefined}
                onClick={() => { scope.closePicker(); setQuery(''); if (!active) scope.select(project.id) }}>
                <span className="project-picker-check" aria-hidden="true">{active && <Check size={14} />}</span><span className="project-picker-name">{project.name}</span>
              </Button>
              <IconButton type="button" variant="ghost" size="xs" className="project-picker-rename" label={t('重命名项目：{name}', { name: project.name })}
                icon={<Pencil size={14} />} onClick={() => scope.rename(project.id)} />
            </div>
          })}
          {!projects.length && <p className="project-picker-empty">{t('没有匹配的项目')}</p>}
        </div>
      </>}
    </Dialog>
  </div>
}

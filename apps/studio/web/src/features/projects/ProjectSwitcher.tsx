import { Check, ChevronDown, FolderClosed, Pencil, Plus, X } from 'lucide-react'
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Button, IconButton, TextField, ValidatedForm } from '../../components/ui'
import { restoreFocus } from '../../components/ui/focus'
import { useI18n } from '../../i18n'
import type { ProjectWorkspaceScope } from './ProjectsWorkspace'

/** 项目列表允许在选中行编辑名称，浏览器负责浮层叠放、外部点击与关闭 */
export function ProjectSwitcher({ scope }: { scope: ProjectWorkspaceScope }) {
  const { t } = useI18n()
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const selected = useRef<HTMLButtonElement>(null)
  const renameButton = useRef<HTMLButtonElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState(false)
  const renaming = scope.renameForm !== null
  const wasRenaming = useRef(false)
  const saving = scope.renameForm?.saving ?? false

  useLayoutEffect(() => {
    if (!open) return
    const fit = () => {
      const anchor = trigger.current?.getBoundingClientRect()
      const menu = panel.current
      if (!anchor || !menu) return
      const tokens = getComputedStyle(document.documentElement)
      const gap = parseFloat(tokens.getPropertyValue('--space-1')) || 0
      const inset = parseFloat(tokens.getPropertyValue('--space-3')) || 0
      const width = Math.min(anchor.width, window.innerWidth - inset * 2)
      menu.style.width = `${width}px`
      menu.style.left = `${Math.max(inset, Math.min(anchor.left, window.innerWidth - width - inset))}px`
      menu.style.top = `${anchor.bottom + gap}px`
      menu.style.maxHeight = `${Math.max(0, window.innerHeight - anchor.bottom - gap - inset)}px`
    }
    fit()
    if (!renaming) {
      selected.current?.focus({ preventScroll: true })
      selected.current?.scrollIntoView({ block: 'nearest' })
    }
    window.addEventListener('resize', fit)
    window.addEventListener('scroll', fit, true)
    return () => { window.removeEventListener('resize', fit); window.removeEventListener('scroll', fit, true) }
  }, [open, renaming])

  useLayoutEffect(() => {
    if (renaming && open) input.current?.focus()
    if (renaming && !wasRenaming.current) input.current?.select()
    if (!renaming && wasRenaming.current) {
      if (open) restoreFocus(renameButton.current, { preventScroll: true })
    }
    wasRenaming.current = renaming
  }, [open, renaming])

  useEffect(() => {
    // 保存失败即使发生在浮层关闭之后，也重新显示原输入和错误，供用户重试
    if (scope.renameForm?.error && !open) panel.current?.showPopover()
  }, [open, scope.renameForm?.error])

  const close = () => { if (!saving) panel.current?.hidePopover() }
  const handleKeys = (event: KeyboardEvent<HTMLElement>) => {
        if (event.key === 'Escape' && renaming) { event.preventDefault(); event.stopPropagation(); scope.renameForm?.cancel() }
        else if (event.key === 'Escape') { event.preventDefault(); close(); trigger.current?.focus({ preventScroll: true }) }
        if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && event.target instanceof HTMLButtonElement) {
          event.preventDefault()
          const buttons = Array.from(panel.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])
          const index = buttons.indexOf(event.target)
          const next = buttons[(index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]
          next?.focus({ preventScroll: true })
          next?.scrollIntoView({ block: 'nearest' })
        }

  }
  return <div className="project-switcher">
    <button ref={trigger} type="button" className="project-switcher-trigger" aria-label={t('切换项目：{name}', { name: scope.project.name })}
      aria-haspopup="dialog" aria-expanded={open} aria-controls={id} disabled={saving}
      onClick={() => { if (open) close(); else panel.current?.showPopover() }}
      onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); panel.current?.showPopover() } }}>
      <FolderClosed size={18} aria-hidden="true" /><span>{scope.project.name}</span><ChevronDown size={15} aria-hidden="true" />
    </button>
    <div ref={panel} id={id} popover="auto" role="dialog" aria-label={t('项目')} aria-hidden={!open} className="project-switcher-popover ui-scrollbar"
      onToggle={event => {
        const visible = event.currentTarget.matches(':popover-open')
        setOpen(visible)
        if (!visible && scope.renameForm && !saving) scope.renameForm.cancel()
      }}
      onBlur={event => { if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) close() }}
      >
      {scope.projects.map(project => {
        const active = project.id === scope.project.id
        return <div key={project.id} className={`project-switcher-row${active ? ` is-selected${renaming ? ' is-renaming' : ''}` : ''}`}>
          {active && scope.renameForm ? <ValidatedForm className="project-rename-form" aria-label={t('重命名项目')} errors={{ projectName: scope.renameForm.error }} validationAttempt={scope.renameForm.validationAttempt}
            onSubmit={event => { event.preventDefault(); void scope.renameForm?.submit() }}>
            <TextField ref={input} name="projectName" label={t('项目名称')} fieldSize="md" shape="standard" value={scope.renameForm.name} error={scope.renameForm.error} maxLength={64} autoComplete="off" onKeyDown={handleKeys} required disabled={saving} onChange={event => scope.renameForm?.change(event.target.value)} />
            <div className="project-rename-actions">
              <IconButton type="submit" size="md" variant="ghost" label={t('保存')} tooltip={t('保存')} icon={<Check size={16} />} loading={saving} onKeyDown={handleKeys} />
              <IconButton type="button" size="md" variant="ghost" label={t('取消')} tooltip={t('取消')} icon={<X size={16} />} disabled={saving} onKeyDown={handleKeys} onClick={() => scope.renameForm?.cancel()} />
            </div>
          </ValidatedForm> : <>
            <Button ref={active ? selected : undefined} type="button" variant="ghost" className="project-switcher-choice" aria-current={active ? 'true' : undefined} disabled={saving || renaming}
              leadingIcon={<FolderClosed size={16} />} onKeyDown={handleKeys} onClick={() => { close(); if (!active) scope.select(project.id) }}><span>{project.name}</span></Button>
            {active && <><Check className="project-selected-check" size={14} aria-hidden="true" /><IconButton ref={renameButton} type="button" variant="ghost" label={t('重命名项目')} tooltip={t('重命名项目')} icon={<Pencil size={16} />} disabled={saving} onKeyDown={handleKeys} onClick={scope.rename} /></>}
          </>}
        </div>
      })}
      <div className="project-switcher-footer"><Button type="button" variant="ghost" leadingIcon={<Plus size={16} />} disabled={saving || renaming} onKeyDown={handleKeys} onClick={() => { close(); scope.create() }}>{t('创建项目')}</Button></div>
    </div>
  </div>
}

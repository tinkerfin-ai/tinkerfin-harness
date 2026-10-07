import { ArrowUpRight, Check, MoreHorizontal } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { Button, IconButton, Surface, Tooltip } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { Project } from '../projects/api'
import { SkillDestinationPicker } from './SkillDestinationPicker'
import type { InstalledSkill, RemoteSkill, SkillSelection } from './model'

export function SkillIcon({ name }: { name: string }) {
  const tone = Array.from(name).reduce((sum, character) => sum + character.codePointAt(0)!, 0) % 6
  return <span className={`skills-icon skills-tone-${tone}`} aria-hidden="true">{Array.from(name)[0]?.toLocaleUpperCase()}</span>
}

export function SkillCard({ project, scopeId, selection, sourceName, installedIn, busy, error, onOpen, onInstall, onToggle, onUpdate, onUninstall }: {
  project: Project
  scopeId: string | null
  selection: SkillSelection; sourceName: string; installedIn: readonly (string | null)[]; busy: boolean; error?: string
  onOpen: (selection: SkillSelection, trigger: HTMLElement) => void
  onInstall: (skill: RemoteSkill, projectId: string | null) => Promise<boolean>; onToggle: (skill: InstalledSkill) => void
  onUninstall: (skill: InstalledSkill, trigger: HTMLElement) => void
  onUpdate: (skill: InstalledSkill, trigger: HTMLElement) => void
}) {
  const { t } = useI18n()
  const [hovered, setHovered] = useState(false)
  const [open, setOpen] = useState(false)
  const [choosingDestination, setChoosingDestination] = useState(false)
  const [destination, setDestination] = useState<string | null>(project.id)
  const installTrigger = useRef<HTMLButtonElement>(null)
  const detailTrigger = useRef<HTMLButtonElement>(null)
  const installForm = useRef<HTMLFormElement>(null)
  const projectInstalled = installedIn.includes(project.id)
  const personalInstalled = installedIn.includes(null)
  const menu = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const skill = selection.skill
  useLayoutEffect(() => {
    if (choosingDestination) installForm.current?.querySelector<HTMLInputElement>('input:checked:not(:disabled)')?.focus({ preventScroll: true })
  }, [choosingDestination])
  const closeDestination = () => { setChoosingDestination(false); (installTrigger.current ?? detailTrigger.current)?.focus({ preventScroll: true }) }
  const dismissInstallation = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape' && choosingDestination && !busy) { event.preventDefault(); event.stopPropagation(); closeDestination() }
  }
  const submitInstallation = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (selection.kind !== 'remote' || busy || installedIn.includes(destination)) return
    if (!await onInstall(selection.skill, destination)) return
    const restoreFocus = installForm.current?.contains(document.activeElement)
    setChoosingDestination(false)
    // 用户已转向其他卡片或弹窗时，安装完成不打断正在进行的操作
    if (restoreFocus) detailTrigger.current?.focus({ preventScroll: true })
  }
  useLayoutEffect(() => {
    if (!open) return
    const popup = menu.current
    const anchor = trigger.current?.getBoundingClientRect()
    if (!popup || !anchor) return
    const box = popup.getBoundingClientRect()
    const style = getComputedStyle(popup)
    const gap = parseFloat(style.getPropertyValue('--space-1'))
    const gutter = parseFloat(style.getPropertyValue('--space-2'))
    const top = anchor.bottom + gap + box.height <= window.innerHeight - gutter ? anchor.bottom + gap : anchor.top - gap - box.height
    popup.style.top = `${Math.max(gutter, Math.min(top, window.innerHeight - box.height - gutter))}px`
    popup.style.left = `${Math.max(gutter, Math.min(anchor.right - box.width, window.innerWidth - box.width - gutter))}px`
    popup.querySelector<HTMLButtonElement>('button')?.focus({ preventScroll: true })
    const dismiss = (event: PointerEvent) => {
      if (!menu.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setOpen(false)
    }
    const moved = (event: Event) => {
      if (event.target instanceof Node && popup.contains(event.target)) return
      setOpen(false); trigger.current?.focus({ preventScroll: true })
    }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('scroll', moved, true)
    window.addEventListener('resize', moved)
    return () => {
      document.removeEventListener('pointerdown', dismiss)
      document.removeEventListener('scroll', moved, true)
      window.removeEventListener('resize', moved)
    }
  }, [open])
  return <Surface as="article" className="skills-card" aria-label={skill.name} data-hovered={hovered || undefined} data-menu-open={open || undefined}
    onPointerMove={event => { if (event.pointerType === 'mouse') setHovered(true) }} onPointerLeave={() => setHovered(false)}>
    <div className="skills-card-top"><SkillIcon name={skill.name} />
      {selection.kind === 'installed' ? <div className="skills-card-management">
        <IconButton ref={trigger} type="button" size="sm" label={t('管理技能：{name}', { name: skill.name })} icon={<MoreHorizontal size={18} />}
          aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(value => !value)} />
        {open && createPortal(<div ref={menu} className="skills-menu" role="menu" tabIndex={-1} aria-label={t('技能操作')} onKeyDown={event => {
          if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus() }
          const entries = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [])
          if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
            event.preventDefault()
            const index = entries.indexOf(document.activeElement as HTMLButtonElement)
            entries[event.key === 'Home' ? 0 : event.key === 'End' ? entries.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + entries.length) % entries.length]?.focus()
          }
          if (event.key === 'Tab') { setOpen(false); trigger.current?.focus({ preventScroll: true }) }
        }}>
          <button type="button" role="menuitem" onClick={() => { setOpen(false); onOpen(selection, trigger.current!) }}>{t('详情')}</button>
          {selection.skill.project_id === scopeId && <button type="button" role="menuitem" disabled={busy} onClick={() => { setOpen(false); trigger.current?.focus({ preventScroll: true }); onUpdate(selection.skill, trigger.current!) }}>{t('更新')}</button>}
          {selection.skill.project_id === scopeId && <button type="button" role="menuitem" className="skills-menu-danger" disabled={busy} onClick={() => { setOpen(false); onUninstall(selection.skill, trigger.current!) }}>{t('卸载')}</button>}
        </div>, document.body)}
      </div> : skill.topics[0] && <span className="skills-card-topic">{skill.topics[0]}</span>}
    </div>
    <button ref={detailTrigger} type="button" className="skills-card-body" onClick={event => onOpen(selection, event.currentTarget)} aria-label={t('查看技能：{name}', { name: skill.name })}>
      <h2><span>{skill.name}</span><ArrowUpRight size={16} aria-hidden="true" /></h2>
      <p>{skill.description || t('查看技能说明')}</p>
    </button>
    {selection.kind === 'installed' && <span className="skills-scope-label">{t(selection.skill.overridden ? '被项目同名技能覆盖' : selection.skill.project_id ? '项目专属' : '个人共用')}</span>}
    <div className="skills-card-footer"><Tooltip content={skill.author || sourceName}><span className="skills-author">{skill.author || sourceName}</span></Tooltip>
      {selection.kind === 'installed' ? <button type="button" role="switch" className="skills-switch" aria-label={t('启用技能：{name}', { name: skill.name })}
        aria-checked={selection.skill.enabled && !selection.skill.overridden} aria-busy={busy || undefined} disabled={busy || selection.skill.overridden} onClick={() => onToggle(selection.skill)}>
        <span>{selection.skill.enabled ? t('已启用') : t('已停用')}</span><i aria-hidden="true"><b /></i>
      </button> : <div className="skills-install-actions">
        {(projectInstalled || personalInstalled) && <span className="skills-installed"><Check size={14} aria-hidden="true" />{t(projectInstalled && personalInstalled ? '已安装' : projectInstalled ? '当前项目已安装' : '个人共用已安装')}</span>}
        {!(projectInstalled && personalInstalled) && <Button ref={installTrigger} type="button" size="xs" className="skills-install" disabled={busy} aria-expanded={choosingDestination}
          onClick={() => { if (choosingDestination) closeDestination(); else { setDestination(projectInstalled ? null : project.id); setChoosingDestination(true) } }} onKeyDown={dismissInstallation} aria-label={t('安装技能：{name}', { name: skill.name })}>{t('安装')}</Button>}
      </div>}
    </div>
    {selection.kind === 'remote' && choosingDestination && <form ref={installForm} className="skills-install-form" aria-label={t('安装技能：{name}', { name: skill.name })}
      onSubmit={event => { void submitInstallation(event) }}>
      <SkillDestinationPicker project={project} value={destination} onChange={setDestination} onKeyDown={dismissInstallation} disabled={busy} installedIn={installedIn} />
      <div className="skills-install-form-actions"><Button type="button" variant="ghost" size="sm" disabled={busy} onClick={closeDestination} onKeyDown={dismissInstallation}>{t('取消')}</Button>
        <Button type="submit" size="sm" variant="primary" loading={busy} disabled={installedIn.includes(destination)} onKeyDown={dismissInstallation}>{t(destination ? '安装到当前项目' : '安装为个人共用')}</Button>
      </div>
    </form>}
    {error && <p className="skills-card-error" role="alert">{error}</p>}
  </Surface>
}

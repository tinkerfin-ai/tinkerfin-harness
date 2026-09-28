import { ArrowUpRight, Check, MoreHorizontal } from 'lucide-react'
import { useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Button, IconButton, Surface } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { InstalledSkill, RemoteSkill, SkillSelection } from './model'

export function SkillIcon({ name }: { name: string }) {
  const tone = Array.from(name).reduce((sum, character) => sum + character.codePointAt(0)!, 0) % 6
  return <span className={`skills-icon skills-tone-${tone}`} aria-hidden="true">{Array.from(name)[0]?.toLocaleUpperCase()}</span>
}

export function SkillCard({ selection, sourceName, installed, busy, error, onOpen, onInstall, onToggle, onUpdate, onUninstall }: {
  selection: SkillSelection; sourceName: string; installed: boolean; busy: boolean; error?: string
  onOpen: (selection: SkillSelection, trigger: HTMLElement) => void
  onInstall: (skill: RemoteSkill) => void; onToggle: (skill: InstalledSkill) => void
  onUninstall: (skill: InstalledSkill, trigger: HTMLElement) => void
  onUpdate: (skill: InstalledSkill, trigger: HTMLElement) => void
}) {
  const { t } = useI18n()
  const [hovered, setHovered] = useState(false)
  const [open, setOpen] = useState(false)
  const menu = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const skill = selection.skill
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
          <button type="button" role="menuitem" disabled={busy} onClick={() => { setOpen(false); trigger.current?.focus({ preventScroll: true }); onUpdate(selection.skill, trigger.current!) }}>{t('更新')}</button>
          <button type="button" role="menuitem" className="skills-menu-danger" disabled={busy} onClick={() => { setOpen(false); onUninstall(selection.skill, trigger.current!) }}>{t('卸载')}</button>
        </div>, document.body)}
      </div> : skill.topics[0] && <span className="skills-card-topic">{skill.topics[0]}</span>}
    </div>
    <button type="button" className="skills-card-body" onClick={event => onOpen(selection, event.currentTarget)} aria-label={t('查看技能：{name}', { name: skill.name })}>
      <h2><span>{skill.name}</span><ArrowUpRight size={16} aria-hidden="true" /></h2>
      <p>{skill.description || t('查看技能说明')}</p>
    </button>
    <div className="skills-card-footer"><span className="skills-author" title={skill.author || sourceName}>{skill.author || sourceName}</span>
      {selection.kind === 'installed' ? <button type="button" role="switch" className="skills-switch" aria-label={t('启用技能：{name}', { name: skill.name })}
        aria-checked={selection.skill.enabled} aria-busy={busy || undefined} disabled={busy} onClick={() => onToggle(selection.skill)}>
        <span>{selection.skill.enabled ? t('已启用') : t('已停用')}</span><i aria-hidden="true"><b /></i>
      </button> : installed ? <span className="skills-installed"><Check size={14} aria-hidden="true" />{t('已安装')}</span>
        : <Button type="button" size="xs" className="skills-install" loading={busy} onClick={() => onInstall(selection.skill)} aria-label={t('安装技能：{name}', { name: skill.name })}>{t('安装')}</Button>}
    </div>
    {error && <p className="skills-card-error" role="alert">{error}</p>}
  </Surface>
}

import { Check, ChevronDown } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { ListboxPicker } from '../components/ui/ListboxPicker'
import { send } from '../bridge'
import { useI18n } from '../i18n'
import type { Language } from '../state'

export function LanguagePicker({ value, disabled }: { value: Language; disabled: boolean }) {
  const { t } = useI18n()
  const anchor = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState<CSSProperties>({})
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const box = anchor.current?.getBoundingClientRect()
      if (!box) return
      const menu = document.querySelector<HTMLElement>('.desktop-language-menu')
      const height = Math.min(menu?.scrollHeight || 160, innerHeight - 16)
      const below = innerHeight - box.bottom - 12, above = box.top - 12
      const up = below < height && above > below, room = Math.max(0, up ? above : below)
      const visibleHeight = Math.min(height, room), width = Math.min(box.width, innerWidth - 16)
      setPosition({ position: 'fixed', left: Math.max(8, Math.min(box.left, innerWidth - width - 8)), top: up ? Math.max(8, box.top - 4 - visibleHeight) : Math.min(box.bottom + 4, innerHeight - visibleHeight - 8), width, maxHeight: room, overflowY: 'auto', margin: 0 })
    }
    place(); window.addEventListener('resize', place); window.addEventListener('scroll', place, true)
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true) }
  }, [open])
  const labels = { system: t('跟随系统'), 'zh-Hans': '简体中文', en: 'English' }
  return <div ref={anchor}><ListboxPicker value={value} disabled={disabled} options={['system', 'zh-Hans', 'en'] as const} open={open} onOpenChange={setOpen}
    onChange={language => void send({ type: 'language', language })} triggerLabel={`${t('语言')}：${labels[value]}`} listboxLabel={t('语言')}
    listboxPortalTarget={document.body} listboxStyle={position} rootClassName="settings-choice-picker desktop-language-picker" triggerClassName="settings-choice-trigger" listboxClassName="settings-choice-options desktop-language-menu" optionClassName="settings-choice-option"
    renderTrigger={selected => <><span>{labels[selected]}</span><span className="row-action-icon" aria-hidden="true"><ChevronDown size={16} /></span></>}
    renderOption={(option, selected) => <><span>{labels[option]}</span><span className="settings-choice-check">{selected && <Check size={16} aria-hidden="true" />}</span></>} /></div>
}

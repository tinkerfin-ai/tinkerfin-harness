import { FileText } from 'lucide-react'
import { useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { Button } from '../../components/ui'
import { restoreFocus } from '../../components/ui/focus'
import { useI18n } from '../../i18n'
import type { WorkspaceFile } from './api'
import { formatWorkspaceFileSize, workspaceFileFormat } from './workspaceFilePresentation'

export function WorkspaceFileInformation({ file }: { file: WorkspaceFile }) {
  const { t, locale } = useI18n()
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const information = useRef<HTMLElement>(null)
  const [visible, setVisible] = useState(false)
  const [position, setPosition] = useState<CSSProperties>({ left: 0, top: 0 })
  const pinned = useRef(false)
  const pointerState = useRef({ open: false, pinned: false })
  const reveal = (pin: boolean) => {
    pinned.current = pin
    information.current?.showPopover()
    if (pin) restoreFocus(information.current, { preventScroll: true })
  }
  const dismiss = (returnFocus = false) => {
    pinned.current = false
    information.current?.hidePopover()
    if (returnFocus) restoreFocus(trigger.current, { preventScroll: true })
  }
  useLayoutEffect(() => {
    if (!visible) return
    const fit = () => {
      const anchor = trigger.current?.getBoundingClientRect()
      const panel = information.current?.getBoundingClientRect()
      if (!anchor || !panel) return
      const tokens = getComputedStyle(document.documentElement)
      const inset = parseFloat(tokens.getPropertyValue('--space-4')) || 0
      const gap = parseFloat(tokens.getPropertyValue('--space-2')) || 0
      setPosition({ left: Math.max(inset, Math.min(anchor.right - panel.width, innerWidth - panel.width - inset)),
        top: Math.max(inset, Math.min(anchor.bottom + gap, innerHeight - panel.height - inset)) })
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      event.preventDefault()
      event.stopPropagation()
      dismiss(true)
    }
    const trackPointer = (event: PointerEvent) => {
      if (pinned.current || event.pointerType !== 'mouse') return
      const anchor = trigger.current?.getBoundingClientRect()
      const panel = information.current?.getBoundingClientRect()
      if (!anchor || !panel) return
      const contains = (rect: DOMRect) => event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom
      if (contains(anchor) || contains(panel)) return
      // 保留按钮到浮层之间的指针通路，斜向移入也不会提前关闭
      if (panel.top > anchor.top && event.clientY >= anchor.top && event.clientY <= panel.top) {
        const progress = (event.clientY - anchor.top) / (panel.top - anchor.top)
        const left = anchor.left + (panel.left - anchor.left) * progress
        const right = anchor.right + (panel.right - anchor.right) * progress
        if (event.clientX >= left && event.clientX <= right) return
      }
      dismiss()
    }
    const leaveWindow = (event: PointerEvent) => { if (!event.relatedTarget && !pinned.current) dismiss() }
    fit()
    const observer = new ResizeObserver(fit)
    if (trigger.current) observer.observe(trigger.current)
    if (information.current) observer.observe(information.current)
    window.addEventListener('resize', fit)
    window.addEventListener('scroll', fit, true)
    document.addEventListener('keydown', escape, true)
    document.addEventListener('pointermove', trackPointer)
    document.addEventListener('pointerout', leaveWindow)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', fit)
      window.removeEventListener('scroll', fit, true)
      document.removeEventListener('keydown', escape, true)
      document.removeEventListener('pointermove', trackPointer)
      document.removeEventListener('pointerout', leaveWindow)
    }
  }, [visible, file])
  return <>
    <Button ref={trigger} type="button" variant="ghost" size="sm" shape="circle" className="workspace-file-information-trigger" leadingIcon={<FileText size={18} />}
      aria-label={t('文件信息')} aria-expanded={visible} aria-controls={id} popoverTarget={id}
      onPointerEnter={event => { if (event.pointerType === 'mouse' && !visible) reveal(false) }}
      onPointerDown={() => { pointerState.current = { open: visible, pinned: pinned.current } }}
      onClick={event => {
        event.preventDefault()
        // 原生外部关闭可能先于 click；按按下时的披露状态判断再次点击
        const previous = event.detail ? pointerState.current : { open: visible, pinned: pinned.current }
        if (previous.open && previous.pinned) dismiss(true)
        else reveal(true)
      }} />
    <aside ref={information} id={id} className="workspace-file-information" popover="auto" aria-label={t('文件信息')} aria-hidden={!visible} tabIndex={-1} style={position}
      onToggle={event => { const shown = event.newState === 'open'; setVisible(shown); if (!shown) pinned.current = false }}>
      <strong>{t('文件信息')}</strong>
      <dl><dt>{t('名称')}</dt><dd>{file.name}</dd><dt>{t('类型')}</dt><dd>{workspaceFileFormat(file.name)}</dd>
        <dt>{t('大小')}</dt><dd>{formatWorkspaceFileSize(file.sizeBytes, locale)}</dd><dt>{t('修改时间')}</dt><dd>{new Date(file.modifiedAt).toLocaleString(locale, { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })}</dd>
        <dt>{t('完整路径')}</dt><dd>{file.path}</dd></dl>
    </aside>
  </>
}

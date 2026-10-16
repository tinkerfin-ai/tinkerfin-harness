import { useEffect, useLayoutEffect, useRef, type HTMLAttributes, type ReactNode, type Ref } from 'react'
import { DrawerHeader } from './DrawerHeader'
import { restoreFocus } from './focus'

export interface DrawerProps extends Omit<HTMLAttributes<HTMLElement>, 'title' | 'children'> {
  id: string
  open: boolean
  title: string
  description?: ReactNode
  actions?: ReactNode
  closeLabel: string
  backLabel?: string
  fullPage?: boolean
  resizeHandle?: ReactNode
  drawerRef?: Ref<HTMLElement>
  className?: string
  announcement?: ReactNode
  children: ReactNode
  onClose: () => void
}

/** 共享侧栏与全宽详情的容器、标题和 Escape 关闭，业务负责返回入口焦点 */
export function Drawer({ id, open, title, description, actions, closeLabel, backLabel, fullPage = false,
  resizeHandle, drawerRef, className, announcement, children, onClose, ...attributes }: DrawerProps) {
  const closeRef = useRef<HTMLButtonElement>(null)
  useLayoutEffect(() => {
    if (open) restoreFocus(closeRef.current, { preventScroll: true })
  }, [open, fullPage])
  useEffect(() => {
    if (!open) return
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector('[aria-modal="true"]')
        || (event.target instanceof Element && event.target.closest('[role="listbox"], [role="menu"]'))) return
      event.preventDefault()
      onClose()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [open, onClose])
  return <aside {...attributes} ref={drawerRef} id={id}
    className={['ui-drawer', className, open && 'is-open', fullPage && 'is-full-page'].filter(Boolean).join(' ')}
    aria-label={title} aria-hidden={!open || undefined} inert={!open || undefined}>
    {open && !fullPage && resizeHandle}
    {announcement}
    <DrawerHeader ref={closeRef} title={title} description={description} actions={actions} closeLabel={closeLabel}
      onClose={onClose} backLabel={backLabel} onBack={fullPage && backLabel ? onClose : undefined} />
    <div className="ui-drawer__body">{children}</div>
  </aside>
}

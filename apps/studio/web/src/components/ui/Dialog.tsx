import { X } from 'lucide-react'
import { useCallback, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode, RefObject } from 'react'
import { createPortal } from 'react-dom'

import { useI18n } from '../../i18n'
import { IconButton } from './IconButton'
import { DialogContext, DialogDetailContext } from './DialogContext'
import type { DialogDetailOptions } from './DialogContext'

const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  'input:not([disabled])',
  'textarea:not([disabled])',
  'select:not([disabled])',
  '[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export interface DialogProps {
  open: boolean
  title: ReactNode
  description?: ReactNode
  children: ReactNode
  className?: string
  fullScreen?: boolean
  header?: ReactNode
  closeDisabled?: boolean
  restoreFocusTo?: HTMLElement | null
  initialFocusRef?: RefObject<HTMLElement | null>
  headerActions?: ReactNode
  onKeyDown?: (event: KeyboardEvent<HTMLDivElement>) => void
  onClose: () => void
}

export function Dialog({
  open,
  title,
  description,
  children,
  className,
  closeDisabled = false,
  fullScreen = false,
  header,
  restoreFocusTo,
  initialFocusRef,
  headerActions,
  onKeyDown,
  onClose,
}: DialogProps) {
  const { t } = useI18n()
  const titleId = useId()
  const descriptionId = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)
  const [detail, setDetail] = useState<DialogDetailOptions | null>(null)
  const [detailTarget, setDetailTarget] = useState<HTMLDivElement | null>(null)
  const showDetail = useCallback((next: DialogDetailOptions) => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setDetail(next)
    return () => {
      setDetail(current => current === next ? null : current)
      queueMicrotask(() => { if (previous?.isConnected && !previous.closest('[inert], [hidden]')) previous.focus({ preventScroll: true }) })
    }
  }, [])
  const detailHost = useMemo(() => ({ target: detailTarget, show: showDetail }), [detailTarget, showDetail])
  const expanded = fullScreen || Boolean(detail)
  useLayoutEffect(() => {
    if (detail) detailTarget?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR)?.focus()
  }, [detail, detailTarget])
  useLayoutEffect(() => {
    if (!open || !expanded) return
    const root = document.getElementById('root')
    const inert = root?.inert
    const overflow = document.body.style.overflow
    if (root) root.inert = true
    document.body.style.overflow = 'hidden'
    return () => { if (root) root.inert = inert ?? false; document.body.style.overflow = overflow }
  }, [open, expanded])


  useLayoutEffect(() => {
    if (!open) return
    restoreFocusRef.current = restoreFocusTo
      ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null)
    ;(initialFocusRef?.current ?? closeRef.current ?? dialogRef.current)?.focus()
    return () => {
      const target = restoreFocusRef.current
      // 等待宿主解除模态隔离；其他交互已设置新焦点时不再抢回入口
      queueMicrotask(() => {
        if (document.activeElement === document.body) target?.focus()
      })
    }
  }, [initialFocusRef, open, restoreFocusTo])

  if (!open) return null

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!detail) onKeyDown?.(event)
    // 内层列表框等浮层先处理键盘事件时，外层对话框必须保持不变
    if (event.defaultPrevented) return
    if (event.key === 'Escape' && !closeDisabled) {
      event.preventDefault()
      if (detail) detail.onClose()
      else onClose()
      return
    }
    if (event.key !== 'Tab') return

    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [],
    ).filter(element => !element.closest('[hidden], [inert]'))
    if (focusable.length === 0) {
      event.preventDefault()
      dialogRef.current?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && (
      document.activeElement === first
      || !dialogRef.current?.contains(document.activeElement)
    )) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return createPortal(
    <DialogContext.Provider value={true}>
    <DialogDetailContext.Provider value={detailHost}>
    {/* 遮罩只处理对话框外部的指针取消，不进入键盘顺序，键盘关闭由对话框自身负责 */}
    {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
    <div
      className={`modal-backdrop${expanded ? ' modal-backdrop--fullscreen' : ''}`}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !closeDisabled && !expanded) onClose()
      }}
    >
      {/* 对话框通过此键盘入口维护 Escape 和 Tab 焦点循环 */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        ref={dialogRef}
        className={`modal-dialog${className ? ` ${className}` : ''}${expanded ? ' modal-dialog--fullscreen' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={!detail && header === undefined ? titleId : undefined}
        aria-label={detail?.title ?? (header !== undefined && typeof title === 'string' ? title : undefined)}
        aria-describedby={!detail && description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <div className="ui-dialog-content" hidden={Boolean(detail)} inert={Boolean(detail)}>
        {header === undefined ? <header className="modal-dialog-head">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && (
              <div id={descriptionId} className="modal-dialog-description">{description}</div>
            )}
          </div>
          {headerActions}
          <IconButton
            ref={closeRef}
            className="modal-dialog-close"
            label={t('关闭对话框')}
            icon={<X size={18} />}
            disabled={closeDisabled}
            onClick={onClose}
          />
        </header> : header}
        {children}
        </div>
        <div ref={setDetailTarget} className="ui-dialog-detail" hidden={!detail} />
      </div>
    </div>
    </DialogDetailContext.Provider>
    </DialogContext.Provider>,
    document.body,
  )
}

import { X } from 'lucide-react'
import { useId, useLayoutEffect, useRef } from 'react'
import type { KeyboardEvent, ReactNode, RefObject } from 'react'
import { createPortal } from 'react-dom'

import { useI18n } from '../../i18n'
import { IconButton } from './IconButton'
import { DialogContext } from './DialogContext'

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
  title: string
  description?: ReactNode
  children: ReactNode
  className?: string
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
    onKeyDown?.(event)
    // 内层列表框等浮层先处理键盘事件时，外层对话框必须保持不变
    if (event.defaultPrevented) return
    if (event.key === 'Escape' && !closeDisabled) {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== 'Tab') return

    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [],
    )
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
    {/* 遮罩只处理对话框外部的指针取消，不进入键盘顺序，键盘关闭由对话框自身负责 */}
    {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !closeDisabled) onClose()
      }}
    >
      {/* 对话框通过此键盘入口维护 Escape 和 Tab 焦点循环 */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        ref={dialogRef}
        className={`modal-dialog${className ? ` ${className}` : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="modal-dialog-head">
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
        </header>
        {children}
      </div>
    </div>
    </DialogContext.Provider>,
    document.body,
  )
}

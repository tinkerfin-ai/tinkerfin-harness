import { cloneElement, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import type { CSSProperties, HTMLAttributes, ReactElement, ReactNode, Ref, RefObject } from 'react'
import { createPortal } from 'react-dom'

type TooltipPlacement = 'top' | 'bottom' | 'left' | 'right'
interface TooltipTriggerProps extends HTMLAttributes<HTMLElement> { ref?: Ref<HTMLElement> }
export interface TooltipProps {
  /** 触发元素需要支持 DOM 引用，工具提示不增加额外布局容器 */
  children: ReactElement<TooltipTriggerProps>
  content: ReactNode
  placement?: TooltipPlacement
  enabled?: boolean
  overflowOnly?: boolean
  overflowRef?: RefObject<HTMLElement | null>
  touchToggle?: boolean
  open?: boolean
  onOpenChange?: (open: boolean) => void
  className?: string
}

/** 统一悬浮、键盘描述与 Escape 关闭；触发器可用 --tooltip-gap 指定间距，长内容支持滚动和翻页键 */
export function Tooltip({ children, content, placement = 'bottom', enabled = true, overflowOnly = false, overflowRef, touchToggle = false, open: controlledOpen, onOpenChange, className }: TooltipProps) {
  const id = useId()
  const anchor = useRef<HTMLElement | null>(null)
  const tip = useRef<HTMLSpanElement>(null)
  const scrollContent = useRef<HTMLSpanElement>(null)
  const touch = useRef(false)
  const hovering = useRef(false)
  const keyboardFocus = useRef(false)
  const [requestedOpen, setRequestedOpen] = useState(false)
  const available = enabled && content != null && content !== ''
  const open = available && (controlledOpen ?? requestedOpen)
  const [position, setPosition] = useState({ left: 0, top: 0, gap: 0 })
  const originalRef = children.props.ref
  const setAnchor = useCallback((node: HTMLElement | null) => {
    anchor.current = node
    if (typeof originalRef === 'function') return originalRef(node)
    if (originalRef) originalRef.current = node
  }, [originalRef])
  const sync = () => { const value = hovering.current || keyboardFocus.current; setRequestedOpen(value); onOpenChange?.(value) }
  const dismiss = () => { hovering.current = false; keyboardFocus.current = false; sync() }
  const reveal = (source: 'pointer' | 'keyboard') => {
    const measured = overflowRef?.current ?? anchor.current
    if (!available || (overflowOnly && (!measured || measured.scrollWidth <= measured.clientWidth))) return
    if (source === 'pointer') hovering.current = true
    else keyboardFocus.current = true
    sync()
  }
  const leave = (relatedTarget: EventTarget | null) => {
    if (relatedTarget instanceof Node && (anchor.current?.contains(relatedTarget) || tip.current?.contains(relatedTarget))) return
    hovering.current = false
    sync()
  }

  useLayoutEffect(() => {
    if (!open) return
    const fit = () => {
      const element = anchor.current
      const tooltip = tip.current?.getBoundingClientRect()
      if (!element || !tooltip) return
      const bounds = element.getBoundingClientRect()
      const tokens = getComputedStyle(document.documentElement)
      const gap = parseFloat(getComputedStyle(element).getPropertyValue('--tooltip-gap')) || parseFloat(tokens.getPropertyValue('--space-1')) || 0
      const inset = parseFloat(tokens.getPropertyValue('--space-3')) || 0
      let left = bounds.left + (bounds.width - tooltip.width) / 2
      let top = placement === 'top' ? bounds.top - tooltip.height - gap : bounds.bottom + gap
      if (placement === 'right' || placement === 'left') {
        left = placement === 'right' ? bounds.right + gap : bounds.left - tooltip.width - gap
        top = bounds.top + (bounds.height - tooltip.height) / 2
        if (left + tooltip.width > window.innerWidth - inset) left = bounds.left - tooltip.width - gap
        if (left < inset) left = bounds.right + gap
      } else {
        if (top + tooltip.height > window.innerHeight - inset) top = bounds.top - tooltip.height - gap
        if (top < inset) top = bounds.bottom + gap
      }
      setPosition({ left: Math.max(inset, Math.min(left, window.innerWidth - tooltip.width - inset)), top: Math.max(inset, Math.min(top, window.innerHeight - tooltip.height - inset)), gap })
    }
    fit()
    const observer = new ResizeObserver(fit)
    if (anchor.current) observer.observe(anchor.current)
    if (tip.current) observer.observe(tip.current)
    window.addEventListener('resize', fit)
    window.addEventListener('scroll', fit, true)
    return () => { observer.disconnect(); window.removeEventListener('resize', fit); window.removeEventListener('scroll', fit, true) }
  }, [open, content, placement])

  useEffect(() => {
    if (!open) return
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) { event.preventDefault(); event.stopPropagation(); dismiss() }
    }
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !anchor.current?.contains(event.target) && !tip.current?.contains(event.target)) dismiss()
    }
    document.addEventListener('keydown', onEscape, true)
    document.addEventListener('pointerdown', outside)
    return () => { document.removeEventListener('keydown', onEscape, true); document.removeEventListener('pointerdown', outside) }
  })

  const describedBy = [children.props['aria-describedby'], open ? id : undefined].filter(Boolean).join(' ') || undefined
  const trigger = cloneElement(children, {
    ref: setAnchor,
    'aria-describedby': describedBy,
    'aria-description': available && typeof content === 'string' ? content : children.props['aria-description'],
    onKeyDown: event => {
      children.props.onKeyDown?.(event)
      const body = scrollContent.current
      if (!open || event.defaultPrevented || !body || body.scrollHeight <= body.clientHeight) return
      if (event.key === 'PageDown' || event.key === 'PageUp') {
        event.preventDefault(); body.scrollTop += body.clientHeight * (event.key === 'PageDown' ? 1 : -1)
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault(); body.scrollTop = event.key === 'Home' ? 0 : body.scrollHeight
      }
    },
    onPointerDown: event => { touch.current = event.pointerType === 'touch'; children.props.onPointerDown?.(event) },
    onPointerEnter: event => { touch.current = event.pointerType === 'touch'; children.props.onPointerEnter?.(event) },
    onPointerMove: event => { children.props.onPointerMove?.(event); if (event.pointerType !== 'touch') reveal('pointer') },
    onPointerLeave: event => { children.props.onPointerLeave?.(event); if (event.pointerType !== 'touch') leave(event.relatedTarget) },
    onFocus: event => { children.props.onFocus?.(event); if (event.currentTarget.matches(':focus-visible')) reveal('keyboard') },
    onBlur: event => { children.props.onBlur?.(event); keyboardFocus.current = false; sync() },
    onClick: event => { children.props.onClick?.(event); if (touchToggle && touch.current) { if (open) dismiss(); else reveal('pointer') } else dismiss() },
  })
  const portalTarget = anchor.current?.closest('[popover],dialog[open]') ?? document.body
  return <>{trigger}{open && createPortal(<span ref={tip} id={id} role="tooltip" className={['ui-tooltip', className].filter(Boolean).join(' ')} style={{ left: position.left, top: position.top, '--tooltip-gap': `${position.gap}px` } as CSSProperties}
    onPointerMove={event => event.stopPropagation()} onPointerEnter={() => reveal('pointer')} onPointerLeave={event => leave(event.relatedTarget)}><span ref={scrollContent} className="ui-tooltip__content" tabIndex={-1}>{content}</span></span>, portalTarget)}</>
}

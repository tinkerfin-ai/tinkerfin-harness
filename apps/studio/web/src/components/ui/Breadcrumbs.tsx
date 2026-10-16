import { Fragment, useLayoutEffect, useRef, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { Button } from './Button'
import { Tooltip } from './Tooltip'
import './breadcrumbs.css'

export interface BreadcrumbItem {
  label: string
  onNavigate?: () => void
}

export interface BreadcrumbsProps {
  label: string
  items: readonly BreadcrumbItem[]
  current?: 'page' | 'location'
  disabled?: boolean
  size?: 'sm' | 'md'
  className?: string
}

/** 展示当前位置及可返回的上级；业务提供导航动作，最后一项始终表示当前位置 */
export function Breadcrumbs({ label, items, current = 'page', disabled = false, size = 'sm', className }: BreadcrumbsProps) {
  const viewport = useRef<HTMLDivElement>(null)
  const trail = useRef<HTMLOListElement>(null)
  const [scrollable, setScrollable] = useState(false)
  const drag = useRef<{ pointer: number; x: number; left: number; moved: boolean } | undefined>(undefined)
  const location = JSON.stringify(items.map(item => item.label))
  useLayoutEffect(() => {
    const element = viewport.current
    if (!element) return
    element.scrollLeft = Math.max(0, element.scrollWidth - element.clientWidth)
    const measure = () => setScrollable(element.scrollWidth > element.clientWidth)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    if (trail.current) observer.observe(trail.current)
    return () => observer.disconnect()
  }, [location])
  const release = (element: HTMLElement, pointer: number) => {
    if (drag.current?.moved && element.hasPointerCapture(pointer)) element.releasePointerCapture(pointer)
    delete element.dataset.dragging
  }
  return <nav className={['ui-breadcrumbs', `ui-breadcrumbs--${size}`, className].filter(Boolean).join(' ')} aria-label={label}>
    <div ref={viewport} className="ui-breadcrumbs__viewport" role="region" aria-label={label} tabIndex={scrollable ? 0 : -1} data-scrollable={scrollable}
    onPointerDown={event => { drag.current = event.pointerType === 'mouse' && event.button === 0 && scrollable ? { pointer: event.pointerId, x: event.clientX, left: event.currentTarget.scrollLeft, moved: false } : undefined }}
    onPointerMove={event => {
      const gesture = drag.current
      if (!gesture || gesture.pointer !== event.pointerId || event.buttons !== 1) return
      const distance = event.clientX - gesture.x
      if (!gesture.moved && Math.abs(distance) <= 6) return
      if (!gesture.moved) event.currentTarget.setPointerCapture(event.pointerId)
      gesture.moved = true
      event.currentTarget.dataset.dragging = 'true'
      event.currentTarget.scrollLeft = gesture.left - distance
      event.preventDefault()
    }}
    onPointerUp={event => release(event.currentTarget, event.pointerId)}
    onPointerCancel={event => { release(event.currentTarget, event.pointerId); drag.current = undefined }}
    onClickCapture={event => { if (drag.current?.moved) { event.preventDefault(); event.stopPropagation() }; drag.current = undefined }}
>
    <ol ref={trail}>{items.map((item, index) => <Fragment key={index}>
      {index > 0 && <li aria-hidden="true" className="ui-breadcrumbs__separator"><ChevronRight size={12} /></li>}
      <li>{index === items.length - 1
        ? <Tooltip content={item.label} overflowOnly className="ui-breadcrumbs__tooltip"><span aria-current={current}>{item.label}</span></Tooltip>
        : <Button type="button" size={size} variant="text" disabled={disabled || !item.onNavigate} onClick={item.onNavigate}>{item.label}</Button>}
      </li>
    </Fragment>)}</ol>
    </div>
  </nav>
}

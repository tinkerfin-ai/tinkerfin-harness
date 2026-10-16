import { useId, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useI18n } from '../../i18n'

/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex -- 可调整 separator 是 WAI-ARIA 范围控件，支持聚焦和键盘调整 */

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

/** 提供方分栏只维护当前页面宽度，拖动不能挤掉右侧模型操作 */
export function ModelProviderSplit({ sidebar, children }: { sidebar: ReactNode; children: ReactNode }) {
  const { t } = useI18n()
  const id = useId()
  const root = useRef<HTMLDivElement>(null)
  const drag = useRef<{ pointerId: number; x: number; width: number } | null>(null)
  const [bounds, setBounds] = useState({ min: 144, max: 320 })
  const [width, setWidth] = useState(180)

  useLayoutEffect(() => {
    const element = root.current
    if (!element) return
    const measure = () => {
      const style = getComputedStyle(element)
      if (style.display !== 'grid') return
      const unit = parseFloat(style.getPropertyValue('--space-4'))
      if (!unit) return
      const min = unit * 9
      const max = Math.max(min, Math.min(unit * 20, element.clientWidth - unit * 16 - parseFloat(style.columnGap) * 2 - unit))
      setBounds({ min, max })
      setWidth(current => clamp(current, min, max))
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  const apply = (value: number) => setWidth(clamp(value, bounds.min, bounds.max))
  return <div ref={root} className="settings-models__workspace" style={{ '--provider-width': `${width}px` } as CSSProperties}>
    <div id={id} className="settings-models__sidebar">{sidebar}</div>
    <div
      tabIndex={0}
      role="separator"
      className="settings-models__splitter"
      aria-label={t('调整提供方列表宽度')}
      aria-controls={id}
      aria-orientation="vertical"
      aria-valuemin={Math.round(bounds.min)}
      aria-valuemax={Math.round(bounds.max)}
      aria-valuenow={Math.round(width)}
      onKeyDown={event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
        event.preventDefault()
        const step = event.shiftKey ? 32 : 8
        apply(event.key === 'Home' ? bounds.min : event.key === 'End' ? bounds.max : width + (event.key === 'ArrowLeft' ? -step : step))
      }}
      onPointerDown={event => {
        if (event.button !== 0 || drag.current) return
        event.preventDefault()
        drag.current = { pointerId: event.pointerId, x: event.clientX, width }
        event.currentTarget.setPointerCapture(event.pointerId)
      }}
      onPointerMove={event => {
        if (drag.current?.pointerId === event.pointerId) apply(drag.current.width + event.clientX - drag.current.x)
      }}
      onPointerUp={event => {
        if (drag.current?.pointerId !== event.pointerId) return
        apply(drag.current.width + event.clientX - drag.current.x)
        drag.current = null
        event.currentTarget.releasePointerCapture(event.pointerId)
      }}
      onPointerCancel={() => {
        if (drag.current) apply(drag.current.width)
        drag.current = null
      }}
      onLostPointerCapture={() => { drag.current = null }}
    />
    {children}
  </div>
}

import type { CSSProperties } from 'react'

export interface BrandMarkProps {
  size?: number
  className?: string
}

export function BrandMark({ size = 22, className }: BrandMarkProps = {}) {
  const style = { '--brand-mark-height': `${size}px` } as CSSProperties

  return (
    <span
      className={className ? `brand-mark ${className}` : 'brand-mark'}
      style={style}
      aria-hidden="true"
    >
      <img src="/brand/tinkerfin-mark.png?v=1" width="647" height="458" alt="" />
    </span>
  )
}

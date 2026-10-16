import { createElement } from 'react'
import type { HTMLAttributes } from 'react'

export type SurfaceTone = 'neutral' | 'brand' | 'danger' | 'success' | 'warning'
export type SurfaceElevation = 0 | 1 | 2 | 3

export interface SurfaceProps extends HTMLAttributes<HTMLElement> {
  as?: 'div' | 'section' | 'article' | 'aside'
  tone?: SurfaceTone
  elevation?: SurfaceElevation
}

export function Surface({
  as = 'div',
  tone = 'neutral',
  elevation = 0,
  className,
  ...surfaceProps
}: SurfaceProps) {
  const classes = [
    'ui-surface',
    `ui-surface--${tone}`,
    `ui-surface--elevation-${elevation}`,
    className,
  ].filter(Boolean).join(' ')

  return createElement(as, { ...surfaceProps, className: classes })
}

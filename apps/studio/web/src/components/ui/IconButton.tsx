import { forwardRef, useId } from 'react'
import type { ReactNode } from 'react'

import { Button } from './Button'
import type { ButtonProps } from './Button'

export interface IconButtonProps extends Omit<
  ButtonProps,
  'aria-label' | 'children' | 'leadingIcon' | 'trailingIcon' | 'shape'
> {
  label: string
  icon: ReactNode
  tooltip?: string
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton({
  label,
  icon,
  tooltip,
  className,
  'aria-describedby': describedBy,
  ...buttonProps
}, ref) {
  const tooltipId = useId()
  const classes = ['ui-icon-button', className].filter(Boolean).join(' ')

  return (
    <span className="ui-icon-button-wrap">
      <Button
        {...buttonProps}
        ref={ref}
        className={classes}
        shape="circle"
        aria-label={label}
        aria-describedby={[describedBy, tooltip ? tooltipId : undefined].filter(Boolean).join(' ') || undefined}
        leadingIcon={<span className="ui-icon-button__icon">{icon}</span>}
      />
      {tooltip && <span id={tooltipId} className="ui-tooltip" role="tooltip">{tooltip}</span>}
    </span>
  )
})

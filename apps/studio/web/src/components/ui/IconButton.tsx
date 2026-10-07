import { forwardRef } from 'react'
import type { ReactNode } from 'react'

import { Button } from './Button'
import { Tooltip } from './Tooltip'
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
  tooltipPlacement,
  className,
  'aria-describedby': describedBy,
  ...buttonProps
}, ref) {
  const classes = ['ui-icon-button', className].filter(Boolean).join(' ')

  const button = (
      <Button
        {...buttonProps}
        ref={ref}
        className={classes}
        shape="circle"
        aria-label={label}
        aria-describedby={describedBy}
        leadingIcon={<span className="ui-icon-button__icon">{icon}</span>}
      />
  )
  return <span className="ui-icon-button-wrap">{tooltip ? <Tooltip content={tooltip} placement={tooltipPlacement}>{button}</Tooltip> : button}</span>
})

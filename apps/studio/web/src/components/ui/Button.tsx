import { LoaderCircle } from 'lucide-react'
import { forwardRef } from 'react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Tooltip } from './Tooltip'
import type { TooltipProps } from './Tooltip'

export type ButtonVariant = 'primary' | 'solid' | 'secondary' | 'ghost' | 'danger' | 'text'
export type ButtonSize = 'xs' | 'sm' | 'md' | 'lg' | 'xl'
export type ButtonShape = 'round' | 'capsule' | 'circle'

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'title'> {
  tooltip?: string
  tooltipPlacement?: TooltipProps['placement']
  variant?: ButtonVariant
  size?: ButtonSize
  shape?: ButtonShape
  loading?: boolean
  selected?: boolean
  leadingIcon?: ReactNode
  trailingIcon?: ReactNode
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({
  tooltip,
  tooltipPlacement,
  variant = 'secondary',
  size = 'xs',
  shape = 'round',
  loading = false,
  selected,
  leadingIcon,
  trailingIcon,
  type = 'button',
  disabled,
  className,
  children,
  ...buttonProps
}, ref) {
  const classes = [
    'ui-button',
    `ui-button--${variant}`,
    `ui-button--${size}`,
    `ui-button--${shape}`,
    selected === true ? 'is-selected' : '',
    className,
  ].filter(Boolean).join(' ')

  const button = (
    <button
      {...buttonProps}
      ref={ref}
      className={classes}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      aria-pressed={buttonProps['aria-pressed'] ?? selected}
    >
      {loading
        ? <LoaderCircle className="ui-button__spinner" size={16} aria-hidden="true" />
        : leadingIcon && <span className="ui-button__icon" aria-hidden="true">{leadingIcon}</span>}
      {children !== undefined && <span className="ui-button__label">{children}</span>}
      {!loading && trailingIcon && <span className="ui-button__icon" aria-hidden="true">{trailingIcon}</span>}
    </button>
  )
  return tooltip ? <Tooltip content={tooltip} placement={tooltipPlacement}>{button}</Tooltip> : button
})

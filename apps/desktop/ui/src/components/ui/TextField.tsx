import { LoaderCircle } from 'lucide-react'
import { forwardRef, useId } from 'react'
import type { InputHTMLAttributes, ReactNode } from 'react'

import { useI18n } from '../../i18n'

export type TextFieldSize = 'md' | 'lg'
export type TextFieldShape = 'round' | 'capsule' | 'standard'

export interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  label: ReactNode
  error?: ReactNode
  helperText?: ReactNode
  leadingContent?: ReactNode
  trailingContent?: ReactNode
  loading?: boolean
  fieldSize?: TextFieldSize
  shape?: TextFieldShape
  rootClassName?: string
}

export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField({
  id,
  label,
  error,
  helperText,
  leadingContent,
  trailingContent,
  loading = false,
  fieldSize = 'lg',
  shape = 'round',
  rootClassName,
  className,
  ...inputProps
}, ref) {
  const { t } = useI18n()
  const generatedId = useId()
  const controlId = id ?? `field-${generatedId}`
  const errorId = `${controlId}-error`
  const helperId = `${controlId}-helper`
  const descriptionIds = [error ? errorId : '', helperText ? helperId : '']
    .filter(Boolean)
    .join(' ') || undefined
  const rootClasses = [
    'ui-text-field',
    `ui-text-field--${fieldSize}`,
    `ui-text-field--${shape}`,
    error ? 'is-invalid' : '',
    rootClassName,
  ].filter(Boolean).join(' ')

  return (
    <div className={rootClasses} data-validation-field={inputProps.name}>
      <label className="ui-text-field__label" htmlFor={controlId}>{label}</label>
      <span className="ui-text-field__control" data-validation-feedback={error ? 'invalid' : undefined}>
        {leadingContent && <span className="ui-text-field__leading" aria-hidden="true">{leadingContent}</span>}
        <input
          {...inputProps}
          ref={ref}
          id={controlId}
          className={className}
          aria-invalid={Boolean(error)}
          aria-busy={loading || undefined}
          aria-describedby={descriptionIds}
        />
        {trailingContent && <span className="ui-text-field__trailing">{trailingContent}</span>}
        {loading && (
          <span className="ui-text-field__spinner" role="status" aria-label={t('加载中')}>
            <LoaderCircle size={16} aria-hidden="true" />
          </span>
        )}
      </span>
      {helperText && <small id={helperId} className="ui-text-field__helper">{helperText}</small>}
      {error && <small id={errorId} className="ui-text-field__error" role="alert">{error}</small>}
    </div>
  )
})

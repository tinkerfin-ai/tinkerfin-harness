import { useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'

import { Button, Dialog, TextField } from '../../../components/ui'
import { useI18n } from '../../../i18n'

export interface ModalDialogProps {
  open: boolean
  title: string
  description?: ReactNode
  confirmLabel: string
  cancelLabel?: string
  tone?: 'default' | 'danger'
  inputLabel?: string
  inputPlaceholder?: string
  initialValue?: string
  isPending?: boolean
  restoreFocusTo?: HTMLElement | null
  onConfirm: (value?: string) => void | Promise<void>
  onCancel: () => void
}

export function ModalDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel,
  tone = 'default',
  inputLabel,
  inputPlaceholder,
  initialValue = '',
  isPending = false,
  restoreFocusTo,
  onConfirm,
  onCancel,
}: ModalDialogProps) {
  const { t } = useI18n()
  const inputRef = useRef<HTMLInputElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const [value, setValue] = useState(initialValue)

  useEffect(() => {
    if (!open) return
    setValue(initialValue)
  }, [initialValue, open])

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (isPending || (inputLabel && !value.trim())) return
    void onConfirm(inputLabel ? value.trim() : undefined)
  }

  const dialogClassName = [
    'modal-dialog--action',
    `is-${tone}`,
    inputLabel ? 'has-input' : '',
  ].filter(Boolean).join(' ')

  return (
    <Dialog
      open={open}
      title={title}
      description={description}
      className={dialogClassName}
      closeDisabled={isPending}
      restoreFocusTo={restoreFocusTo}
      initialFocusRef={inputLabel ? inputRef : cancelRef}
      onClose={onCancel}
    >
      <form onSubmit={handleSubmit}>
        {inputLabel && (
          <TextField
            ref={inputRef}
            rootClassName="modal-dialog-field"
            label={<span className="visually-hidden">{inputLabel}</span>}
            value={value}
            placeholder={inputPlaceholder}
            disabled={isPending}
            loading={isPending}
            fieldSize="md"
            onChange={(event) => setValue(event.target.value)}
          />
        )}
        <footer className="modal-dialog-actions">
          <Button ref={cancelRef} variant="secondary" disabled={isPending} onClick={onCancel}>{cancelLabel ?? t('取消')}</Button>
          <Button
            type="submit"
            variant={tone === 'danger' ? 'danger' : 'primary'}
            disabled={isPending || Boolean(inputLabel && !value.trim())}
          >
            {isPending ? t('处理中…') : confirmLabel}
          </Button>
        </footer>
      </form>
    </Dialog>
  )
}

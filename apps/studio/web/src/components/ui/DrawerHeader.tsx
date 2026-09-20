import { ArrowLeft, X } from 'lucide-react'
import { forwardRef } from 'react'
import type { ReactNode } from 'react'

import { IconButton } from './IconButton'

export interface DrawerHeaderProps {
  title: ReactNode
  description?: ReactNode
  closeLabel?: string
  onClose?: () => void
  backLabel?: string
  onBack?: () => void
  className?: string
  density?: 'regular' | 'compact'
}

export const DrawerHeader = forwardRef<HTMLButtonElement, DrawerHeaderProps>(function DrawerHeader({
  title,
  description,
  closeLabel,
  onClose,
  backLabel,
  onBack,
  className,
  density = 'regular',
}, ref) {
  const classes = [
    'ui-drawer-header',
    `ui-drawer-header--${density}`,
    description ? 'has-description' : '',
    onBack ? 'has-back' : '',
    className,
  ].filter(Boolean).join(' ')

  return (
    <header className={classes}>
      {onBack && backLabel && (
        <IconButton ref={ref} label={backLabel} icon={<ArrowLeft size={18} />} onClick={onBack} />
      )}
      <span className="ui-drawer-header__heading">
        <h2>{title}</h2>
        {description && <span>{description}</span>}
      </span>
      {!onBack && onClose && closeLabel && (
        <IconButton ref={ref} label={closeLabel} icon={<X size={18} />} onClick={onClose} />
      )}
    </header>
  )
})

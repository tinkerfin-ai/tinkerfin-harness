import { BRAND_MARK_SRC } from './BrandMark'

export type BrandLogoSize = 'sm' | 'md' | 'lg'

export interface BrandLogoProps {
  size?: BrandLogoSize
  className?: string
}

export function BrandLogo({ size = 'sm', className }: BrandLogoProps) {
  const classes = ['brand-logo', `brand-logo--${size}`, className]
    .filter(Boolean)
    .join(' ')

  return (
    <span className={classes} aria-hidden="true">
      <img
        className="brand-logo__mark"
        src={BRAND_MARK_SRC}
        width="647"
        height="458"
        alt=""
      />
      <img
        className="brand-logo__wordmark"
        src={`${BRAND_MARK_SRC}#wordmark`}
        width="1295"
        height="242"
        alt=""
      />
    </span>
  )
}

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
        src="/brand/tinkerfin-mark.png?v=1"
        width="647"
        height="458"
        alt=""
      />
      <img
        className="brand-logo__wordmark"
        src="/brand/tinkerfin-wordmark.png?v=1"
        width="1295"
        height="242"
        alt=""
      />
    </span>
  )
}

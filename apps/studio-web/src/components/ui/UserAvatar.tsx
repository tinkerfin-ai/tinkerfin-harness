import { UserRound } from 'lucide-react'
import { useState } from 'react'

export interface UserAvatarProps {
  avatarUrl: string | null
  size?: 'sm' | 'lg'
  className?: string
}

export function UserAvatar({
  avatarUrl,
  size = 'sm',
  className,
}: UserAvatarProps) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const canRenderImage = Boolean(avatarUrl && avatarUrl !== failedUrl)

  return (
    <span
      className={`user-avatar user-avatar--${size}${className ? ` ${className}` : ''}`}
      aria-hidden="true"
    >
      {canRenderImage ? (
        <img
          src={avatarUrl ?? undefined}
          alt=""
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailedUrl(avatarUrl)}
        />
      ) : (
        <UserRound className="user-avatar__fallback" aria-hidden="true" />
      )}
    </span>
  )
}

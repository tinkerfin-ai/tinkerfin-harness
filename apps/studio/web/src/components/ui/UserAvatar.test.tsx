import { fireEvent, render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { UserAvatar } from './UserAvatar'

describe('UserAvatar', () => {
  it('loads a real avatar without referrer and falls back after an image error', () => {
    const { container } = render(
      <UserAvatar
        avatarUrl="https://cdn.example.test/avatar.webp"
      />,
    )
    const image = container.querySelector('img')

    expect(image).toHaveAttribute('src', 'https://cdn.example.test/avatar.webp')
    expect(image).toHaveAttribute('referrerpolicy', 'no-referrer')
    expect(image).toHaveAttribute('alt', '')

    fireEvent.error(image as HTMLImageElement)

    expect(container.querySelector('img')).not.toBeInTheDocument()
    expect(container.textContent).toBe('')
  })

  it('uses the frontend default icon without a saved avatar', () => {
    const { container } = render(<UserAvatar avatarUrl={null} size="lg" />)

    expect(container.textContent).toBe('')

  })
})

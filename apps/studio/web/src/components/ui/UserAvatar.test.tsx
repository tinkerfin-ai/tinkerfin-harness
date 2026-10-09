import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { UserAvatar } from './UserAvatar'

describe('UserAvatar', () => {
  it('loads a real avatar without referrer and falls back after an image error', () => {
    const { container } = render(
      <UserAvatar
        avatarUrl="https://cdn.example.test/avatar.webp"
        displayName="云杉"
        username="yunsan"
      />,
    )
    const image = container.querySelector('img')

    expect(image).toHaveAttribute('src', 'https://cdn.example.test/avatar.webp')
    expect(image).toHaveAttribute('referrerpolicy', 'no-referrer')
    expect(image).toHaveAttribute('alt', '')

    fireEvent.error(image as HTMLImageElement)

    expect(container.querySelector('img')).not.toBeInTheDocument()
    expect(screen.getByText('云')).toBeInTheDocument()
  })

  it('uses the username initial when display name is empty', () => {
    render(<UserAvatar avatarUrl={null} displayName="" username="yunsan" size="lg" />)

    expect(screen.getByText('Y')).toBeInTheDocument()

  })
})

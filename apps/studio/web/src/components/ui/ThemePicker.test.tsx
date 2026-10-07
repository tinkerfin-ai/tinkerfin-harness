import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { THEME_PREFERENCE_STORAGE_KEY } from '../../theme'
import { ThemePicker } from './ThemePicker'
import { MOTION_DURATION_MS } from './motion'

type MediaQueryController = {
  mediaQuery: MediaQueryList
  setMatches: (matches: boolean) => void
}

const createMediaQuery = (initialMatches: boolean): MediaQueryController => {
  let matches = initialMatches
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const mediaQuery = {
    get matches() { return matches },
    media: '(prefers-color-scheme: dark)',
    onchange: null,
    addEventListener: vi.fn((_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener)),
    removeEventListener: vi.fn((_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener)),
    addListener: vi.fn((listener: (event: MediaQueryListEvent) => void) => listeners.add(listener)),
    removeListener: vi.fn((listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener)),
    dispatchEvent: vi.fn(() => true),
  } as unknown as MediaQueryList

  return {
    mediaQuery,
    setMatches(nextMatches) {
      matches = nextMatches
      const event = { matches, media: mediaQuery.media } as MediaQueryListEvent
      listeners.forEach((listener) => listener(event))
    },
  }
}

const installThemeColorMeta = () => {
  const meta = document.createElement('meta')
  meta.name = 'theme-color'
  document.head.appendChild(meta)
  return meta
}

describe('ThemePicker', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    delete document.documentElement.dataset.theme
    delete document.documentElement.dataset.themePreference
    document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => meta.remove())
  })

  afterEach(() => vi.useRealTimers())

  it('renders a three-segment radio group, defaults to light, and persists dark', () => {
    const media = createMediaQuery(true)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    const themeColor = installThemeColorMeta()

    const firstRender = render(<ThemePicker />)
    const themeGroup = screen.getByRole('group', { name: '主题' })
    const systemOption = screen.getByRole('radio', { name: '跟随系统' })
    const lightOption = screen.getByRole('radio', { name: '浅色' })
    const darkOption = screen.getByRole('radio', { name: '深色' })

    expect(themeGroup).toContainElement(systemOption)
    expect(themeGroup).toContainElement(lightOption)
    expect(themeGroup).toContainElement(darkOption)
    expect(systemOption).not.toBeChecked()
    expect(lightOption).toBeChecked()
    expect(darkOption).not.toBeChecked()
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    fireEvent.pointerEnter(themeGroup)
    act(() => { vi.advanceTimersByTime(MOTION_DURATION_MS.slow) })
    expect(themeGroup).toHaveAttribute('data-settled', 'true')
    fireEvent.click(darkOption)

    expect(darkOption).toBeChecked()
    expect(document.documentElement).toHaveAttribute('data-theme-preference', 'dark')
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(window.localStorage.getItem(THEME_PREFERENCE_STORAGE_KEY)).toBe('dark')
    expect(themeColor).toHaveAttribute('content', '#151517')

    firstRender.unmount()
    render(<ThemePicker />)
    expect(screen.getByRole('radio', { name: '深色' })).toBeChecked()
  })

  it('tracks operating-system changes only while following the system', () => {
    const media = createMediaQuery(false)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    installThemeColorMeta()

    render(<ThemePicker />)
    const themeGroup = screen.getByRole('group', { name: '主题' })
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')

    fireEvent.pointerEnter(themeGroup)
    act(() => { vi.advanceTimersByTime(MOTION_DURATION_MS.slow) })
    expect(themeGroup).toHaveAttribute('data-settled', 'true')
    const systemOption = screen.getByRole('radio', { name: '跟随系统' })
    fireEvent.click(systemOption)
    expect(systemOption).toBeChecked()
    expect(document.documentElement).toHaveAttribute('data-theme-preference', 'system')
    act(() => media.setMatches(true))
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')

    fireEvent.pointerEnter(themeGroup)
    act(() => { vi.advanceTimersByTime(MOTION_DURATION_MS.slow) })
    expect(themeGroup).toHaveAttribute('data-settled', 'true')
    fireEvent.click(screen.getByRole('radio', { name: '浅色' }))
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')

    act(() => media.setMatches(false))
    act(() => media.setMatches(true))
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
  })

  it('opens from hover or keyboard focus and closes on selection or a real outside action', () => {
    const media = createMediaQuery(false)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    installThemeColorMeta()

    render(
      <>
        <ThemePicker />
        <button type="button">主题控件外</button>
      </>,
    )

    const themeGroup = screen.getByRole('group', { name: '主题' })
    const lightOption = screen.getByRole('radio', { name: '浅色' })

    expect(themeGroup).toHaveAttribute('data-expanded', 'false')
    fireEvent.pointerEnter(themeGroup)
    expect(themeGroup).toHaveAttribute('data-expanded', 'true')
    fireEvent.pointerLeave(themeGroup)
    expect(themeGroup).toHaveAttribute('data-expanded', 'false')

    fireEvent.pointerEnter(themeGroup)
    expect(themeGroup).toHaveAttribute('data-expanded', 'true')
    fireEvent.pointerDown(screen.getByRole('button', { name: '主题控件外' }))
    fireEvent.click(screen.getByRole('button', { name: '主题控件外' }))
    expect(themeGroup).toHaveAttribute('data-expanded', 'false')

    act(() => lightOption.focus())
    expect(themeGroup).toContainElement(document.activeElement as HTMLElement)
    expect(themeGroup).toHaveAttribute('data-expanded', 'true')

    act(() => { vi.advanceTimersByTime(MOTION_DURATION_MS.slow) })
    expect(themeGroup).toHaveAttribute('data-settled', 'true')
    fireEvent.click(screen.getByRole('radio', { name: '深色' }))
    expect(screen.getByRole('radio', { name: '深色' })).toBeChecked()
    expect(themeGroup).toHaveAttribute('data-expanded', 'false')
  })

  it('opens on touch and collapses after a settled theme is selected', () => {
    const media = createMediaQuery(false)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    installThemeColorMeta()

    render(<ThemePicker />)
    const themeGroup = screen.getByRole('group', { name: '主题' })
    const lightOption = screen.getByRole('radio', { name: '浅色' })
    const darkOption = screen.getByRole('radio', { name: '深色' })

    const touchPointerDown = new MouseEvent('pointerdown', { bubbles: true })
    Object.defineProperty(touchPointerDown, 'pointerType', { value: 'touch' })
    fireEvent(lightOption, touchPointerDown)
    expect(themeGroup).toHaveAttribute('data-expanded', 'true')

    act(() => { vi.advanceTimersByTime(MOTION_DURATION_MS.slow) })
    expect(themeGroup).toHaveAttribute('data-settled', 'true')
    fireEvent.click(darkOption)
    expect(darkOption).toBeChecked()
    expect(themeGroup).toHaveAttribute('data-expanded', 'false')
  })

  it('selects the visible label only after the expansion has settled', () => {
    const media = createMediaQuery(false)
    vi.stubGlobal('matchMedia', vi.fn(() => media.mediaQuery))
    installThemeColorMeta()
    render(<ThemePicker />)

    const themeGroup = screen.getByRole('group', { name: '主题' })
    fireEvent.pointerEnter(themeGroup)
    act(() => { vi.advanceTimersByTime(MOTION_DURATION_MS.slow) })
    expect(themeGroup).toHaveAttribute('data-settled', 'true')
    fireEvent.pointerMove(screen.getByRole('radio', { name: '深色' }))
    expect(screen.getByRole('tooltip', { name: '深色' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: '深色' }))

    expect(screen.getByRole('radio', { name: '深色' })).toBeChecked()
    expect(themeGroup).toHaveAttribute('data-expanded', 'false')
  })
})

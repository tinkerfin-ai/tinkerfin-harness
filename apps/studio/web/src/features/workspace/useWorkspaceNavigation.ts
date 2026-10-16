import { useCallback, useEffect, useRef, useState } from 'react'

export type SidebarMode = 'expanded' | 'rail' | 'overlay'
type ViewportBand = 'desktop' | 'tablet' | 'mobile'

const DESKTOP_QUERY = '(min-width: 1024px)'
const TABLET_QUERY = '(min-width: 768px)'
const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'
const COLLAPSE_CONTENT_DELAY_MS = 100
const SIDEBAR_SETTLE_FALLBACK_MS = 350

const readViewportBand = (): ViewportBand => {
  if (window.matchMedia(DESKTOP_QUERY).matches) return 'desktop'
  if (window.matchMedia(TABLET_QUERY).matches) return 'tablet'
  return 'mobile'
}

const defaultModeForBand = (band: ViewportBand): SidebarMode => {
  if (band === 'desktop') return 'expanded'
  if (band === 'tablet') return 'rail'
  return 'overlay'
}

export function useWorkspaceNavigation() {
  const initialBand = readViewportBand()
  const [band, setBand] = useState<ViewportBand>(initialBand)
  const [mode, setMode] = useState<SidebarMode>(() => defaultModeForBand(initialBand))
  const [settledMode, setSettledMode] = useState<SidebarMode>(() => defaultModeForBand(initialBand))
  const [overlayOpen, setOverlayOpen] = useState(false)
  const [wideInteractive, setWideInteractive] = useState(initialBand === 'desktop')
  const [railInteractive, setRailInteractive] = useState(initialBand === 'tablet')
  const overlayTriggerRef = useRef<HTMLButtonElement>(null)
  const targetModeRef = useRef(mode)
  const contentTimerRef = useRef<number | null>(null)
  const settleTimerRef = useRef<number | null>(null)

  const clearTransitionTasks = useCallback(() => {
    if (contentTimerRef.current != null) {
      window.clearTimeout(contentTimerRef.current)
      contentTimerRef.current = null
    }
    if (settleTimerRef.current != null) {
      window.clearTimeout(settleTimerRef.current)
      settleTimerRef.current = null
    }
  }, [])

  const settle = useCallback((expectedMode = targetModeRef.current) => {
    if (targetModeRef.current !== expectedMode) return
    if (settleTimerRef.current != null) {
      window.clearTimeout(settleTimerRef.current)
      settleTimerRef.current = null
    }
    setSettledMode(expectedMode)
  }, [])

  const changeMode = useCallback((nextMode: SidebarMode) => {
    clearTransitionTasks()
    targetModeRef.current = nextMode
    setMode(nextMode)
    const reducedMotion = window.matchMedia(REDUCED_MOTION_QUERY).matches

    if (nextMode === 'expanded') {
      setWideInteractive(true)
      setRailInteractive(false)
    } else if (nextMode === 'rail') {
      if (reducedMotion) {
        setWideInteractive(false)
        setRailInteractive(true)
      } else {
        contentTimerRef.current = window.setTimeout(() => {
          if (targetModeRef.current !== 'rail') return
          setWideInteractive(false)
          setRailInteractive(true)
          contentTimerRef.current = null
        }, COLLAPSE_CONTENT_DELAY_MS)
      }
    } else {
      setWideInteractive(overlayOpen)
      setRailInteractive(false)
    }

    if (reducedMotion || nextMode === 'overlay') {
      settle(nextMode)
    } else {
      settleTimerRef.current = window.setTimeout(
        () => settle(nextMode),
        SIDEBAR_SETTLE_FALLBACK_MS,
      )
    }
  }, [clearTransitionTasks, overlayOpen, settle])

  useEffect(() => {
    const desktopMedia = window.matchMedia(DESKTOP_QUERY)
    const tabletMedia = window.matchMedia(TABLET_QUERY)
    const handleBandChange = () => {
      const nextBand = readViewportBand()
      setBand((currentBand) => {
        if (currentBand === nextBand) return currentBand
        const nextMode = defaultModeForBand(nextBand)
        setOverlayOpen(false)
        changeMode(nextMode)
        return nextBand
      })
    }
    desktopMedia.addEventListener('change', handleBandChange)
    tabletMedia.addEventListener('change', handleBandChange)
    return () => {
      desktopMedia.removeEventListener('change', handleBandChange)
      tabletMedia.removeEventListener('change', handleBandChange)
    }
  }, [changeMode])

  useEffect(() => () => clearTransitionTasks(), [clearTransitionTasks])

  const toggleDesktopMode = useCallback(() => {
    if (band === 'mobile') return
    changeMode(mode === 'expanded' ? 'rail' : 'expanded')
  }, [band, changeMode, mode])

  const openOverlay = useCallback(() => {
    if (band !== 'mobile') return
    setOverlayOpen(true)
    setWideInteractive(true)
  }, [band])

  const closeOverlay = useCallback((restoreFocus = true) => {
    if (band !== 'mobile') return
    setOverlayOpen(false)
    setWideInteractive(false)
    if (restoreFocus) window.requestAnimationFrame(() => overlayTriggerRef.current?.focus())
  }, [band])

  const requestExpanded = useCallback(() => {
    if (band === 'mobile') {
      openOverlay()
      return
    }
    if (mode !== 'expanded') changeMode('expanded')
  }, [band, changeMode, mode, openOverlay])

  return {
    band,
    mode,
    settledMode,
    overlayOpen,
    wideInteractive,
    railInteractive,
    overlayTriggerRef,
    toggleDesktopMode,
    openOverlay,
    closeOverlay,
    requestExpanded,
  }
}

import { useCallback, useEffect, useState } from 'react'

import {
  applyThemePreference,
  persistThemePreference,
  readThemePreference,
  subscribeToSystemTheme,
  THEME_MEDIA_QUERY,
  type ThemePreference,
} from '../../theme'

export function useThemePreference() {
  const [preference, setPreference] = useState<ThemePreference>(readThemePreference)

  useEffect(() => {
    const mediaQuery = window.matchMedia(THEME_MEDIA_QUERY)
    applyThemePreference(preference, mediaQuery)
    return subscribeToSystemTheme(preference, mediaQuery)
  }, [preference])

  const selectPreference = useCallback((nextPreference: ThemePreference) => {
    persistThemePreference(nextPreference)
    applyThemePreference(nextPreference)
    setPreference(nextPreference)
  }, [])

  return { preference, selectPreference }
}

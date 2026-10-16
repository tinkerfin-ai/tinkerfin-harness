export const THEME_PREFERENCE_STORAGE_KEY = 'tinkerfin:theme'
export const THEME_MEDIA_QUERY = '(prefers-color-scheme: dark)'

export type ThemePreference = 'system' | 'light' | 'dark'
export type ResolvedTheme = Exclude<ThemePreference, 'system'>

const THEME_COLORS: Record<ResolvedTheme, string> = {
  light: '#ffffff',
  dark: '#151517',
}

const isThemePreference = (value: string | null): value is ThemePreference => (
  value === 'system' || value === 'light' || value === 'dark'
)

export const readThemePreference = (): ThemePreference => {
  try {
    const storedPreference = window.localStorage.getItem(THEME_PREFERENCE_STORAGE_KEY)
    return isThemePreference(storedPreference) ? storedPreference : 'light'
  } catch {
    const bootstrapPreference = document.documentElement.dataset.themePreference ?? null
    return isThemePreference(bootstrapPreference) ? bootstrapPreference : 'light'
  }
}

export const resolveTheme = (
  preference: ThemePreference,
  prefersDark: boolean,
): ResolvedTheme => (
  preference === 'system' ? (prefersDark ? 'dark' : 'light') : preference
)

export const applyThemePreference = (
  preference: ThemePreference,
  mediaQuery = window.matchMedia(THEME_MEDIA_QUERY),
): ResolvedTheme => {
  const theme = resolveTheme(preference, mediaQuery.matches)
  const root = document.documentElement
  root.dataset.themePreference = preference
  root.dataset.theme = theme
  root.style.colorScheme = theme
  document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    ?.setAttribute('content', THEME_COLORS[theme])
  return theme
}

export const persistThemePreference = (preference: ThemePreference): void => {
  try {
    window.localStorage.setItem(THEME_PREFERENCE_STORAGE_KEY, preference)
  } catch {
    // 浏览器禁用本地存储时，主题仍在当前页面内生效
  }
}

export const subscribeToSystemTheme = (
  preference: ThemePreference,
  mediaQuery: MediaQueryList,
): (() => void) => {
  if (preference !== 'system') return () => undefined
  const applySystemTheme = () => applyThemePreference('system', mediaQuery)
  mediaQuery.addEventListener?.('change', applySystemTheme)
  return () => mediaQuery.removeEventListener?.('change', applySystemTheme)
}

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import {
  applyResolvedLanguage,
  LANGUAGE_STORAGE_KEY,
  persistLanguagePreference,
  readLanguagePreference,
  resolveLanguage,
  resolveSystemLanguage,
  translate,
  type LanguagePreference,
} from './locale'
import type { TranslationKey, TranslationParams } from './messages'
import { LocaleContext } from './LocaleContext'

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<LanguagePreference>(readLanguagePreference)
  const [systemLanguage, setSystemLanguage] = useState(resolveSystemLanguage)
  const locale = preference === 'system' ? systemLanguage : resolveLanguage(preference)

  useEffect(() => {
    document.documentElement.dataset.languagePreference = preference
    applyResolvedLanguage(locale)
  }, [locale, preference])

  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      if (event.key !== LANGUAGE_STORAGE_KEY) return
      const nextPreference = readLanguagePreference()
      if (nextPreference === 'system') setSystemLanguage(resolveSystemLanguage())
      setPreferenceState(nextPreference)
    }
    const handleLanguageChange = () => {
      setSystemLanguage(resolveSystemLanguage())
    }
    window.addEventListener('storage', handleStorage)
    window.addEventListener('languagechange', handleLanguageChange)
    return () => {
      window.removeEventListener('storage', handleStorage)
      window.removeEventListener('languagechange', handleLanguageChange)
    }
  }, [])

  const setPreference = useCallback((next: LanguagePreference) => {
    persistLanguagePreference(next)
    if (next === 'system') setSystemLanguage(resolveSystemLanguage())
    setPreferenceState(next)
  }, [])
  const t = useCallback(
    (key: TranslationKey, params?: TranslationParams) => translate(locale, key, params),
    [locale],
  )
  const value = useMemo(
    () => ({ preference, locale, setPreference, t }),
    [locale, preference, setPreference, t],
  )

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
}

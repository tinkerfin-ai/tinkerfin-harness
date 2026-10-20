import { createContext } from 'react'

import { translate, type LanguagePreference, type ResolvedLanguage } from './locale'
import type { TranslationKey, TranslationParams } from './messages'

export interface LocaleContextValue {
  preference: LanguagePreference
  locale: ResolvedLanguage
  setPreference: (preference: LanguagePreference) => void
  t: (key: TranslationKey, params?: TranslationParams) => string
}

export const LocaleContext = createContext<LocaleContextValue>({
  preference: 'zh-CN',
  locale: 'zh-CN',
  setPreference: () => undefined,
  t: (key, params) => translate('zh-CN', key, params),
})

import type { TranslationKey, TranslationParams } from './messages'
import { englishMessages } from './messages'

export const LANGUAGE_STORAGE_KEY = 'tinkerfin:language'
export type LanguagePreference = 'system' | 'zh-CN' | 'en'
export type ResolvedLanguage = Exclude<LanguagePreference, 'system'>

const isPreference = (value: string | null): value is LanguagePreference => (
  value === 'system' || value === 'zh-CN' || value === 'en'
)

export const readLanguagePreference = (): LanguagePreference => {
  try {
    const stored = window.localStorage.getItem(LANGUAGE_STORAGE_KEY)
    return isPreference(stored) ? stored : 'zh-CN'
  } catch {
    return 'zh-CN'
  }
}

export const resolveSystemLanguage = (
  languages: readonly string[] = navigator.languages,
): ResolvedLanguage => {
  for (const language of languages) {
    const normalized = language.toLocaleLowerCase('en-US')
    if (normalized === 'zh' || normalized.startsWith('zh-')) return 'zh-CN'
    if (normalized === 'en' || normalized.startsWith('en-')) return 'en'
  }
  return 'en'
}

export const resolveLanguage = (
  preference: LanguagePreference,
  languages?: readonly string[],
): ResolvedLanguage => preference === 'system'
  ? resolveSystemLanguage(languages)
  : preference

export const persistLanguagePreference = (preference: LanguagePreference) => {
  try {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, preference)
  } catch {
    // 本地存储不可用时，语言仍在当前页面内生效
  }
}

export const translate = (
  locale: ResolvedLanguage,
  key: TranslationKey,
  params: TranslationParams = {},
) => {
  const template = locale === 'en' ? englishMessages[key] : key
  return template.replace(/\{(\w+)\}/g, (placeholder, name: string) => (
    Object.hasOwn(params, name) ? String(params[name]) : placeholder
  ))
}

export const translateCurrent = (key: TranslationKey, params?: TranslationParams) => (
  translate(resolveLanguage(readLanguagePreference()), key, params)
)

export const applyResolvedLanguage = (locale: ResolvedLanguage) => {
  const root = document.documentElement
  root.lang = locale
  root.dataset.language = locale
  document.querySelector<HTMLMetaElement>('meta[name="description"]')?.setAttribute(
    'content',
    translate(locale, 'TinkerFin AI Agent 对话工作台'),
  )
}

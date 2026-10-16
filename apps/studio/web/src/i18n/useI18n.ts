import { useContext } from 'react'

import { LocaleContext } from './LocaleContext'

export const useI18n = () => useContext(LocaleContext)

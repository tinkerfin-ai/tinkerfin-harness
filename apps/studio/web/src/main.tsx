// 先确定样式层顺序，避免组件颜色被基础样式覆盖
import './styles/tokens.css'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { ErrorBoundary } from './components/ui'
import { GlobalErrorFallback } from './components/ui/GlobalErrorFallback'
import './styles/fonts.css'
import '@fontsource-variable/noto-sans-sc'
import './styles/typography.css'
import './styles/global.css'
import './components/ui/ui.css'
import { normalizeAppLocation } from './lib/threadRoute'
import { LocaleProvider } from './i18n'

normalizeAppLocation()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <LocaleProvider>
      <ErrorBoundary
        fallback={() => <GlobalErrorFallback />}
        onError={(error) => console.error('TinkerFin 页面渲染失败', error)}
      >
        <App />
      </ErrorBoundary>
    </LocaleProvider>
  </StrictMode>,
)

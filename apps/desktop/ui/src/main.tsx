import { createRoot } from 'react-dom/client'
import { App } from './App'
import './styles/tokens.css'
import './styles/typography.css'
import './styles/fonts.css'
import '@fontsource-variable/noto-sans-sc/index.css'
import './styles/layout-base.css'
import './components/ui/ui.css'
import './styles/picker.css'
import './styles/layout.css'
import './styles/native.css'

createRoot(document.getElementById('root')!).render(<App />)

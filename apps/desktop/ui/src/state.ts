export type Page = 'connection' | 'workspace' | 'permissions' | 'account'
export type Language = 'system' | 'zh-Hans' | 'en'
export type Connection = 'loading' | 'unpaired' | 'authorizing' | 'connecting' | 'online' | 'offline' | 'revoked'
export type UpdatePhase = 'idle' | 'checking' | 'available' | 'latest' | 'check-error'
  | 'downloading' | 'download-error' | 'verifying' | 'verify-error' | 'waiting'
  | 'installing' | 'install-error' | 'restarting' | 'restart-error' | 'success'

/** 仅保留界面草稿，授权凭证与已提交配置始终由原生宿主管理 */
export interface ViewState {
  page: Page
  connectionForm: boolean
  service: string
  api: string
  manualAPI: boolean
  editingName: boolean
  name: string
}

export interface UpdateState {
  phase: UpdatePhase
  percent: number
  currentVersion: string
  targetVersion: string
  failure: 'none' | 'network' | 'storage' | 'release' | 'unavailable'
}

export interface DesktopState {
  revision: number
  surface: 'workbench' | 'status' | 'updater'
  language: Language
  locale: 'zh-Hans' | 'en'
  theme: 'light' | 'dark'
  ready: boolean
  connected: boolean
  connection: Connection
  connectionReason: 'restore' | 'authorization' | 'retry'
  authorizationPhase: 'idle' | 'starting' | 'waiting' | 'cancelling'
  authorizationError: string
  connectionError: string
  service: string
  api: string
  switching: boolean
  name: string
  account: string
  version: string
  busy: '' | 'name' | 'logout' | 'connect' | 'retry'
  projects: { id: string; name: string }[]
  projectsLoading: boolean
  openingProject: string
  permissions: { accessibility: boolean; screenCapture: boolean }
  update: UpdateState
  resume: ViewState | null
}

export type DesktopAction =
  | { type: 'ready' | 'open-workbench' | 'quit' | 'open-studio' | 'retry' | 'cancel-authorization' | 'open-authorization' | 'refresh-projects' | 'logout' | 'update-confirm' | 'update-cancel' | 'update-retry' }
  | { type: 'connect'; service: string; api: string }
  | { type: 'save-name'; name: string }
  | { type: 'language'; language: Language }
  | { type: 'permission'; permission: 'accessibility' | 'screenCapture' }
  | { type: 'open-project'; id: string }
  | { type: 'save-view' | 'check-update'; view: ViewState }

export type ActionResult = { ok: true } | { ok: false; message: string; field: '' | 'service' | 'api' | 'name' }

/**
 * 极简「会话 URL 路由」：用 `?thread={threadId}` 查询参数记录当前会话，
 * 让浏览器刷新后能回到原会话；自动化页面使用 `?page=automation`
 *
 * 用 `history.replaceState` 同步而非 `pushState`——只保证 URL 始终反映当前
 * 会话、刷新可还原，不为每次选择追加浏览器历史条目
 */

const THREAD_PARAM = 'thread'
const APP_PATHNAME = '/'
const LAST_VALID_LOCATION_KEY = 'tinkerfin:last-valid-location'

function relativeLocation(url: URL): string {
  return `${url.pathname}${url.search}${url.hash}`
}

function readStoredValidLocation(): string | null {
  try {
    const storedLocation = window.sessionStorage.getItem(LAST_VALID_LOCATION_KEY)
    if (!storedLocation) return null
    const url = new URL(storedLocation, window.location.origin)
    if (url.origin !== window.location.origin || url.pathname !== APP_PATHNAME) return null
    return relativeLocation(url)
  } catch {
    return null
  }
}

function rememberCurrentLocation(): void {
  try {
    window.sessionStorage.setItem(
      LAST_VALID_LOCATION_KEY,
      window.location.pathname + window.location.search + window.location.hash,
    )
  } catch {
    // 浏览器禁用会话存储时仍可回退到应用根路径
  }
}

/** 在应用挂载前把未知路径替换为最近一次有效的应用地址 */
export function normalizeAppLocation(): void {
  if (window.location.pathname === APP_PATHNAME) {
    rememberCurrentLocation()
    return
  }

  const desired = readStoredValidLocation() ?? APP_PATHNAME
  window.history.replaceState(null, '', desired)
}

/** 从当前 URL 读取 `?thread=` 指定的会话 id，无则返回空串 */
export function readThreadFromLocation(): string {
  if (readPageFromLocation() === 'automation') return ''
  return new URLSearchParams(window.location.search).get(THREAD_PARAM) ?? ''
}

export type WorkspacePage = 'conversation' | 'automation'

export function readPageFromLocation(): WorkspacePage {
  return new URLSearchParams(window.location.search).get('page') === 'automation'
    ? 'automation'
    : 'conversation'
}

/** 按当前页面一次性同步地址；自动化不携带后台会话身份，草稿不携带 thread */
export function writeWorkspaceToLocation(page: WorkspacePage, threadId: string): void {
  const url = new URL(window.location.href)
  url.pathname = APP_PATHNAME
  if (page === 'automation') url.searchParams.set('page', page)
  else url.searchParams.delete('page')
  if (page === 'conversation' && threadId) url.searchParams.set(THREAD_PARAM, threadId)
  else url.searchParams.delete(THREAD_PARAM)
  const desired = relativeLocation(url)
  if (desired !== relativeLocation(new URL(window.location.href))) {
    window.history.replaceState(null, '', desired)
  }
  rememberCurrentLocation()
}
